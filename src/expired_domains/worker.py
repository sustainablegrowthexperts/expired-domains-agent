"""Automatic background workers for the staged domain-evaluation pipeline.

The web app starts a main polling worker that claims every domain whose niche is
``NULL`` and has not been blocked, runs the deterministic blocklist and
lexicon-first pipeline, writes the result back, runs the local prefilter over
domains that now have a niche, checks prefilter-passing keywords with the LLM,
and collects paid data when enabled. Separate Free DR, Live, Wayback, and
evaluation workers poll independently: the DR worker checks keyword-passed
domains against the free Ahrefs Domain Rating endpoint, the Live worker probes
DR-passed domains, the Wayback worker scores Live-passed domains against
archived captures, and the evaluation worker scores domains that have a
successful Ahrefs run. Each worker sleeps between cycles, so newly imported or
collected domains are picked up without another import or a manual command.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from expired_domains.blocklist import (
    Blocklist,
    block_mapping,
    load_blocklist,
    split_blocked,
)
from expired_domains.collection import collect_pending
from expired_domains.config import (
    DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS,
    DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE,
    DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
    DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
    DEFAULT_DR_CHECK_MAX_ATTEMPTS,
    DEFAULT_DR_FLOOR,
    DEFAULT_EVALUATION_CHECKS_PER_CYCLE,
    DEFAULT_KEYWORD_CHECK_MODEL,
    DEFAULT_LIVE_CHECK_MAX_ATTEMPTS,
    DEFAULT_LIVE_CHECKS_PER_CYCLE,
    DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
    DEFAULT_WAYBACK_CHECK_MAX_ATTEMPTS,
    DEFAULT_WAYBACK_CHECKS_PER_CYCLE,
)
from expired_domains.dr_check import (
    DR_CHECK_BATCHES_PER_CYCLE,
    DomainRatingChecker,
    dr_check_pending,
)
from expired_domains.evaluation import (
    EvaluationConfig,
    EvidenceReviewLLM,
    evaluate_pending,
    load_evaluation_config,
)
from expired_domains.keyword_check import (
    KEYWORD_CHECK_BATCH_SIZE,
    KEYWORD_CHECK_BATCHES_PER_CYCLE,
    KeywordChecker,
    batches,
    load_keyword_candidates,
)
from expired_domains.live_check import (
    LiveCheckConfig,
    LiveProber,
    live_check_pending,
    load_live_config,
)
from expired_domains.models import RunSummary
from expired_domains.niches import (
    NicheLexicon,
    NicheLLM,
    StubNicheClassifier,
    categorize_domains,
    load_niche_lexicon,
)
from expired_domains.prefilter import (
    PrefilterConfig,
    evaluate_domain,
    load_prefilter_candidates,
    load_prefilter_config,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    list_pending_domains,
    mark_keyword_checks_processing,
    reset_keyword_checks,
    set_domain_blocks,
    set_domain_niches,
    set_keyword_check_results,
    set_prefilter_results,
    transaction,
)
from expired_domains.wayback import (
    WaybackConfig,
    WaybackLLM,
    WaybackSource,
    load_wayback_config,
    wayback_check_pending,
)

logger = logging.getLogger(__name__)


def classify_pending(
    db_path: Path,
    *,
    lexicon: NicheLexicon | None = None,
    llm: NicheLLM | None = None,
    blocklist: Blocklist | None = None,
) -> int:
    """Classify every currently pending domain and return how many were updated.

    The deterministic blocklist runs first. Blocked domains are marked
    terminal and are never sent to the niche dictionary or the LLM.
    """
    resolved_lexicon = lexicon or load_niche_lexicon()
    resolved_llm = llm if llm is not None else StubNicheClassifier()
    resolved_blocklist = blocklist or load_blocklist()

    conn = connect(db_path)
    try:
        pending = list_pending_domains(conn)
    finally:
        conn.close()

    if not pending:
        return 0

    allowed, blocked = split_blocked(pending, resolved_blocklist)
    assignments = categorize_domains(allowed, resolved_lexicon, llm=resolved_llm)

    conn = connect(db_path)
    try:
        with transaction(conn):
            blocked_updated = set_domain_blocks(conn, block_mapping(blocked))
            niche_updated = set_domain_niches(
                conn,
                {
                    assignment.domain: (assignment.niche, assignment.source)
                    for assignment in assignments
                },
            )
    finally:
        conn.close()
    return blocked_updated + niche_updated


def prefilter_pending(
    db_path: Path,
    *,
    config: PrefilterConfig | None = None,
    now: datetime | None = None,
) -> int:
    """Prefilter every classified domain that has no result for the current config.

    Returns the number of domains updated. Blocked and unclassified domains are
    left for the earlier stages.
    """
    resolved = config or load_prefilter_config()
    moment = _utc(now) or datetime.now(UTC)
    checked_at = moment.isoformat(timespec="seconds")

    conn = connect(db_path)
    try:
        candidates = load_prefilter_candidates(conn, resolved.version)
    finally:
        conn.close()

    if not candidates:
        return 0

    results = {
        candidate.domain: evaluate_domain(
            candidate.domain,
            niche=candidate.niche,
            metrics=candidate.metrics,
            config=resolved,
            now=moment,
            checked_at=checked_at,
        )
        for candidate in candidates
    }

    conn = connect(db_path)
    try:
        with transaction(conn):
            return set_prefilter_results(conn, results)
    finally:
        conn.close()


def keyword_check_pending(
    db_path: Path,
    *,
    checker: KeywordChecker | None,
    model: str = DEFAULT_KEYWORD_CHECK_MODEL,
    batch_size: int = KEYWORD_CHECK_BATCH_SIZE,
    max_batches: int | None = None,
) -> int:
    """Check every prefilter-passing keyword that has no verdict for ``model``.

    Keywords are sent to the LLM ten at a time. A failed batch is logged and
    left pending so the next worker cycle retries it; it never blocks the rest
    of the batch or the pipeline.
    """
    if checker is None:
        return 0

    conn = connect(db_path)
    try:
        candidates = load_keyword_candidates(conn, model)
    finally:
        conn.close()

    if not candidates:
        return 0

    pending_batches = batches([candidate.domain for candidate in candidates], batch_size)
    if max_batches is not None:
        pending_batches = pending_batches[: max(0, max_batches)]

    updated = 0
    for batch in pending_batches:
        domains = set(batch)
        started_at = datetime.now(UTC).isoformat(timespec="seconds")

        conn = connect(db_path)
        try:
            with transaction(conn):
                mark_keyword_checks_processing(
                    conn,
                    batch,
                    model=model,
                    started_at=started_at,
                )
        finally:
            conn.close()

        try:
            verdicts = checker.check(batch)
        except Exception as exc:  # noqa: BLE001 - retry this batch next cycle
            logger.warning("keyword check batch failed: %s", exc)
            conn = connect(db_path)
            try:
                with transaction(conn):
                    reset_keyword_checks(conn, batch)
            finally:
                conn.close()
            continue

        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        accepted = {domain: verdict for domain, verdict in verdicts.items() if domain in domains}
        missing = [domain for domain in batch if domain not in accepted]

        conn = connect(db_path)
        try:
            with transaction(conn):
                if accepted:
                    updated += set_keyword_check_results(
                        conn,
                        accepted,
                        model=model,
                        checked_at=finished_at,
                    )
                if missing:
                    reset_keyword_checks(conn, missing)
        finally:
            conn.close()

    return updated


def _utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime, or ``None`` when no value was supplied."""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


_INIT_LOCK = threading.Lock()


class PollingWorker:
    """Base polling thread for one pipeline stage."""

    worker_name = "pipeline worker"
    thread_name = "pipeline-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._interval = max(0.1, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the polling thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self.thread_name,
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to stop and wait briefly for the current cycle."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Return True while the polling thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            # Two workers can start together; serialise the schema migration.
            with _INIT_LOCK:
                init_db(self._db_path)
        except Exception:  # noqa: BLE001 - keep the web app alive
            logger.exception("%s could not initialise the index", self.worker_name)
            return

        logger.info("%s started for %s", self.worker_name, self._db_path)
        while not self._stop.is_set():
            try:
                self._run_cycle()
            except Exception:  # noqa: BLE001 - one bad cycle must not stop the worker
                logger.exception("%s cycle failed", self.worker_name)
            self._stop.wait(self._interval)
        logger.info("%s stopped", self.worker_name)

    def _run_cycle(self) -> None:
        """Run one stage cycle; subclasses implement this."""
        raise NotImplementedError


class NicheWorker(PollingWorker):
    """Polling worker for classification, prefilter, keyword check, and collection."""

    worker_name = "domain worker"
    thread_name = "domain-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        lexicon: NicheLexicon | None = None,
        llm: NicheLLM | None = None,
        blocklist: Blocklist | None = None,
        prefilter_config: PrefilterConfig | None = None,
        keyword_checker: KeywordChecker | None = None,
        keyword_check_model: str = DEFAULT_KEYWORD_CHECK_MODEL,
        collect_data: bool = False,
        collect_fn: Callable[[str], RunSummary] | None = None,
        collection_limit: int = DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE,
        collection_monthly_units: int = DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
        collection_reserve_units: int = DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
        collection_estimated_units: int = DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS,
        collection_requires_wayback: bool = True,
    ) -> None:
        super().__init__(db_path, interval=interval)
        self._lexicon = lexicon
        self._llm = llm
        self._blocklist = blocklist
        self._prefilter_config = prefilter_config
        self._keyword_checker = keyword_checker
        self._keyword_check_model = keyword_check_model
        self._collect_data = collect_data
        self._collect_fn = collect_fn
        self._collection_limit = collection_limit
        self._collection_monthly_units = collection_monthly_units
        self._collection_reserve_units = collection_reserve_units
        self._collection_estimated_units = collection_estimated_units
        self._collection_requires_wayback = collection_requires_wayback

    def _run_cycle(self) -> None:
        classified = classify_pending(
            self._db_path,
            lexicon=self._lexicon,
            llm=self._llm,
            blocklist=self._blocklist,
        )
        prefilted = prefilter_pending(
            self._db_path,
            config=self._prefilter_config,
        )
        keyword_checked = keyword_check_pending(
            self._db_path,
            checker=self._keyword_checker,
            model=self._keyword_check_model,
            max_batches=KEYWORD_CHECK_BATCHES_PER_CYCLE,
        )
        collected = 0
        if self._collect_data and self._collect_fn is not None:
            collected = collect_pending(
                self._db_path,
                collect=self._collect_fn,
                monthly_cap=self._collection_monthly_units,
                reserve_units=self._collection_reserve_units,
                estimated_units=self._collection_estimated_units,
                limit=self._collection_limit,
                require_wayback=self._collection_requires_wayback,
            )
        if classified or prefilted or keyword_checked or collected:
            logger.info(
                "domain worker classified=%s prefilted=%s keyword_checked=%s collected=%s",
                classified,
                prefilted,
                keyword_checked,
                collected,
            )


class EvaluationWorker(PollingWorker):
    """Polling worker for collected domains that still need an evaluation.

    The worker is deliberately independent of the collection worker: a long
    Ahrefs run cannot hold up evaluation, and a newly collected domain is
    picked up on the next cycle.
    """

    worker_name = "evaluation worker"
    thread_name = "evaluation-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        config: EvaluationConfig | None = None,
        llm: EvidenceReviewLLM | None = None,
        limit: int = DEFAULT_EVALUATION_CHECKS_PER_CYCLE,
    ) -> None:
        super().__init__(db_path, interval=interval)
        self._config = config
        self._llm = llm
        self._limit = limit

    def _run_cycle(self) -> None:
        evaluated = evaluate_pending(
            self._db_path,
            config=self._config or load_evaluation_config(),
            llm=self._llm,
            limit=self._limit,
        )
        if evaluated:
            logger.info("evaluation worker evaluated=%s", evaluated)


class LiveCheckWorker(PollingWorker):
    """Polling worker dedicated to the deterministic Live check stage."""

    worker_name = "live check worker"
    thread_name = "live-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        client: LiveProber | None,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        config: LiveCheckConfig | None = None,
        max_attempts: int = DEFAULT_LIVE_CHECK_MAX_ATTEMPTS,
        limit: int = DEFAULT_LIVE_CHECKS_PER_CYCLE,
        raw_root: Path | None = None,
    ) -> None:
        super().__init__(db_path, interval=interval)
        self._client = client
        self._config = config
        self._max_attempts = max_attempts
        self._limit = limit
        self._raw_root = raw_root

    def _run_cycle(self) -> None:
        checked = live_check_pending(
            self._db_path,
            client=self._client,
            config=self._config or load_live_config(),
            max_attempts=self._max_attempts,
            limit=self._limit,
            raw_root=self._raw_root,
        )
        if checked:
            logger.info("live check worker checked=%s", checked)


class WaybackCheckWorker(PollingWorker):
    """Polling worker dedicated to the Wayback Machine stage."""

    worker_name = "wayback check worker"
    thread_name = "wayback-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        client: WaybackSource | None,
        llm: WaybackLLM | None = None,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        config: WaybackConfig | None = None,
        max_attempts: int = DEFAULT_WAYBACK_CHECK_MAX_ATTEMPTS,
        limit: int = DEFAULT_WAYBACK_CHECKS_PER_CYCLE,
        raw_root: Path | None = None,
    ) -> None:
        super().__init__(db_path, interval=interval)
        self._client = client
        self._llm = llm
        self._config = config
        self._max_attempts = max_attempts
        self._limit = limit
        self._raw_root = raw_root

    def _run_cycle(self) -> None:
        checked = wayback_check_pending(
            self._db_path,
            client=self._client,
            llm=self._llm,
            config=self._config or load_wayback_config(),
            max_attempts=self._max_attempts,
            limit=self._limit,
            raw_root=self._raw_root,
        )
        if checked:
            logger.info("wayback check worker checked=%s", checked)


class FreeDRCheckWorker(PollingWorker):
    """Polling worker dedicated to the free Ahrefs Domain Rating stage."""

    worker_name = "free DR check worker"
    thread_name = "free-dr-worker"

    def __init__(
        self,
        db_path: Path,
        *,
        checker: DomainRatingChecker | None,
        interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        dr_floor: int = DEFAULT_DR_FLOOR,
        max_attempts: int = DEFAULT_DR_CHECK_MAX_ATTEMPTS,
    ) -> None:
        super().__init__(db_path, interval=interval)
        self._checker = checker
        self._dr_floor = dr_floor
        self._max_attempts = max_attempts

    def _run_cycle(self) -> None:
        checked = dr_check_pending(
            self._db_path,
            checker=self._checker,
            dr_floor=self._dr_floor,
            max_attempts=self._max_attempts,
            max_batches=DR_CHECK_BATCHES_PER_CYCLE,
        )
        if checked:
            logger.info("free DR check worker checked=%s", checked)

"""Command-line interface for the expired-domain tooling."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from expired_domains import __version__
from expired_domains.blocklist import block_mapping, load_blocklist, split_blocked
from expired_domains.collection import (
    collect_pending,
    collection_budget,
    load_collection_records,
)
from expired_domains.config import ConfigError, Settings, load_settings
from expired_domains.domain_detail import load_domain_details
from expired_domains.domains import normalize_collected_date, parse_domain_csv
from expired_domains.dr_check import AhrefsDomainRatingChecker, dr_check_pending
from expired_domains.enrich import run_enrich
from expired_domains.evaluation import (
    EvaluationConfig,
    OpenRouterRelevanceLLM,
    evaluate_domains,
    evaluate_pending,
    load_evaluation_config,
    store_evaluation,
)
from expired_domains.keyword_check import OpenRouterKeywordChecker
from expired_domains.live_check import (
    LiveClient,
    live_check_pending,
    load_live_config,
)
from expired_domains.models import Coverage, RequestRecord, RunSummary
from expired_domains.niches import (
    StubNicheClassifier,
    categorize_domains,
    load_niche_lexicon,
)
from expired_domains.shortlist import (
    load_discovery_records,
    load_source_imports,
    write_shortlist_html,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    list_pending_domains,
    list_unblocked_domains,
    set_domain_blocks,
    set_domain_niches,
    store_domain_observations,
    transaction,
)
from expired_domains.wayback import (
    OpenRouterWaybackLLM,
    WaybackClient,
    load_wayback_config,
    wayback_check_pending,
)
from expired_domains.worker import (
    EvaluationWorker,
    FreeDRCheckWorker,
    LiveCheckWorker,
    NicheWorker,
    WaybackCheckWorker,
    classify_pending,
    keyword_check_pending,
    prefilter_pending,
)

HISTORY_QUERY_IDS = ("refdomains_history", "metrics_history")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="expired_domains",
        description="Evaluate expired domains using Ahrefs data.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    enrich = subparsers.add_parser(
        "enrich",
        help="Retrieve the standard-tier Ahrefs dataset for one domain.",
    )
    enrich.add_argument("domain", help="Bare domain to retrieve, e.g. example.com")
    enrich.add_argument(
        "--force",
        action="store_true",
        help="Fetch a new run even when a run already exists for today.",
    )
    enrich.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )

    import_domains = subparsers.add_parser(
        "import-domains",
        help="Import candidate-domain exports into the normalised store.",
    )
    import_domains.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="CSV export(s) to import, e.g. reference/misc/spamzilla.csv",
    )
    import_domains.add_argument(
        "--source",
        default=None,
        help="Override source detection (spamzilla, godaddy_auctions, expireddomains, ...).",
    )
    import_domains.add_argument(
        "--collected-date",
        default=None,
        help="Collection date in YYYY-MM-DD form (defaults to today in UTC).",
    )
    import_domains.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )

    shortlist = subparsers.add_parser(
        "shortlist",
        help="Build the domain-discovery shortlist HTML from imported candidates.",
    )
    shortlist.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )
    shortlist.add_argument(
        "--output",
        type=Path,
        default=None,
        help="HTML output path (defaults to <data>/../output/shortlist.html).",
    )

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Score collected domains and produce a purchase verdict.",
    )
    evaluate.add_argument(
        "domains",
        nargs="*",
        help="Domains to evaluate (defaults to every collected domain).",
    )
    evaluate.add_argument(
        "--all",
        action="store_true",
        help="Evaluate every domain with a successful Ahrefs run.",
    )
    evaluate.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )
    evaluate.add_argument(
        "--json",
        action="store_true",
        help="Print the full evaluation payload as JSON.",
    )
    evaluate.add_argument(
        "--no-store",
        action="store_true",
        help="Do not persist the evaluation on the domains table.",
    )
    evaluate.add_argument(
        "--llm",
        action="store_true",
        help="Use the OpenRouter relevance gate for ambiguous names/topics.",
    )
    evaluate.add_argument(
        "--model",
        default=None,
        help="Override the relevance LLM model.",
    )
    evaluate.add_argument(
        "--advisory-only",
        action="store_true",
        help="Keep cap flags advisory instead of letting them cap the verdict.",
    )

    blocklist = subparsers.add_parser(
        "blocklist",
        help="Mark stored domains that match the deterministic blocklist dictionary.",
    )
    blocklist.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )

    serve = subparsers.add_parser(
        "serve",
        help="Run the FastAPI web app.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    serve.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )
    serve.add_argument(
        "--no-worker",
        action="store_true",
        help="Do not start the automatic pending-niche worker.",
    )

    worker = subparsers.add_parser(
        "worker",
        help="Classify, prefilter, keyword-check, DR-check, Wayback-check, collect, and evaluate.",
    )
    worker.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (defaults to the configured index).",
    )
    worker.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Seconds between pending-domain scans (defaults to the configured interval).",
    )
    worker.add_argument(
        "--once",
        action="store_true",
        help="Process the current pending backlog once and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "enrich":
        return _run_enrich(args, settings)
    if args.command == "import-domains":
        return _run_import_domains(args, settings)
    if args.command == "shortlist":
        return _run_shortlist(args, settings)
    if args.command == "evaluate":
        return _run_evaluate(args, settings)
    if args.command == "blocklist":
        return _run_blocklist(args, settings)
    if args.command == "serve":
        return _run_serve(args, settings)
    if args.command == "worker":
        return _run_worker(args, settings)

    parser.print_help()
    return 0


def _run_enrich(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``enrich`` command."""
    resolved = settings if settings is not None else load_settings()
    try:
        summary = run_enrich(
            args.domain,
            settings=resolved,
            db_path=args.db,
            force=args.force,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print_summary(summary)
    return summary.exit_code


def _run_import_domains(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``import-domains`` command."""
    try:
        collected_date = normalize_collected_date(args.collected_date)
        parsed = [
            parse_domain_csv(
                path,
                source=args.source,
                collected_date=collected_date,
            )
            for path in args.paths
        ]
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    init_db(db_path)
    conn = connect(db_path)
    try:
        with transaction(conn):
            counts = [store_domain_observations(conn, entry.observations) for entry in parsed]
        pending = list_pending_domains(conn)
    finally:
        conn.close()

    allowed, blocked = split_blocked(pending, load_blocklist())
    assignments = categorize_domains(
        allowed,
        load_niche_lexicon(),
        llm=StubNicheClassifier(),
    )

    conn = connect(db_path)
    try:
        with transaction(conn):
            set_domain_blocks(conn, block_mapping(blocked))
            set_domain_niches(
                conn,
                {
                    assignment.domain: (assignment.niche, assignment.source)
                    for assignment in assignments
                },
            )
    finally:
        conn.close()

    for path, entry, count in zip(args.paths, parsed, counts, strict=True):
        unique = len({row.domain for row in entry.observations})
        print(
            f"{path}: source={entry.source} observations={count} "
            f"domains={unique} collected_date={collected_date}"
        )
    print(f"classifier: processed={len(pending)}")
    niche_counts = Counter(assignment.niche for assignment in assignments)
    print(
        "niches: " + " ".join(f"{niche}={count}" for niche, count in sorted(niche_counts.items()))
    )
    print(f"blocklist: blocked={len(blocked)}")
    return 0


def _run_shortlist(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``shortlist`` command."""
    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    if not db_path.is_file():
        print(f"index not found: {db_path}", file=sys.stderr)
        return 2

    conn = connect(db_path)
    try:
        records = load_discovery_records(conn)
        last_imports = load_source_imports(conn)
        collections = load_collection_records(conn)
        details = load_domain_details(conn, collections)
        budget = collection_budget(
            conn,
            monthly_cap=resolved.data_collection_monthly_units,
            reserve=resolved.data_collection_reserve_units,
        )
    finally:
        conn.close()

    output_path = (
        args.output
        if args.output is not None
        else resolved.data_root.parent / "output" / "shortlist.html"
    )
    written = write_shortlist_html(
        output_path,
        records,
        db_path=db_path,
        source_last_import=last_imports,
        collection=collections,
        collection_budget=budget,
        # A standalone HTML file cannot run the worker; show the queue as paused.
        collection_enabled=False,
        wayback_enabled=resolved.wayback_checks_enabled,
        live_enabled=resolved.live_check_enabled,
        details=details,
    )

    sources = len({record.source for record in records if not record.blocked})
    blocked = sum(1 for record in records if record.blocked)
    print(
        f"shortlist: {written.resolve()} domains={len(records) - blocked} "
        f"blocked={blocked} sources={sources}"
    )
    return 0


def _run_evaluate(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``evaluate`` command."""
    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    if not db_path.is_file():
        print(f"index not found: {db_path}", file=sys.stderr)
        return 2

    config = load_evaluation_config()
    if args.advisory_only:
        config = EvaluationConfig(
            version=config.version,
            data={
                **config.data,
                "flag_policy": {
                    **config.section("flag_policy"),
                    "gate_cap_flags": False,
                },
            },
        )

    llm = None
    if args.llm:
        if not resolved.openrouter_key:
            print("OPENROUTER_API_KEY is required for --llm", file=sys.stderr)
            return 2
        llm = OpenRouterRelevanceLLM(
            api_key=resolved.openrouter_key,
            model=args.model or resolved.evaluation_llm_model,
            base_url=resolved.openrouter_base_url,
        )

    init_db(db_path)
    conn = connect(db_path)
    try:
        verdicts = evaluate_domains(
            conn,
            args.domains or None,
            config=config,
            llm=llm,
        )
        if not args.no_store:
            with transaction(conn):
                for verdict in verdicts:
                    store_evaluation(conn, verdict)
    finally:
        conn.close()
        if llm is not None:
            llm.close()

    if args.json:
        print(
            json.dumps(
                [verdict.as_dict() for verdict in verdicts],
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"evaluate: domains={len(verdicts)} config={config.version}")
    for verdict in verdicts:
        flags = [
            signal["key"]
            for signal in verdict.signals
            if signal.get("gate_eligible") and signal["key"] not in {"price_carried_score"}
        ]
        flag_text = f" flags={','.join(flags)}" if flags else ""
        print(
            f"{verdict.domain:<32} {verdict.verdict:<6} "
            f"score={verdict.score:<5.1f} evidence={verdict.evidence_points:<5.1f} "
            f"recommendation={verdict.recommendation}{flag_text}"
        )
    return 0


def _run_blocklist(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``blocklist`` command."""
    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    if not db_path.is_file():
        print(f"index not found: {db_path}", file=sys.stderr)
        return 2

    init_db(db_path)
    conn = connect(db_path)
    try:
        domains = list_unblocked_domains(conn)
    finally:
        conn.close()

    _allowed, blocked = split_blocked(domains, load_blocklist())

    conn = connect(db_path)
    try:
        with transaction(conn):
            updated = set_domain_blocks(conn, block_mapping(blocked))
    finally:
        conn.close()

    counts = Counter(match.category for match in blocked.values())
    print(f"blocklist: scanned={len(domains)} blocked={updated}")
    if counts:
        print(
            "blocked categories: "
            + " ".join(f"{category}={count}" for category, count in sorted(counts.items()))
        )
    return 0


def _run_serve(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``serve`` command with uvicorn."""
    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    if not db_path.is_file():
        print(
            f"warning: index not found: {db_path}; the discovery view will return 503",
            file=sys.stderr,
        )

    import uvicorn

    from expired_domains.web.app import create_app

    app = create_app(
        settings=resolved,
        db_path=db_path,
        start_worker=not args.no_worker,
    )
    print(f"serving Domain Hunter on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _run_worker(args: argparse.Namespace, settings: Settings | None) -> int:
    """Execute the ``worker`` command."""
    resolved = settings if settings is not None else load_settings()
    db_path = args.db if args.db is not None else resolved.db_path

    if not db_path.is_file():
        print(f"index not found: {db_path}", file=sys.stderr)
        return 2

    init_db(db_path)
    keyword_checker = (
        OpenRouterKeywordChecker(
            api_key=resolved.openrouter_key,
            model=resolved.keyword_check_model,
            base_url=resolved.openrouter_base_url,
        )
        if resolved.openrouter_key
        else None
    )
    evaluation_llm = (
        OpenRouterRelevanceLLM(
            api_key=resolved.openrouter_key,
            model=resolved.evaluation_llm_model,
            base_url=resolved.openrouter_base_url,
        )
        if resolved.openrouter_key
        else None
    )
    evaluation_config = load_evaluation_config()
    dr_checker = (
        AhrefsDomainRatingChecker(
            api_key=resolved.ahrefs_free_dr_check,
            base_url=resolved.api_base,
        )
        if resolved.ahrefs_free_dr_check
        else None
    )
    wayback_config = load_wayback_config() if resolved.wayback_checks_enabled else None
    wayback_client = (
        WaybackClient(rate_per_sec=resolved.wayback_rate_per_sec)
        if resolved.wayback_checks_enabled and wayback_config is not None
        else None
    )
    wayback_llm = (
        OpenRouterWaybackLLM(
            api_key=resolved.openrouter_key,
            model=resolved.wayback_llm_model,
            base_url=resolved.openrouter_base_url,
        )
        if resolved.wayback_checks_enabled and resolved.openrouter_key
        else None
    )
    wayback_raw_root = resolved.data_root / "raw" / "wayback"
    live_config = load_live_config() if resolved.live_check_enabled else None
    live_client = (
        LiveClient(
            timeout_seconds=resolved.live_check_timeout_seconds,
            max_redirects=resolved.live_check_max_redirects,
            max_body_bytes=resolved.live_check_max_body_bytes,
        )
        if resolved.live_check_enabled and live_config is not None
        else None
    )
    live_raw_root = resolved.data_root / "raw" / "live"

    collect_fn = None
    if resolved.data_collection_enabled:
        if not resolved.ahrefs_key:
            print(
                "warning: DATA_COLLECTION_ENABLED is set but AHREFS_KEY is missing; "
                "data collection stays off",
                file=sys.stderr,
            )
        else:

            def _collect(domain: str) -> RunSummary:
                return run_enrich(domain, settings=resolved, db_path=db_path)

            collect_fn = _collect
            if dr_checker is None:
                print(
                    "warning: data collection is enabled but AHREFS_FREE_DR_CHECK is "
                    "missing; no domain can pass the DR check",
                    file=sys.stderr,
                )

    if args.once:
        try:
            classified = classify_pending(db_path)
            prefilted = prefilter_pending(db_path)
            keywords = keyword_check_pending(
                db_path,
                checker=keyword_checker,
                model=resolved.keyword_check_model,
            )
            dr_checked = dr_check_pending(
                db_path,
                checker=dr_checker,
                dr_floor=resolved.dr_floor,
                max_attempts=resolved.dr_check_max_attempts,
            )
            live_checked = live_check_pending(
                db_path,
                client=live_client,
                config=live_config,
                max_attempts=resolved.live_check_max_attempts,
                limit=resolved.live_checks_per_cycle,
                raw_root=live_raw_root,
            )
            wayback_checked = wayback_check_pending(
                db_path,
                client=wayback_client,
                llm=wayback_llm,
                config=wayback_config,
                max_attempts=resolved.wayback_check_max_attempts,
                limit=resolved.wayback_checks_per_cycle,
                raw_root=wayback_raw_root,
                require_live=resolved.live_check_enabled,
            )
            collected = (
                collect_pending(
                    db_path,
                    collect=collect_fn,
                    monthly_cap=resolved.data_collection_monthly_units,
                    reserve_units=resolved.data_collection_reserve_units,
                    estimated_units=resolved.data_collection_estimated_units,
                    limit=resolved.data_collection_max_per_cycle,
                    require_wayback=resolved.wayback_checks_enabled,
                )
                if collect_fn is not None
                else 0
            )
            evaluated = evaluate_pending(
                db_path,
                config=evaluation_config,
                llm=evaluation_llm,
                limit=resolved.evaluation_checks_per_cycle,
            )
        finally:
            if keyword_checker is not None:
                keyword_checker.close()
            if evaluation_llm is not None:
                evaluation_llm.close()
            if dr_checker is not None:
                dr_checker.close()
            if wayback_client is not None:
                wayback_client.close()
            if wayback_llm is not None:
                wayback_llm.close()
            if live_client is not None:
                live_client.close()
        print(
            f"worker: classified={classified} prefilted={prefilted} "
            f"keyword_checked={keywords} dr_checked={dr_checked} "
            f"live_checked={live_checked} wayback_checked={wayback_checked} "
            f"collected={collected} evaluated={evaluated}"
        )
        return 0

    interval = args.interval if args.interval is not None else resolved.niche_worker_interval
    worker = NicheWorker(
        db_path,
        interval=interval,
        keyword_checker=keyword_checker,
        keyword_check_model=resolved.keyword_check_model,
        collect_data=collect_fn is not None,
        collect_fn=collect_fn,
        collection_limit=resolved.data_collection_max_per_cycle,
        collection_monthly_units=resolved.data_collection_monthly_units,
        collection_reserve_units=resolved.data_collection_reserve_units,
        collection_estimated_units=resolved.data_collection_estimated_units,
        collection_requires_wayback=resolved.wayback_checks_enabled,
    )
    dr_worker = (
        FreeDRCheckWorker(
            db_path,
            checker=dr_checker,
            interval=interval,
            dr_floor=resolved.dr_floor,
            max_attempts=resolved.dr_check_max_attempts,
        )
        if dr_checker is not None
        else None
    )
    wayback_worker = (
        WaybackCheckWorker(
            db_path,
            client=wayback_client,
            llm=wayback_llm,
            interval=interval,
            config=wayback_config,
            max_attempts=resolved.wayback_check_max_attempts,
            limit=resolved.wayback_checks_per_cycle,
            raw_root=wayback_raw_root,
        )
        if wayback_client is not None
        else None
    )
    live_worker = (
        LiveCheckWorker(
            db_path,
            client=live_client,
            interval=interval,
            config=live_config,
            max_attempts=resolved.live_check_max_attempts,
            limit=resolved.live_checks_per_cycle,
            raw_root=live_raw_root,
        )
        if live_client is not None
        else None
    )
    evaluation_worker = EvaluationWorker(
        db_path,
        interval=interval,
        config=evaluation_config,
        llm=evaluation_llm,
        limit=resolved.evaluation_checks_per_cycle,
    )
    worker.start()
    if dr_worker is not None:
        dr_worker.start()
    if live_worker is not None:
        live_worker.start()
    if wayback_worker is not None:
        wayback_worker.start()
    evaluation_worker.start()
    print(
        f"worker: watching {db_path} every {interval:g}s",
        flush=True,
    )
    try:
        while (
            worker.is_alive()
            or (dr_worker is not None and dr_worker.is_alive())
            or (live_worker is not None and live_worker.is_alive())
            or (wayback_worker is not None and wayback_worker.is_alive())
            or evaluation_worker.is_alive()
        ):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        if dr_worker is not None:
            dr_worker.stop()
        if live_worker is not None:
            live_worker.stop()
        if wayback_worker is not None:
            wayback_worker.stop()
        evaluation_worker.stop()
        if keyword_checker is not None:
            keyword_checker.close()
        if evaluation_llm is not None:
            evaluation_llm.close()
        if dr_checker is not None:
            dr_checker.close()
        if wayback_client is not None:
            wayback_client.close()
        if wayback_llm is not None:
            wayback_llm.close()
        if live_client is not None:
            live_client.close()
    return 0


def _print_summary(summary: RunSummary) -> None:
    """Print one line per request followed by the run summary."""
    if summary.message:
        print(summary.message)
        return

    for record in summary.requests:
        units = record.units
        line = (
            f"{record.query_id}: status={record.status} rows={record.rows} "
            f"units_actual={units.actual}"
        )
        if units.billed != units.actual:
            line += f" units_billed={units.billed}"
        print(line)

    print(f"run_id: {summary.run_id}")
    print(f"raw_dir: {summary.raw_dir}")
    print(f"coverage: {_coverage_line(summary.coverage)}")
    print(f"history: {_history_line(summary.requests)}")
    print(f"backlinks: {_backlinks_line(summary.coverage)}")
    print(f"units: actual={summary.units.actual} billed={summary.units.billed}")
    if summary.error:
        failures = ", ".join(
            f"{query_id}: {message}" for query_id, message in summary.error.items()
        )
        print(f"failures: {failures}", file=sys.stderr)


def _history_line(requests: Sequence[RequestRecord]) -> str:
    """Render the reported history row counts, showing zero rows explicitly."""
    counts = dict.fromkeys(HISTORY_QUERY_IDS, 0)
    for record in requests:
        if record.query_id in counts:
            counts[record.query_id] = _row_count(record.rows)
    return " ".join(f"{query_id}={count}" for query_id, count in counts.items())


def _backlinks_line(coverage: Coverage) -> str:
    """Render the deduplicated backlink counts for the run summary."""
    return (
        f"top_dr={coverage.rows['top_dr']} latest={coverage.rows['latest']} "
        f"unique={coverage.rows['unique']}"
    )


def _row_count(value: str | None) -> int:
    """Convert an ``x-api-rows`` value to an integer, treating bad values as zero."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _coverage_line(coverage: Coverage) -> str:
    """Render the coverage flags required by the run summary."""
    stats = " ".join(f"{key}:{value}" for key, value in coverage.stats.items())
    rows = " ".join(f"{key}:{value}" for key, value in coverage.rows.items())
    return (
        f"stats={stats} rows={rows} top_anchors={coverage.top_anchors} "
        f"lexicon_complete={str(coverage.lexicon_complete).lower()} "
        f"history_months={coverage.history_months} "
        f"drop_date_source={coverage.drop_date_source}"
    )

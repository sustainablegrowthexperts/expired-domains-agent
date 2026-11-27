"""FastAPI application for the domain-discovery shortlist."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from expired_domains import __version__
from expired_domains.collection import (
    CollectionBudget,
    CollectionRecord,
    collection_budget,
    load_collection_records,
)
from expired_domains.config import Settings, load_settings
from expired_domains.domain_detail import load_domain_detail, load_domain_details
from expired_domains.dr_check import AhrefsDomainRatingChecker
from expired_domains.enrich import run_enrich
from expired_domains.evaluation import (
    OpenRouterRelevanceLLM,
    evaluate_domain,
    load_evaluation_config,
    load_stored_evaluation,
)
from expired_domains.keyword_check import OpenRouterKeywordChecker
from expired_domains.live_check import (
    LiveClient,
    load_live_config,
)
from expired_domains.shortlist import (
    DiscoveryRecord,
    build_page_data,
    load_discovery_records,
    load_source_imports,
    render_shortlist_html,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    set_live_check_manual_pass,
    set_prefilter_manual_pass,
    set_wayback_manual_pass,
    transaction,
)
from expired_domains.wayback import (
    OpenRouterWaybackLLM,
    WaybackClient,
    load_wayback_config,
)
from expired_domains.worker import (
    EvaluationWorker,
    FreeDRCheckWorker,
    LiveCheckWorker,
    NicheWorker,
    WaybackCheckWorker,
)


def create_app(
    *,
    settings: Settings | None = None,
    db_path: Path | str | None = None,
    start_worker: bool = True,
) -> FastAPI:
    """Build the web app, optionally against an explicit settings/index."""
    resolved = settings if settings is not None else load_settings()
    index_path = Path(db_path) if db_path is not None else resolved.db_path

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        worker: NicheWorker | None = None
        dr_worker: FreeDRCheckWorker | None = None
        live_worker: LiveCheckWorker | None = None
        wayback_worker: WaybackCheckWorker | None = None
        evaluation_worker: EvaluationWorker | None = None
        keyword_checker: OpenRouterKeywordChecker | None = None
        dr_checker: AhrefsDomainRatingChecker | None = None
        live_client: LiveClient | None = None
        wayback_client: WaybackClient | None = None
        wayback_llm: OpenRouterWaybackLLM | None = None
        evaluation_llm: OpenRouterRelevanceLLM | None = None
        collect_fn = None
        if start_worker and index_path.is_file():
            if resolved.openrouter_key:
                keyword_checker = OpenRouterKeywordChecker(
                    api_key=resolved.openrouter_key,
                    model=resolved.keyword_check_model,
                    base_url=resolved.openrouter_base_url,
                )
                evaluation_llm = OpenRouterRelevanceLLM(
                    api_key=resolved.openrouter_key,
                    model=resolved.evaluation_llm_model,
                    base_url=resolved.openrouter_base_url,
                )
            if resolved.ahrefs_free_dr_check:
                dr_checker = AhrefsDomainRatingChecker(
                    api_key=resolved.ahrefs_free_dr_check,
                    base_url=resolved.api_base,
                )
            if resolved.wayback_checks_enabled:
                wayback_config = load_wayback_config()
                wayback_client = WaybackClient(rate_per_sec=resolved.wayback_rate_per_sec)
                if resolved.openrouter_key:
                    wayback_llm = OpenRouterWaybackLLM(
                        api_key=resolved.openrouter_key,
                        model=resolved.wayback_llm_model,
                        base_url=resolved.openrouter_base_url,
                    )
            live_config = None
            if resolved.live_check_enabled:
                live_config = load_live_config()
                live_client = LiveClient(
                    timeout_seconds=resolved.live_check_timeout_seconds,
                    max_redirects=resolved.live_check_max_redirects,
                    max_body_bytes=resolved.live_check_max_body_bytes,
                )
            if resolved.data_collection_enabled and resolved.ahrefs_key:

                def _collect(domain: str):
                    return run_enrich(domain, settings=resolved, db_path=index_path)

                collect_fn = _collect

            worker = NicheWorker(
                index_path,
                interval=resolved.niche_worker_interval,
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
            worker.start()
            if dr_checker is not None:
                dr_worker = FreeDRCheckWorker(
                    index_path,
                    checker=dr_checker,
                    interval=resolved.niche_worker_interval,
                    dr_floor=resolved.dr_floor,
                    max_attempts=resolved.dr_check_max_attempts,
                )
                dr_worker.start()
            if wayback_client is not None:
                wayback_worker = WaybackCheckWorker(
                    index_path,
                    client=wayback_client,
                    llm=wayback_llm,
                    interval=resolved.niche_worker_interval,
                    config=wayback_config,
                    max_attempts=resolved.wayback_check_max_attempts,
                    limit=resolved.wayback_checks_per_cycle,
                    raw_root=resolved.data_root / "raw" / "wayback",
                )
                wayback_worker.start()
            if live_client is not None:
                live_worker = LiveCheckWorker(
                    index_path,
                    client=live_client,
                    interval=resolved.niche_worker_interval,
                    config=live_config,
                    max_attempts=resolved.live_check_max_attempts,
                    limit=resolved.live_checks_per_cycle,
                    raw_root=resolved.data_root / "raw" / "live",
                )
                live_worker.start()
            evaluation_worker = EvaluationWorker(
                index_path,
                interval=resolved.niche_worker_interval,
                config=load_evaluation_config(),
                llm=evaluation_llm,
                limit=resolved.evaluation_checks_per_cycle,
            )
            evaluation_worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            if dr_worker is not None:
                dr_worker.stop()
            if wayback_worker is not None:
                wayback_worker.stop()
            if live_worker is not None:
                live_worker.stop()
            if evaluation_worker is not None:
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

    app = FastAPI(
        title="Domain Hunter",
        version=__version__,
        description="Discovery and prefilter views over the imported domain-observation index.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.db_path = index_path

    def discovery_data() -> tuple[
        list[DiscoveryRecord],
        dict[str, str],
        dict[str, CollectionRecord],
        CollectionBudget,
        dict[str, dict[str, Any]],
    ]:
        return _load_discovery(index_path, resolved)

    def shortlist_page() -> HTMLResponse:
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")
        records, last_imports, collections, budget, details = discovery_data()
        return HTMLResponse(
            render_shortlist_html(
                records,
                db_path=index_path,
                source_last_import=last_imports,
                writable=True,
                collection=collections,
                collection_budget=budget,
                collection_enabled=(
                    resolved.data_collection_enabled and start_worker and bool(resolved.ahrefs_key)
                ),
                wayback_enabled=resolved.wayback_checks_enabled,
                live_enabled=resolved.live_check_enabled,
                details=details,
            )
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        """Serve the discovery view at the root for convenience."""
        return shortlist_page()

    @app.get("/shortlist", response_class=HTMLResponse)
    def shortlist_view() -> HTMLResponse:
        """Render the domain-discovery shortlist."""
        return shortlist_page()

    @app.get("/api/discovery")
    def discovery_api(
        source: str | None = Query(default=None, description="Filter by source slug"),
        q: str | None = Query(default=None, description="Case-insensitive domain search"),
    ) -> dict[str, Any]:
        """Return the discovery rows and metadata as JSON."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        records, last_imports, collections, budget, _details = discovery_data()
        rows, meta = build_page_data(
            records,
            db_path=index_path,
            source_last_import=last_imports,
            writable=True,
            collection=collections,
            collection_budget=budget,
            collection_enabled=(
                resolved.data_collection_enabled and start_worker and bool(resolved.ahrefs_key)
            ),
            wayback_enabled=resolved.wayback_checks_enabled,
            live_enabled=resolved.live_check_enabled,
        )
        blocked_rows = [row for row in rows if row["blocked"]]
        rows = [row for row in rows if not row["blocked"]]
        if source:
            rows = [row for row in rows if row["source"] == source]
            blocked_rows = [row for row in blocked_rows if row["source"] == source]
        if q:
            needle = q.strip().lower()
            rows = [row for row in rows if needle in row["domain"]]
            blocked_rows = [row for row in blocked_rows if needle in row["domain"]]
        return {
            "rows": rows,
            "blocked_rows": blocked_rows,
            "meta": meta,
            "count": len(rows),
            "blocked_count": len(blocked_rows),
        }

    @app.get("/api/domains/{domain}")
    def domain_detail(domain: str) -> dict[str, Any]:
        """Return the stored Ahrefs evidence for one domain."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        try:
            conn = connect(index_path)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not open index: {exc}") from exc
        try:
            detail = load_domain_detail(conn, domain)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not read index: {exc}") from exc
        finally:
            conn.close()

        if detail is None:
            raise HTTPException(status_code=404, detail=f"domain not found: {domain}")
        return detail

    @app.get("/api/evaluation/{domain}")
    def domain_evaluation(domain: str, refresh: bool = False) -> dict[str, Any]:
        """Return the deterministic purchase evaluation for one domain."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        init_db(index_path)
        conn = connect(index_path)
        try:
            if not refresh:
                stored = load_stored_evaluation(conn, domain)
                if stored is not None:
                    return stored
            verdict = evaluate_domain(
                conn,
                domain,
                config=load_evaluation_config(),
            )
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not read index: {exc}") from exc
        finally:
            conn.close()

        if verdict is None:
            raise HTTPException(status_code=404, detail=f"domain not found: {domain}")
        return verdict.as_dict()

    @app.post("/api/prefilter/{domain}/pass")
    def pass_prefilter(domain: str) -> dict[str, Any]:
        """Manually pass one failed prefilter result."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        init_db(index_path)
        conn = connect(index_path)
        try:
            with transaction(conn):
                updated = set_prefilter_manual_pass(conn, domain)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not update index: {exc}") from exc
        finally:
            conn.close()

        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"domain not found or not a failed prefilter: {domain}",
            )
        return {"domain": domain, "prefilter_pass": True, "manual": True}

    @app.post("/api/live/{domain}/pass")
    def pass_live(domain: str) -> dict[str, Any]:
        """Manually pass one failed Live check result."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        init_db(index_path)
        conn = connect(index_path)
        try:
            with transaction(conn):
                updated = set_live_check_manual_pass(conn, domain)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not update index: {exc}") from exc
        finally:
            conn.close()

        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"domain not found or not a failed Live check: {domain}",
            )
        return {"domain": domain, "live_check_pass": True, "manual": True}

    @app.post("/api/wayback/{domain}/pass")
    def pass_wayback(domain: str) -> dict[str, Any]:
        """Manually pass one failed Wayback result."""
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail=f"index not found: {index_path}")

        init_db(index_path)
        conn = connect(index_path)
        try:
            with transaction(conn):
                updated = set_wayback_manual_pass(conn, domain)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=500, detail=f"could not update index: {exc}") from exc
        finally:
            conn.close()

        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"domain not found or not a failed Wayback result: {domain}",
            )
        return {"domain": domain, "wayback_check_pass": True, "manual": True}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Report app health without touching the index."""
        return {"status": "ok", "db_path": str(index_path), "db_exists": index_path.is_file()}

    return app


def _load_discovery(
    db_path: Path, settings: Settings
) -> tuple[
    list[DiscoveryRecord],
    dict[str, str],
    dict[str, CollectionRecord],
    CollectionBudget,
    dict[str, dict[str, Any]],
]:
    """Read discovery rows, imports, collection state, and detail payloads."""
    try:
        conn = connect(db_path)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"could not open index: {exc}") from exc

    try:
        collections = load_collection_records(conn)
        return (
            load_discovery_records(conn),
            load_source_imports(conn),
            collections,
            collection_budget(
                conn,
                monthly_cap=settings.data_collection_monthly_units,
                reserve=settings.data_collection_reserve_units,
            ),
            load_domain_details(conn, collections),
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"could not read index: {exc}") from exc
    finally:
        conn.close()


app = create_app()

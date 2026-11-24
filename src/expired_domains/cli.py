"""Command-line interface for the expired-domain tooling."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from expired_domains import __version__
from expired_domains.config import ConfigError, Settings, load_settings
from expired_domains.enrich import run_enrich
from expired_domains.models import Coverage, RequestRecord, RunSummary

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
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "enrich":
        parser.print_help()
        return 0

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

# Change Summary Specification — Project Scaffolding & Reference Relocation

**Date:** 2026-11-24
**Slug:** `project-scaffolding`
**Parent:** none (first spec)
**Followed by:** `specs/2026-11-24-evaluation-data-retrieval.md`

---

## 1. Objective & Overview

Create the repository foundation for the expired-domain evaluation app — a Python 3.12 `src/` package with a working CLI stub, configuration loading, a test harness, and a documented layout — and relocate the existing exploratory artifacts (requirements doc, evaluation reports, legacy scripts, raw Ahrefs runs, workbooks) into stable `docs/`, `reference/`, and `tests/fixtures/` locations. No Ahrefs calls, no database schema, and no business logic are added here. After this one-shot task, `uv run pytest` and `uv run python -m expired_domains --help` must both work, and every later task can assume this layout.

---

## 2. Scope Boundaries

### In Scope

| Item | Detail |
|---|---|
| Project initialisation | `pyproject.toml` (uv-managed, Python 3.12), src layout, dev tooling config (pytest, ruff). |
| Package skeleton | `src/expired_domains/` importable package with `__init__.py` (version), `config.py`, `cli.py`, `__main__.py`. |
| CLI stub | `python -m expired_domains --help` and `--version` only; no domain logic. |
| Configuration loader | Reads `AHREFS_KEY` and path settings from environment, falling back to `.env`; never logs secret values. |
| Secrets template | `.env.example` with placeholder values and comments. Real `.env` is left untouched. |
| Test harness | pytest configured; one smoke test proving import, version, config parsing, and CLI help. |
| Reference relocation | Move requirements, reports, legacy scripts, raw runs, and workbooks into the target layout below. |
| Cleanup | Delete `__pycache__/` and `*:Zone.Identifier` files. |
| Documentation | `README.md` describing layout, setup, and the spec → task workflow. |

### Out of Scope

| Item | Reason |
|---|---|
| Ahrefs client, queries, storage, SQLite schema | Delivered by `specs/2026-11-24-evaluation-data-retrieval.md`. |
| Evaluation logic, scoring, verdicts | Deferred; not designed yet. |
| Web UI | Deferred; user does not need UI now. |
| LangGraph / agents | Explicitly dropped for this milestone. |
| DataForSEO integration | Not part of this milestone. |
| Deleting the v1/v2 requirement documents | Keep as historical context under `docs/archive/`. |
| Rotating the exposed API keys | Manual user action; note it in the README only. |
| Moving `.env`, `.venv/`, `.git/`, `.opencode/`, `opencode.json`, `task_runner.py` | These stay where they are; `.env` and `.venv` are gitignored state, the rest are active tooling. |
| Rewriting git history | Out of scope; use `git mv` to preserve file history. |

---

## 3. Technical Architecture & Decisions

| Decision | Rationale |
|---|---|
| Python 3.12 with uv | The existing `.venv` is uv-style (`.venv/.lock`); keep one toolchain. |
| `src/` layout, package `expired_domains` | Importable, testable without shipping tests, room for a later API/web layer. |
| Runtime dependency: `python-dotenv` only | Config loading is the only need in this scaffold; `httpx` and `tldextract` are added by the follow-up spec. |
| Dev dependencies: `pytest`, `ruff` | Minimal harness plus lint/format. |
| CLI via stdlib `argparse` in `cli.py` | No framework dependency; web layer comes later. |
| Config loads env vars, `.env` fallback | Matches current `.env` practice and keeps secrets out of code. |
| `docs/reference/` for authoritative docs, `docs/archive/` for superseded ones | v3 is the requirements doc; v1/v2 remain readable but are not authoritative. |
| `reference/legacy/` for old scripts | User decision: scripts are endpoint reference only, to be replaced by the new implementation. Read-only. |
| `tests/fixtures/runs/` for the existing raw runs | They are the zero-cost test corpus (5 domains, 7 runs). Gitignored and local-only; tests skip when absent. |
| `reference/output/` for existing workbooks | Keeps generated artifacts out of the project root. |
| `data/` reserved and gitignored | Runtime raw payloads and SQLite index will live here from the next spec. |
| Keep `task_runner.py` at project root | It is active workflow tooling referenced by the spec → task process. |
| Commit `.env.example`, never `.env` | Secrets stay local; template documents required variables. |
| Move raw runs without editing manifests | Verified: manifests and payloads contain no absolute paths (only `run_id`, API URLs, and params), so relocation is safe. |
| Delete Windows `*:Zone.Identifier` and `__pycache__/` | Junk, not project data. |

### Target Layout

```text
.
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── .env                      # untouched, gitignored
├── opencode.json             # untouched
├── task_runner.py            # untouched
├── .opencode/                # untouched
├── docs/
│   ├── reference/
│   │   ├── ahrefs-ingestion-spec-v3.md
│   │   ├── ahrefs-spec-evaluation-report.md
│   │   └── smoke-fire-evaluation-report.md
│   └── archive/
│       ├── ahrefs-ingestion-spec.md
│       └── ahrefs-ingestion-spec-v2.md
├── reference/
│   ├── legacy/
│   │   ├── ahrefs_ingest.py
│   │   ├── build_workbooks.py
│   │   ├── build_summary_workbook.py
│   │   └── complete_lexicon.py
│   └── output/
│       ├── _all-sites-summary.xlsx
│       ├── 123eft.com.xlsx
│       ├── abipr.com.xlsx
│       ├── kanuksheepskinproducts.com.xlsx
│       ├── longbeachcoffee.com.xlsx
│       ├── smoke-fire.com.xlsx
│       └── build_summary.json
├── specs/
│   ├── 2026-11-24-project-scaffolding.md
│   └── 2026-11-24-evaluation-data-retrieval.md
├── src/
│   └── expired_domains/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       └── config.py
└── tests/
    ├── fixtures/
    │   └── runs/             # moved from ./runs/, gitignored
    │       ├── ingest_log.txt
    │       ├── 123eft.com/
    │       ├── abipr.com/
    │       ├── kanuksheepskinproducts.com/
    │       ├── longbeachcoffee.com/
    │       └── smoke-fire.com/
    └── test_smoke.py
```

### Configuration Contract

`config.py` exposes a `load_settings()` returning a frozen dataclass with at least:

| Field | Source | Default |
|---|---|---|
| `ahrefs_key` | `AHREFS_KEY` env var, then `.env` | `None` (callers fail fast with a clear message) |
| `data_root` | optional env / constant | `<project_root>/data` |
| `db_path` | optional env | `<project_root>/data/index.sqlite3` |
| `raw_root` | optional env | `<project_root>/data/raw/ahrefs` |

Project root is resolved from `Path(__file__).resolve().parents[2]`. The loader must not print or log values.

### `.env.example` Contents

Placeholders only, with comments:

```dotenv
# Required for Ahrefs retrieval (next milestone). Get this from your Ahrefs account.
AHREFS_KEY=

# Reserved for future cross-check integration; not used in the current milestone.
DATAFORSEO_EMAIL=
DATAFORSEO_API_PASSWORD=

# Reserved for future agent/LLM work; not used in the current milestone.
OPENROUTER_API_KEY=
```

---

## 4. Affected Files

| File | Action |
|---|---|
| `pyproject.toml` | Create — project metadata, `python-dotenv`, dev deps, pytest/ruff config, hatchling build backend. |
| `uv.lock` | Create — generated by `uv sync`. |
| `README.md` | Create — layout, setup, required env vars, spec/task workflow, key-rotation note. |
| `.env.example` | Create — template above. |
| `src/expired_domains/__init__.py` | Create — `__version__ = "0.1.0"`. |
| `src/expired_domains/__main__.py` | Create — delegates to `cli.main()`. |
| `src/expired_domains/cli.py` | Create — argparse stub with `--help` and `--version`. |
| `src/expired_domains/config.py` | Create — settings dataclass + `.env` fallback loader. |
| `tests/test_smoke.py` | Create — import, version, config parsing (monkeypatched env), CLI `--help` exit 0. |
| `.gitignore` | Modify — add `data/`, `reference/output/`, `tests/fixtures/runs/`, `*.sqlite3*`; keep existing entries. |
| `ahrefs-ingestion-spec-v3.md` | Move → `docs/reference/ahrefs-ingestion-spec-v3.md` — authoritative requirements. |
| `ahrefs-spec-evaluation-report.md` | Move → `docs/reference/ahrefs-spec-evaluation-report.md` — diagnostic input. |
| `smoke-fire-evaluation-report.md` | Move → `docs/reference/smoke-fire-evaluation-report.md` — diagnostic input. |
| `ahrefs-ingestion-spec.md` | Move → `docs/archive/ahrefs-ingestion-spec.md` — superseded. |
| `ahrefs-ingestion-spec-v2.md` | Move → `docs/archive/ahrefs-ingestion-spec-v2.md` — superseded. |
| `ahrefs_ingest.py` | Move → `reference/legacy/ahrefs_ingest.py` — endpoint reference only. |
| `build_workbooks.py` | Move → `reference/legacy/build_workbooks.py` — endpoint reference only. |
| `build_summary_workbook.py` | Move → `reference/legacy/build_summary_workbook.py` — endpoint reference only. |
| `complete_lexicon.py` | Move → `reference/legacy/complete_lexicon.py` — endpoint reference only. |
| `runs/` | Move → `tests/fixtures/runs/` — test corpus; 5 domains, 7 run dirs. |
| `output/` | Move → `reference/output/` — archived XLSX artifacts. |
| `ahrefs-ingestion-spec-v2.md:Zone.Identifier` | Delete — Windows junk. |
| `ahrefs-ingestion-spec-v3.md:Zone.Identifier` | Delete — Windows junk. |
| `__pycache__/` | Delete — generated junk. |

Do **not** create or modify `.env`. Do not commit `data/`, `reference/output/`, or `tests/fixtures/runs/`.

---

## 5. Proposed Task Breakdown

**Acceptance for this spec:** `uv run pytest` passes; `uv run python -m expired_domains --help` and `--version` exit 0; the five domains and seven run directories are present under `tests/fixtures/runs/`; no file remains at the old `runs/` or `output/` locations; `.env` is unchanged; no Ahrefs API calls are made.

### Phase 1 — Project Scaffold

1. **Create `pyproject.toml` and lockfile** — Define project metadata, Python 3.12, runtime dep `python-dotenv`, dev deps `pytest` + `ruff`, hatchling src build, pytest `testpaths=["tests"]`, ruff defaults; run `uv sync` and confirm the lockfile is generated.
2. **Create package skeleton and CLI stub** — Add `src/expired_domains/{__init__,__main__,cli,config}.py`; `--help` lists commands (none yet) and `--version` prints `0.1.0`.
3. **Implement config loader** — `load_settings()` resolves the project root, loads env vars with `.env` fallback via `python-dotenv`, returns a frozen dataclass with `ahrefs_key`, `data_root`, `db_path`, `raw_root`; never prints values.
4. **Create `.env.example` and smoke tests** — Add the template above and `tests/test_smoke.py` covering import, version, env parsing via monkeypatch, and CLI `--help` exit 0.

### Phase 2 — Relocate Reference Material

5. **Create directory structure and move docs** — `mkdir -p docs/reference docs/archive reference/legacy reference/output`; `git mv` the v3 spec and both reports into `docs/reference/`, v1/v2 into `docs/archive/`.
6. **Move legacy scripts** — `git mv` the four scripts into `reference/legacy/`; leave them unmodified.
7. **Move raw runs** — `git mv runs tests/fixtures/runs` (if git ignores the content, use a plain move); verify exactly 5 domain directories, 7 run directories total, 3 of them under `smoke-fire.com`, and that `ingest_log.txt` moved with them.
8. **Move archived workbooks** — `git mv output reference/output`; verify all 6 XLSX files plus `build_summary.json` are present.
9. **Delete junk files** — Remove `__pycache__/` and both `*:Zone.Identifier` files.

### Phase 3 — Documentation & Verification

10. **Update `.gitignore`** — Add the new ignored paths; ensure `uv.lock`, `pyproject.toml`, `docs/`, `reference/legacy/`, `specs/`, and `src/` are **not** ignored.
11. **Write `README.md`** — Document the target layout, `uv sync` setup, `.env.example` → `.env` copy step, required variables, the spec → `.tasks` → `task_runner.py` workflow, that `reference/legacy/` is read-only reference, and a note to rotate API keys that were previously shared in plaintext.
12. **Run final verification** — `uv run pytest`, `uv run python -m expired_domains --version`, `uv run python -m expired_domains --help`; confirm no old `runs/`/`output/` paths remain; `git status` shows only intended additions/moves.

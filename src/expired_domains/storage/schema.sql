-- First vertical slice: runs, request audit trail, and stats snapshots.
-- Later slices add history, backlinks, and anchors tables.

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    domain            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    tier              TEXT NOT NULL,
    prefilter_batch   TEXT,
    drop_date         TEXT,
    drop_date_source  TEXT,
    fetched_at        TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    coverage_json     TEXT NOT NULL,
    units_actual      INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    run_id        TEXT NOT NULL,
    query_id      TEXT NOT NULL,
    page          INTEGER NOT NULL,
    endpoint      TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    row_count     INTEGER,
    units_actual  INTEGER NOT NULL DEFAULT 0,
    raw_path      TEXT,
    status        INTEGER,
    error         TEXT,
    PRIMARY KEY (run_id, query_id, page),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE TABLE IF NOT EXISTS stats (
    run_id     TEXT NOT NULL,
    snapshot   TEXT NOT NULL,
    links      INTEGER,
    refdomains INTEGER,
    PRIMARY KEY (run_id, snapshot),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

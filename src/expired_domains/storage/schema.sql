-- Implemented vertical slices: runs, request audit trail, stats snapshots,
-- monthly history series, deduplicated backlinks, and classified anchors.

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

CREATE TABLE IF NOT EXISTS history (
    run_id  TEXT NOT NULL,
    series  TEXT NOT NULL,
    date    TEXT NOT NULL,
    value   INTEGER,
    PRIMARY KEY (run_id, series, date),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE TABLE IF NOT EXISTS backlinks (
    run_id            TEXT NOT NULL,
    link_hash         TEXT NOT NULL,
    source_queries    TEXT NOT NULL,
    url_from          TEXT,
    url_to            TEXT,
    domain_from       TEXT,
    root_domain_from  TEXT,
    anchor_raw        TEXT,
    anchor_norm       TEXT,
    is_dofollow       INTEGER,
    domain_rating     REAL,
    first_seen_link   TEXT,
    era               TEXT,
    raw_json          TEXT NOT NULL,
    PRIMARY KEY (run_id, link_hash),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE TABLE IF NOT EXISTS anchors (
    run_id             TEXT NOT NULL,
    source_query       TEXT NOT NULL,
    anchor_raw         TEXT,
    anchor_norm        TEXT,
    links_to_target    INTEGER,
    dofollow_links     INTEGER,
    refdomains         INTEGER,
    first_seen         TEXT,
    last_seen          TEXT,
    lexicon_terms      TEXT NOT NULL,
    lexicon_categories TEXT NOT NULL,
    raw_json           TEXT NOT NULL,
    PRIMARY KEY (run_id, source_query, anchor_norm),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

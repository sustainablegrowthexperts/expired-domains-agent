-- Implemented vertical slices: runs, request audit trail, stats snapshots,
-- monthly history series, deduplicated backlinks, and classified anchors.
-- Candidate domains are stored separately: one canonical row per registrable
-- domain, plus one observation per (domain, source, collection date). The
-- canonical row also carries the niche, blocklist, and local-prefilter verdicts.

CREATE TABLE IF NOT EXISTS domains (
    domain           TEXT PRIMARY KEY,
    first_seen_date  TEXT NOT NULL,
    last_seen_date   TEXT NOT NULL,
    niche            TEXT,
    niche_source     TEXT,
    blocked_category TEXT,
    blocked_terms_json TEXT,
    prefilter_pass   INTEGER,
    prefilter_reason TEXT,
    prefilter_details_json TEXT,
    prefilter_config_version TEXT,
    prefilter_checked_at TEXT,
    prefilter_manual_pass INTEGER NOT NULL DEFAULT 0,
    prefilter_manual_pass_at TEXT,
    keyword_check_status TEXT,
    keyword_check_pass INTEGER,
    keyword_check_reason TEXT,
    keyword_check_details_json TEXT,
    keyword_check_confidence REAL,
    keyword_check_model TEXT,
    keyword_check_checked_at TEXT,
    dr_check_status TEXT,
    dr_check_pass INTEGER,
    dr_rating REAL,
    dr_check_reason TEXT,
    dr_check_details_json TEXT,
    dr_check_attempts INTEGER NOT NULL DEFAULT 0,
    dr_check_checked_at TEXT,
    live_check_status TEXT,
    live_check_pass INTEGER,
    live_check_class TEXT,
    live_check_reason TEXT,
    live_check_details_json TEXT,
    live_check_attempts INTEGER NOT NULL DEFAULT 0,
    live_check_checked_at TEXT,
    live_check_manual_pass INTEGER NOT NULL DEFAULT 0,
    live_check_manual_pass_at TEXT,
    live_check_final_host TEXT,
    live_check_http_status INTEGER,
    live_check_config_version TEXT,
    wayback_check_status TEXT,
    wayback_check_pass INTEGER,
    wayback_score INTEGER,
    wayback_check_reason TEXT,
    wayback_check_source TEXT,
    wayback_check_details_json TEXT,
    wayback_check_attempts INTEGER NOT NULL DEFAULT 0,
    wayback_check_checked_at TEXT,
    wayback_check_manual_pass INTEGER NOT NULL DEFAULT 0,
    wayback_check_manual_pass_at TEXT,
    wayback_first_capture TEXT,
    wayback_last_capture TEXT,
    wayback_age_years REAL,
    wayback_capture_count INTEGER,
    wayback_config_version TEXT,
    evaluation_status TEXT,
    evaluation_score REAL,
    evaluation_evidence_points REAL,
    evaluation_verdict TEXT,
    evaluation_recommendation TEXT,
    evaluation_reason TEXT,
    evaluation_details_json TEXT,
    evaluation_config_version TEXT,
    evaluation_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_observations (
    domain          TEXT NOT NULL,
    source          TEXT NOT NULL,
    collected_date  TEXT NOT NULL,
    metrics_json    TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (domain, source, collected_date),
    FOREIGN KEY (domain) REFERENCES domains (domain) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_domain_observations_source_date
    ON domain_observations (source, collected_date);

CREATE INDEX IF NOT EXISTS idx_domains_last_seen
    ON domains (last_seen_date);

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

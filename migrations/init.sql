CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) PRIMARY KEY,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    result_links JSON NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    key VARCHAR(255) NOT NULL UNIQUE,
    request_hash CHAR(64) NOT NULL,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS destinations (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    platform_key VARCHAR(64) NOT NULL,
    surface VARCHAR(64) NOT NULL,
    category VARCHAR(128), audience_tags JSON NOT NULL DEFAULT '[]', language VARCHAR(32), geo VARCHAR(64),
    est_audience INTEGER, rules_url VARCHAR(2048), frequency_cap JSON NOT NULL DEFAULT '{}',
    self_promo_allowed BOOLEAN NOT NULL DEFAULT TRUE, link_policy VARCHAR(32) NOT NULL DEFAULT 'allow',
    disclosure_required BOOLEAN NOT NULL DEFAULT FALSE, automation_allowed BOOLEAN NOT NULL DEFAULT TRUE,
    connector_key VARCHAR(64) NOT NULL, relevance_score INTEGER, relevance_reason TEXT,
    level VARCHAR(4) NOT NULL DEFAULT 'L0', last_certified_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rule_decisions (
    id BIGSERIAL PRIMARY KEY, destination_id VARCHAR(36), outcome VARCHAR(16) NOT NULL,
    reason TEXT NOT NULL, error_code VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

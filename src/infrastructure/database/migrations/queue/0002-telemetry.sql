-- depends: 0001-queue

CREATE TABLE IF NOT EXISTS request_log (
    id UUID PRIMARY KEY,
    group_id BIGINT NOT NULL,
    inbox_id BIGINT NOT NULL,
    parent_request_id UUID,
    user_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_source TEXT NOT NULL,
    query_text TEXT, query_hash TEXT, query_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    environment TEXT NOT NULL,
    app_revision TEXT NOT NULL,
    schema_version INT NOT NULL DEFAULT 1,
    outcome JSONB NOT NULL DEFAULT '{}',
    telemetry_state TEXT NOT NULL DEFAULT 'pending',
    recorded_bytes BIGINT NOT NULL DEFAULT 0,
    UNIQUE (group_id, inbox_id)
);


CREATE INDEX IF NOT EXISTS request_log_retention ON request_log (group_id, updated_at);


CREATE TABLE IF NOT EXISTS request_spans
(
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES request_log (id) ON DELETE CASCADE,
    parent_span_id UUID,
    queue TEXT NOT NULL,
    job_id BIGINT NOT NULL,
    attempt INT NOT NULL,
    service TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    error_type TEXT, error_code TEXT, attributes JSONB NOT NULL DEFAULT '{}'
);


CREATE INDEX IF NOT EXISTS request_spans_request ON request_spans (request_id);


ALTER TABLE request_spans ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT 'unknown';


CREATE TABLE IF NOT EXISTS search_runs
(
    span_id UUID PRIMARY KEY REFERENCES request_spans (id) ON DELETE CASCADE,
    ENGINE TEXT NOT NULL,
    RESULT TEXT,
    query_text TEXT,
    normalized_query TEXT,
    building_id TEXT,
    address TEXT,
    candidates JSONB NOT NULL DEFAULT '[]',
    attributes JSONB NOT NULL DEFAULT '{}'
);


CREATE TABLE IF NOT EXISTS network_attempts
(
    span_id UUID PRIMARY KEY REFERENCES request_spans (id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    OPERATION TEXT NOT NULL,
    route TEXT NOT NULL,
    route_reason TEXT NOT NULL,
    attempt INT NOT NULL,
    http_status INT, provider_error_code TEXT, attributes JSONB NOT NULL DEFAULT '{}'
);


CREATE OR REPLACE VIEW request_summary AS
SELECT
    r.*,
    coalesce(r.outcome -> 'result' ->> 'engine', r.outcome ->> 'cached_result_engine') AS result_engine,
    r.outcome -> 'result' ->> 'status' AS search_result,
    coalesce((r.outcome ->> 'cache_hit')::boolean, (r.outcome ->> 'geo_cache_hit')::boolean, FALSE) AS cache_hit,
    r.outcome -> 'result' ->> 'fallback_reason' AS search_fallback_reason,
    r.outcome -> 'result' ->> 'fallback_rejected' AS fallback_rejected_reason,
    EXISTS(
        SELECT 1
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v1'
    ) AS v1_executed,
    EXISTS(
        SELECT 1
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v2'
    ) AS v2_executed,

    (
        SELECT s.result
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v1'
        ORDER BY
            p.started_at DESC,
            p.id DESC
        LIMIT 1
    ) AS v1_result,

    (
        SELECT s.result
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v2'
        ORDER BY
            p.started_at DESC,
            p.id DESC
        LIMIT 1
    ) AS v2_result,

    (
        SELECT sum(p.duration_ms)
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v1'
    ) AS v1_total_ms,

    (
        SELECT sum(p.duration_ms)
        FROM search_runs s
        JOIN request_spans p ON p.id = s.span_id
        WHERE
            p.request_id = r.id
            AND s.engine = 'v2'
    ) AS v2_total_ms,
    (r.outcome ->> 'processing_delay_ms')::double precision AS processing_delay_ms,
    ((
        r.outcome ->> 'last_delivery_at')::double precision - extract(
        EPOCH
        FROM r.received_at
    )) * 1000 AS elapsed_to_last_delivery_ms
FROM request_log r;

CREATE TABLE IF NOT EXISTS bot_inbox (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL,
    event_key TEXT NOT NULL,
    peer_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    next_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    OWNER TEXT, lease_until DOUBLE PRECISION, last_error TEXT, UNIQUE (group_id, event_key)
);


CREATE INDEX IF NOT EXISTS bot_inbox_due ON bot_inbox (group_id, status, next_at, id);


CREATE INDEX IF NOT EXISTS bot_inbox_peer_order ON bot_inbox (group_id, peer_id, id)
WHERE status IN (
    'pending',
    'processing'
);


CREATE TABLE IF NOT EXISTS bot_outbox (
    id BIGSERIAL PRIMARY KEY,
    inbox_id BIGINT NOT NULL REFERENCES bot_inbox (id),
    group_id BIGINT NOT NULL,
    peer_id BIGINT NOT NULL,
    effect_index INT NOT NULL,
    METHOD TEXT NOT NULL,
    params JSONB NOT NULL,
    context JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    next_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    OWNER TEXT, lease_until DOUBLE PRECISION, RESULT JSONB,
    last_error TEXT, UNIQUE (inbox_id, effect_index)
);


CREATE INDEX IF NOT EXISTS bot_outbox_due ON bot_outbox (group_id, status, next_at, id);


CREATE INDEX IF NOT EXISTS bot_outbox_peer_order ON bot_outbox (group_id, peer_id, id)
WHERE status IN (
    'pending',
    'processing'
);


CREATE TABLE IF NOT EXISTS bot_state (
    group_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    value JSONB NOT NULL,
    PRIMARY KEY (
        group_id,
        name
    )
);


CREATE TABLE IF NOT EXISTS bot_message_context (
    group_id BIGINT NOT NULL,
    peer_id BIGINT NOT NULL,
    conversation_message_id BIGINT NOT NULL,
    RESULT JSONB NOT NULL,
    PRIMARY KEY (
        group_id,
        peer_id,
        conversation_message_id
    )
);


CREATE TABLE IF NOT EXISTS bot_receipts (
    group_id BIGINT NOT NULL,
    event_key TEXT NOT NULL,
    PRIMARY KEY (
        group_id,
        event_key
    )
);


ALTER TABLE bot_inbox ADD COLUMN IF NOT EXISTS completed_at DOUBLE PRECISION;


ALTER TABLE bot_outbox ADD COLUMN IF NOT EXISTS completed_at DOUBLE PRECISION;


ALTER TABLE bot_message_context ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(
    EPOCH
    FROM CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS bot_inbox_completed ON bot_inbox (group_id, completed_at)
WHERE status = 'done';


CREATE INDEX IF NOT EXISTS bot_inbox_unfinished_peer ON bot_inbox (group_id, peer_id)
WHERE status <> 'done';


CREATE INDEX IF NOT EXISTS bot_outbox_unfinished_peer ON bot_outbox (group_id, peer_id)
WHERE status <> 'done';


CREATE INDEX IF NOT EXISTS bot_context_expiry ON bot_message_context (group_id, updated_at);

"""Durable inbox/outbox. All identifiers are scoped to a VK community."""

import json
import logging
import math
import random
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.infrastructure.network import network_settings
from src.infrastructure.telemetry import active, measured

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_inbox (
 id BIGSERIAL PRIMARY KEY, group_id BIGINT NOT NULL, event_key TEXT NOT NULL,
 peer_id BIGINT NOT NULL, kind TEXT NOT NULL, payload JSONB NOT NULL,
 occurred_at DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
 next_at DOUBLE PRECISION NOT NULL DEFAULT 0, owner TEXT, lease_until DOUBLE PRECISION,
 last_error TEXT, UNIQUE(group_id,event_key)
);
CREATE INDEX IF NOT EXISTS bot_inbox_due ON bot_inbox(group_id,status,next_at,id);
CREATE INDEX IF NOT EXISTS bot_inbox_peer_order ON bot_inbox(group_id,peer_id,id)
WHERE status IN ('pending','processing');
CREATE TABLE IF NOT EXISTS bot_outbox (
 id BIGSERIAL PRIMARY KEY, inbox_id BIGINT NOT NULL REFERENCES bot_inbox(id),
 group_id BIGINT NOT NULL, peer_id BIGINT NOT NULL, effect_index INT NOT NULL,
 method TEXT NOT NULL, params JSONB NOT NULL, context JSONB,
 status TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
 next_at DOUBLE PRECISION NOT NULL DEFAULT 0, owner TEXT, lease_until DOUBLE PRECISION,
 result JSONB, last_error TEXT, UNIQUE(inbox_id,effect_index)
);
CREATE INDEX IF NOT EXISTS bot_outbox_due ON bot_outbox(group_id,status,next_at,id);
CREATE INDEX IF NOT EXISTS bot_outbox_peer_order ON bot_outbox(group_id,peer_id,id)
WHERE status IN ('pending','processing');
CREATE TABLE IF NOT EXISTS bot_state (
 group_id BIGINT NOT NULL, name TEXT NOT NULL, value JSONB NOT NULL,
 PRIMARY KEY(group_id,name)
);
CREATE TABLE IF NOT EXISTS bot_message_context (
 group_id BIGINT NOT NULL, peer_id BIGINT NOT NULL, conversation_message_id BIGINT NOT NULL,
 result JSONB NOT NULL, PRIMARY KEY(group_id,peer_id,conversation_message_id)
);
CREATE TABLE IF NOT EXISTS bot_receipts (
 group_id BIGINT NOT NULL,event_key TEXT NOT NULL,PRIMARY KEY(group_id,event_key)
);
ALTER TABLE bot_inbox ADD COLUMN IF NOT EXISTS completed_at DOUBLE PRECISION;
ALTER TABLE bot_outbox ADD COLUMN IF NOT EXISTS completed_at DOUBLE PRECISION;
ALTER TABLE bot_message_context ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION
 NOT NULL DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS bot_inbox_completed ON bot_inbox(group_id,completed_at) WHERE status='done';
CREATE INDEX IF NOT EXISTS bot_inbox_unfinished_peer ON bot_inbox(group_id,peer_id) WHERE status<>'done';
CREATE INDEX IF NOT EXISTS bot_outbox_unfinished_peer ON bot_outbox(group_id,peer_id) WHERE status<>'done';
CREATE INDEX IF NOT EXISTS bot_context_expiry ON bot_message_context(group_id,updated_at);
"""


def event_record(group_id: int, kind: str, obj: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if kind == "message_new":
        message = obj["message"]
        peer = int(message["peer_id"])
        if message.get("out") or peer != int(message["from_id"]) or peer <= 0:
            raise ValueError("Only incoming private user messages are supported")
        cmid = message.get("conversation_message_id")
        identity = f"cmid:{cmid}" if cmid else f"id:{message['id']}"
        key = f"message:{peer}:{identity}"
        occurred = message.get("date", now)
    elif kind == "message_event":
        peer = int(obj["peer_id"])
        if peer <= 0 or peer != int(obj["user_id"]):
            raise ValueError("Only private user callbacks are supported")
        key = f"callback:{peer}:{obj['event_id']}"
        occurred = now
    else:
        raise ValueError("Unsupported event type")
    return {
        "group_id": group_id,
        "event_key": key,
        "peer_id": peer,
        "kind": kind,
        "payload": obj,
        "occurred_at": occurred,
        "created_at": now,
    }


class QueueStore:
    def __init__(
        self,
        dsn: str,
        group_id: int,
        lease_seconds: int = 120,
        telemetry: bool = False,
        *,
        max_pending_messages_per_peer: int | None = None,
        reply_interval_seconds: float = 0,
    ):
        if (
            max_pending_messages_per_peer is not None and max_pending_messages_per_peer < 1
        ) or reply_interval_seconds < 0:
            raise ValueError("Invalid per-user queue limits")
        self.dsn = dsn
        self.group_id = group_id
        self.lease_seconds = lease_seconds
        self.telemetry = telemetry
        self.environment = "unknown"
        # Runtime supplies the configured limits; maintenance clients need not.
        self.max_pending_messages_per_peer = max_pending_messages_per_peer
        self.reply_interval_seconds = reply_interval_seconds

    def _queue_lock(self, conn: psycopg.Connection) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(20261106,%s)", (self.group_id,))

    def _trim_pending(self, conn: psycopg.Connection, peer: int) -> set[int]:
        if self.max_pending_messages_per_peer is None:
            return set()
        # Only whole, unstarted requests can be removed. An attempted or partly
        # delivered outbox must retain its stable random_id and remaining pages.
        rows = conn.execute(
            r"""WITH waiting_ids AS (
              SELECT id FROM bot_inbox WHERE group_id=%s AND peer_id=%s AND kind='message_new' AND status='pending'
              UNION SELECT inbox_id FROM bot_outbox WHERE group_id=%s AND peer_id=%s AND status='pending'
            ), waiting AS (
              SELECT i.id,i.occurred_at,
                coalesce((i.payload#>>'{message,conversation_message_id}')::bigint,
                         (i.payload#>>'{message,id}')::bigint,i.id) AS message_order,
                jsonb_build_object(
                  'text',btrim(regexp_replace(lower(coalesce(i.payload#>>'{message,text}','')),'\s+',' ','g')),
                  'geo',i.payload#>'{message,geo}', 'attachments',i.payload#>'{message,attachments}',
                  'payload',i.payload#>'{message,payload}', 'reply',i.payload#>'{message,reply_message}',
                  'forwarded',i.payload#>'{message,fwd_messages}') AS signature
              FROM waiting_ids w JOIN bot_inbox i ON i.id=w.id WHERE i.kind='message_new'
                AND (i.status='pending' OR (i.status='done'
                  AND NOT EXISTS(SELECT 1 FROM bot_outbox o WHERE o.inbox_id=i.id
                    AND (o.status<>'pending' OR o.attempts>0 OR o.result IS NOT NULL))))
            ), duplicates AS (
              SELECT *,row_number() OVER(PARTITION BY signature
                ORDER BY occurred_at DESC,message_order DESC,id DESC) AS duplicate_order FROM waiting
            ), latest AS (
              SELECT id,row_number() OVER(ORDER BY occurred_at DESC,message_order DESC,id DESC) AS position
              FROM duplicates WHERE duplicate_order=1
            ), obsolete AS (
              SELECT id,'superseded_duplicate' AS reason FROM duplicates WHERE duplicate_order>1
              UNION ALL SELECT id,'pending_limit' AS reason FROM latest WHERE position>%s
            ) UPDATE bot_inbox i SET status='done',completed_at=%s,owner=NULL,lease_until=NULL,
                payload=jsonb_set(i.payload,'{_queue_disposition}',to_jsonb(o.reason))
              FROM obsolete o WHERE i.id=o.id RETURNING i.id""",
            (self.group_id, peer, self.group_id, peer, self.max_pending_messages_per_peer, time.time()),
        ).fetchall()
        ids = {r["id"] for r in rows}
        if ids:
            conn.execute(
                "UPDATE bot_outbox SET status='done',completed_at=%s WHERE inbox_id=ANY(%s) AND group_id=%s",
                (time.time(), list(ids), self.group_id),
            )
            for inbox_id in ids:
                self._observe(conn, inbox_id)
        return ids

    def _reply_gate(self, conn: psycopg.Connection, peer: int) -> float:
        name = f"reply_gate:{peer}"
        row = conn.execute(
            "SELECT value FROM bot_state WHERE group_id=%s AND name=%s",
            (self.group_id, name),
        ).fetchone()
        return float(row["value"]) if row else 0

    def _reserve_reply(self, conn: psycopg.Connection, peer: int, now: float) -> None:
        if self.reply_interval_seconds:
            self._state(
                conn,
                f"reply_gate:{peer}",
                max(self._reply_gate(conn, peer), now + self.reply_interval_seconds),
            )

    def _observe(self, conn: psycopg.Connection, inbox_id: int) -> None:
        # A measured worker flushes once after the operation. Ingest, failures
        # outside that scope and callers without Capture still snapshot here.
        if self.telemetry and not active():
            from src.vkbot.telemetry import guarded, refresh, register

            def update() -> None:
                if register(conn, self.group_id, inbox_id, self.environment):
                    refresh(conn, self.group_id, inbox_id)
                    parent = conn.execute(
                        "SELECT payload->>'_geo_source_id' AS id FROM bot_inbox WHERE id=%s AND group_id=%s",
                        (inbox_id, self.group_id),
                    ).fetchone()
                    if parent and parent["id"]:
                        refresh(conn, self.group_id, int(parent["id"]))

            guarded(conn, update)

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self.dsn,
            connect_timeout=network_settings.queue_db_connect_timeout_seconds,
            row_factory=dict_row,
            options=(
                f"-c statement_timeout={network_settings.queue_db_statement_timeout_ms} "
                f"-c lock_timeout={network_settings.queue_db_lock_timeout_ms}"
            ),
        )

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)
            if self.telemetry:
                from src.vkbot.telemetry import SCHEMA as TELEMETRY_SCHEMA

                try:
                    with conn.transaction():
                        conn.execute(TELEMETRY_SCHEMA)
                except Exception as exc:
                    self.telemetry = False
                    logging.getLogger(__name__).warning("Telemetry schema unavailable error=%s", type(exc).__name__)
            for table in ("bot_inbox", "bot_outbox"):
                conn.execute(
                    f"UPDATE {table} SET completed_at=%s WHERE group_id=%s AND status='done' AND completed_at IS NULL",
                    (time.time(), self.group_id),
                )
            conn.execute(
                "INSERT INTO bot_state VALUES (%s,'tracking_started',%s) ON CONFLICT DO NOTHING",
                (self.group_id, Jsonb(time.time())),
            )
            if self.max_pending_messages_per_peer is not None:
                self._queue_lock(conn)
                peers = conn.execute(
                    "SELECT peer_id FROM bot_inbox WHERE group_id=%s AND kind='message_new' AND status='pending' "
                    "UNION SELECT peer_id FROM bot_outbox WHERE group_id=%s AND status='pending'",
                    (self.group_id, self.group_id),
                ).fetchall()
                for row in peers:
                    self._trim_pending(conn, row["peer_id"])

    def get_state(self, name: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE group_id=%s AND name=%s", (self.group_id, name)
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, name: str, value: Any) -> None:
        with self.connect() as conn:
            self._state(conn, name, value)

    def _state(self, conn: psycopg.Connection, name: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO bot_state VALUES (%s,%s,%s) ON CONFLICT(group_id,name) DO UPDATE SET value=excluded.value",
            (self.group_id, name, Jsonb(value)),
        )

    def ingest(
        self,
        records: list[dict],
        cursor: str | None = None,
        callback_window: float = 5,
        notice_interval: float = 10,
        ack_interval: float = 1,
    ) -> int:
        added = 0
        with self.connect() as conn:
            # Serializes receipt compaction with both collector and history replay.
            self._queue_lock(conn)
            peers = set()
            for record in records:
                assert record["group_id"] == self.group_id
                if conn.execute(
                    "SELECT 1 FROM bot_receipts WHERE group_id=%s AND event_key=%s",
                    (self.group_id, record["event_key"]),
                ).fetchone():
                    continue
                row = conn.execute(
                    """INSERT INTO bot_inbox
                    (group_id,event_key,peer_id,kind,payload,occurred_at,created_at,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(group_id,event_key) DO NOTHING RETURNING id""",
                    (
                        self.group_id,
                        record["event_key"],
                        record["peer_id"],
                        record["kind"],
                        Jsonb(record["payload"]),
                        record["occurred_at"],
                        record["created_at"],
                        record.get("status", "pending"),
                    ),
                ).fetchone()
                if row and record["kind"] == "message_event":
                    try:
                        self._register_callback(
                            conn,
                            record,
                            row["id"],
                            callback_window,
                            notice_interval,
                            ack_interval,
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        conn.execute(
                            "UPDATE bot_inbox SET status='failed',last_error=%s WHERE id=%s",
                            (safe_error(exc), row["id"]),
                        )
                added += bool(row)
                if row and record["kind"] == "message_new":
                    peers.add(record["peer_id"])
                    conn.execute(
                        """UPDATE bot_inbox SET status='done',completed_at=%s WHERE group_id=%s AND peer_id=%s
                        AND kind='geo_retry' AND status IN ('deferred','failed') AND id<%s""",
                        (time.time(), self.group_id, record["peer_id"], row["id"]),
                    )
                if row and record.get("status") == "done":
                    conn.execute(
                        "UPDATE bot_inbox SET completed_at=%s WHERE id=%s",
                        (time.time(), row["id"]),
                    )
                if row and self.telemetry:
                    self._observe(conn, row["id"])
            for peer in sorted(peers):
                self._trim_pending(conn, peer)
            if cursor is not None:
                self._state(conn, "longpoll_ts", str(cursor))
        return added

    def _register_callback(
        self,
        conn: psycopg.Connection,
        record: dict,
        inbox_id: int,
        window: float,
        notice_interval: float,
        ack_interval: float,
    ) -> None:
        obj = record["payload"]
        action = obj["payload"]
        if isinstance(action, str):
            action = json.loads(action)
        if "entrance" in action:
            target = "entrance:" + str(action["entrance"])
        elif "select_building" in action:
            target = "building:" + str(int(action["select_building"]))
        elif action.get("dismiss_choices") is True:
            target = "dismiss"
        elif action.get("return_to_choices") is True:
            target = "return_to_choices"
        elif "entrance_page" in action:
            target = "entrances:" + str(int(action["entrance_page"]))
        else:
            target = "page:" + str(int(action["choice_page"]))
        signature = f"{int(obj['conversation_message_id'])}:{target}"
        name = f"callback_gate:{record['peer_id']}"
        conn.execute("INSERT INTO bot_state VALUES (%s,%s,'{}') ON CONFLICT DO NOTHING", (self.group_id, name))
        state = conn.execute(
            "SELECT value FROM bot_state WHERE group_id=%s AND name=%s FOR UPDATE", (self.group_id, name)
        ).fetchone()["value"]
        now = time.time()
        previous = None
        if state.get("canonical_id"):
            previous = conn.execute(
                """SELECT i.status,i.last_error,
                EXISTS(SELECT 1 FROM bot_outbox o WHERE o.inbox_id=i.id
                AND o.status IN ('pending','processing')) AS active,
                EXISTS(SELECT 1 FROM bot_outbox o WHERE o.inbox_id=i.id AND o.status='failed') AS failed,
                EXISTS(SELECT 1 FROM bot_outbox o WHERE o.inbox_id=i.id
                AND o.status IN ('pending','processing') AND o.last_error IS NOT NULL) AS delayed
                FROM bot_inbox i WHERE i.id=%s AND i.group_id=%s""",
                (state["canonical_id"], self.group_id),
            ).fetchone()
        pending = bool(previous and (previous["status"] in ("pending", "processing") or previous["active"]))
        failed = bool(previous and (previous["status"] == "failed" or previous["failed"]))
        remaining = max(0, math.ceil(state.get("completed_at", state.get("accepted_at", 0)) + window - now))
        duplicate = bool(previous and not failed and state.get("signature") == signature and (pending or remaining > 0))
        throttled = bool(previous and not failed and (pending or remaining > 0))
        delayed = bool(previous and (previous["last_error"] or previous["delayed"]))
        notice = None
        if throttled:
            notice = "pending" if pending and (delayed or remaining == 0) else "wait"
        else:
            state.update(signature=signature, canonical_id=inbox_id, accepted_at=now)
            state.pop("completed_at", None)
            # A known pending failure in this peer can delay a newly accepted click.
            blocked = conn.execute(
                """SELECT EXISTS(SELECT 1 FROM bot_outbox WHERE group_id=%s AND peer_id=%s
                AND status IN ('pending','processing') AND last_error IS NOT NULL) AS blocked""",
                (self.group_id, record["peer_id"]),
            ).fetchone()["blocked"]
            if blocked:
                notice = "pending"
        feedback = "skip"
        if notice and now - state.get("notice_at", 0) >= notice_interval:
            feedback = notice
            state["notice_at"] = now
        elif now - state.get("ack_at", 0) >= ack_interval:
            feedback = "silent"
        if feedback != "skip":
            state["ack_at"] = now
        receipt = {
            "feedback": feedback,
            "wait_seconds": max(1, remaining),
            "duplicate": duplicate,
            "throttled": throttled,
            "canonical_id": state["canonical_id"],
            "ack_reserved": False,
        }
        stored = dict(obj, _queue_receipt=receipt)
        conn.execute(
            "UPDATE bot_inbox SET payload=%s,status=%s,completed_at=%s WHERE id=%s",
            (
                Jsonb(stored),
                "done" if throttled else record.get("status", "pending"),
                now if throttled else None,
                inbox_id,
            ),
        )
        self._state(conn, name, state)

    def callback_feedback(self, event_key: str) -> dict | None:
        # Reserve once before touching VK; ephemeral acks are never retried.
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id,payload FROM bot_inbox WHERE group_id=%s AND event_key=%s FOR UPDATE",
                (self.group_id, event_key),
            ).fetchone()
            if not row:
                return None
            obj = row["payload"]
            receipt = obj.get("_queue_receipt", {})
            if receipt.get("ack_reserved") or receipt.get("feedback", "skip") == "skip":
                return None
            receipt["ack_reserved"] = True
            conn.execute("UPDATE bot_inbox SET payload=%s WHERE id=%s", (Jsonb(obj), row["id"]))
            return dict(receipt, inbox_id=row["id"])

    @staticmethod
    def _table(table: str) -> str:
        if table not in {"bot_inbox", "bot_outbox"}:
            raise ValueError("Invalid queue table")
        return table

    def claim(self, table: str) -> dict | None:
        table = self._table(table)
        now = time.time()
        owner = str(uuid.uuid4())
        with self.connect() as conn:
            if self.max_pending_messages_per_peer is not None or self.reply_interval_seconds:
                self._queue_lock(conn)
            if table == "bot_inbox":
                conn.execute(
                    """UPDATE bot_inbox i SET status='done',completed_at=%s
                    WHERE i.group_id=%s AND i.status='deferred' AND EXISTS(
                    SELECT 1 FROM bot_inbox newer WHERE newer.group_id=i.group_id AND newer.peer_id=i.peer_id
                    AND newer.kind='message_new' AND newer.id>(i.payload->>'_geo_source_id')::bigint)""",
                    (now, self.group_id),
                )
                conn.execute(
                    """UPDATE bot_inbox SET status='failed',last_error=%s WHERE group_id=%s AND status='deferred'
                    AND (payload->>'_geo_deadline')::double precision<=%s""",
                    (json.dumps({"type": "GeoRetryExpired"}), self.group_id, now),
                )
            while True:
                row = conn.execute(
                    f"""SELECT q.* FROM {table} q
                WHERE q.group_id=%s AND (
                  (q.status IN ('pending','deferred') AND q.next_at<=%s) OR
                  (q.status='processing' AND q.lease_until<%s))
                AND NOT EXISTS (SELECT 1 FROM {table} older
                  WHERE older.group_id=q.group_id AND older.peer_id=q.peer_id
                  AND older.id<q.id AND older.status IN ('pending','processing'))
                ORDER BY q.next_at,q.id FOR UPDATE OF q SKIP LOCKED LIMIT 1""",
                    (self.group_id, now, now),
                ).fetchone()
                if not row:
                    return None
                removed = self._trim_pending(conn, row["peer_id"])
                if row["id" if table == "bot_inbox" else "inbox_id"] not in removed:
                    break
            now = time.time()
            if table == "bot_outbox" and self.reply_interval_seconds:
                eligible = self._reply_gate(conn, row["peer_id"])
                if eligible > now:
                    conn.execute(
                        "UPDATE bot_outbox SET status='pending',owner=NULL,lease_until=NULL,next_at=%s WHERE id=%s",
                        (eligible, row["id"]),
                    )
                    return None
                self._reserve_reply(conn, row["peer_id"], now)
            conn.execute(
                f"UPDATE {table} SET status='processing',owner=%s,lease_until=%s,attempts=attempts+1 WHERE id=%s",
                (owner, now + self.lease_seconds, row["id"]),
            )
            row.update(owner=owner, attempts=row["attempts"] + 1)
            return row

    def renew(self, table: str, job: dict) -> bool:
        table = self._table(table)
        with self.connect() as conn:
            return bool(
                conn.execute(
                    f"UPDATE {table} SET lease_until=%s WHERE id=%s AND group_id=%s "
                    "AND owner=%s AND status='processing' RETURNING id",
                    (time.time() + self.lease_seconds, job["id"], self.group_id, job["owner"]),
                ).fetchone()
            )

    @measured("outbox.persist")
    def plan(self, job: dict, effects: list[dict], callback_at: float | None = None) -> None:
        with self.connect() as conn:
            if self.max_pending_messages_per_peer is not None:
                self._queue_lock(conn)
            self._owned(conn, "bot_inbox", job)
            if job["kind"] == "geo_retry" and self._geo_superseded(conn, job["peer_id"], job["payload"]):
                effects = []
            for index, effect in enumerate(effects):
                if effect.get("defer_geo"):
                    retry = effect["defer_geo"]
                    payload = dict(
                        job["payload"],
                        _geo_source_id=job["id"],
                        _geo_deadline=time.time() + retry["max_age"],
                    )
                    conn.execute(
                        """INSERT INTO bot_inbox
                        (group_id,event_key,peer_id,kind,payload,occurred_at,created_at,status,next_at)
                        VALUES(%s,%s,%s,'geo_retry',%s,%s,%s,'deferred',%s) ON CONFLICT DO NOTHING""",
                        (
                            self.group_id,
                            "geo-retry:" + job["event_key"],
                            job["peer_id"],
                            Jsonb(payload),
                            job["occurred_at"],
                            time.time(),
                            time.time() + retry["delay"],
                        ),
                    )
                if effect.get("close_choices"):
                    cmid = int(effect["params"]["conversation_message_id"])
                    self._state(conn, f"choices_closed:{job['peer_id']}:{cmid}", True)
                conn.execute(
                    """INSERT INTO bot_outbox(inbox_id,group_id,peer_id,effect_index,method,params,context)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(inbox_id,effect_index) DO NOTHING""",
                    (
                        job["id"],
                        self.group_id,
                        job["peer_id"],
                        index,
                        effect["method"],
                        Jsonb(effect["params"]),
                        Jsonb(effect.get("context")),
                    ),
                )
            if callback_at is not None:
                self._state(conn, f"callback:{job['peer_id']}", callback_at)
            conn.execute(
                "UPDATE bot_inbox SET status='done',owner=NULL,lease_until=NULL,last_error=NULL,"
                "completed_at=%s WHERE id=%s",
                (time.time(), job["id"]),
            )
            if not effects:
                self._callback_completed(conn, job["peer_id"], job["id"])
            self._observe(conn, job["id"])
            self._trim_pending(conn, job["peer_id"])

    def _geo_current(self, conn: psycopg.Connection, peer: int, payload: dict) -> bool:
        if time.time() >= payload["_geo_deadline"]:
            return False
        return not conn.execute(
            "SELECT 1 FROM bot_inbox WHERE group_id=%s AND peer_id=%s AND kind='message_new' AND id>%s LIMIT 1",
            (self.group_id, peer, payload["_geo_source_id"]),
        ).fetchone()

    def geo_retry_current(self, job: dict) -> bool:
        with self.connect() as conn:
            return self._geo_current(conn, job["peer_id"], job["payload"])

    def outbox_geo_current(self, job: dict) -> bool:
        with self.connect() as conn:
            parent = conn.execute(
                "SELECT kind,payload FROM bot_inbox WHERE group_id=%s AND id=%s", (self.group_id, job["inbox_id"])
            ).fetchone()
            return parent["kind"] != "geo_retry" or not self._geo_superseded(conn, job["peer_id"], parent["payload"])

    def defer_geo_retry(self, job: dict, delay: float) -> None:
        with self.connect() as conn:
            self._owned(conn, "bot_inbox", job)
            current = self._geo_current(conn, job["peer_id"], job["payload"])
            expired = not current and not self._geo_superseded(conn, job["peer_id"], job["payload"])
            conn.execute(
                "UPDATE bot_inbox SET status=%s,next_at=%s,owner=NULL,lease_until=NULL,"
                "completed_at=%s,last_error=%s WHERE id=%s",
                (
                    "deferred" if current else ("failed" if expired else "done"),
                    min(time.time() + delay, job["payload"]["_geo_deadline"]),
                    None if current or expired else time.time(),
                    json.dumps({"type": "GeoRetryExpired"}) if expired else None,
                    job["id"],
                ),
            )
            self._observe(conn, job["id"])

    def _geo_superseded(self, conn: psycopg.Connection, peer: int, payload: dict) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM bot_inbox WHERE group_id=%s AND peer_id=%s AND kind='message_new' AND id>%s LIMIT 1",
                (self.group_id, peer, payload["_geo_source_id"]),
            ).fetchone()
        )

    def _owned(self, conn: psycopg.Connection, table: str, job: dict) -> None:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE id=%s AND group_id=%s AND owner=%s "
            "AND status='processing' AND lease_until>%s FOR UPDATE",
            (job["id"], self.group_id, job["owner"], time.time()),
        ).fetchone()
        if not row:
            raise RuntimeError("Queue lease lost")

    def save_result(self, job: dict, result: Any) -> None:
        with self.connect() as conn:
            self._owned(conn, "bot_outbox", job)
            conn.execute("UPDATE bot_outbox SET result=%s WHERE id=%s", (Jsonb(result), job["id"]))

    @measured("outbox.finish")
    def finish(self, job: dict, context: tuple[int, dict] | None = None) -> None:
        with self.connect() as conn:
            if self.reply_interval_seconds:
                self._queue_lock(conn)
            self._owned(conn, "bot_outbox", job)
            if context:
                cmid, result = context
                if result.get("_reopen_choices") is True:
                    result = dict(result)
                    result.pop("_reopen_choices")
                    if result.get("status") == "ambiguous" and result.get("candidates"):
                        self._state(conn, f"choices_closed:{job['peer_id']}:{cmid}", False)
                conn.execute(
                    """INSERT INTO bot_message_context(group_id,peer_id,conversation_message_id,result,updated_at)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(group_id,peer_id,conversation_message_id) DO UPDATE SET result=excluded.result,
                    updated_at=excluded.updated_at""",
                    (self.group_id, job["peer_id"], cmid, Jsonb(result), time.time()),
                )
            conn.execute(
                "UPDATE bot_outbox SET status='done',owner=NULL,lease_until=NULL,last_error=NULL,"
                "completed_at=%s WHERE id=%s",
                (time.time(), job["id"]),
            )
            self._callback_completed(conn, job["peer_id"], job["inbox_id"])
            self._reserve_reply(conn, job["peer_id"], time.time())
            self._observe(conn, job["inbox_id"])

    def _callback_completed(self, conn: psycopg.Connection, peer: int, inbox_id: int) -> None:
        name = f"callback_gate:{peer}"
        row = conn.execute(
            "SELECT value FROM bot_state WHERE group_id=%s AND name=%s FOR UPDATE", (self.group_id, name)
        ).fetchone()
        if not row or row["value"].get("canonical_id") != inbox_id:
            return
        pending = conn.execute(
            "SELECT 1 FROM bot_outbox WHERE inbox_id=%s AND status IN ('pending','processing') LIMIT 1", (inbox_id,)
        ).fetchone()
        if not pending:
            state = row["value"]
            state["completed_at"] = time.time()
            self._state(conn, name, state)

    def fail(self, table: str, job: dict, error: str, delay: float, permanent: bool = False) -> None:
        table = self._table(table)
        with self.connect() as conn:
            if self.reply_interval_seconds:
                self._queue_lock(conn)
            changed = conn.execute(
                f"""UPDATE {table} SET status=%s,next_at=%s,last_error=%s,owner=NULL,lease_until=NULL
                WHERE id=%s AND group_id=%s AND owner=%s AND status='processing'""",
                (
                    "failed" if permanent else "pending",
                    time.time() + delay,
                    error[:300],
                    job["id"],
                    self.group_id,
                    job["owner"],
                ),
            ).rowcount
            if changed and table == "bot_outbox":
                self._reserve_reply(conn, job["peer_id"], time.time())
            self._observe(conn, job["id"] if table == "bot_inbox" else job["inbox_id"])

    def message_context(self, peer: int, cmid: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result FROM bot_message_context WHERE group_id=%s AND peer_id=%s "
                "AND conversation_message_id=%s",
                (self.group_id, peer, cmid),
            ).fetchone()
            return row["result"] if row else None

    def choices_closed(self, peer: int, cmid: int) -> bool:
        return self.get_state(f"choices_closed:{peer}:{cmid}", False) is True

    def consume_fault(self, name: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE group_id=%s AND name=%s FOR UPDATE", (self.group_id, "fault:" + name)
            ).fetchone()
            if not row or not row["value"]:
                return False
            count = int(row["value"])
            if count > 0:
                self._state(conn, "fault:" + name, count - 1)
            return True

    def stats(self) -> dict:
        result = {}
        with self.connect() as conn:
            for table in ("bot_inbox", "bot_outbox"):
                result[table] = conn.execute(
                    f"SELECT status,count(*) AS count FROM {table} WHERE group_id=%s GROUP BY status", (self.group_id,)
                ).fetchall()
            result["last_reconciliation"] = self.get_state("reconcile_watermark")
        return result

    def retry_failed(self, geo_max_age: float = 1800) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}',%s) WHERE group_id=%s
                AND kind='geo_retry' AND status='failed'""",
                (Jsonb(time.time() + geo_max_age), self.group_id),
            )
            for table in ("bot_inbox", "bot_outbox"):
                conn.execute(
                    f"UPDATE {table} SET status='pending',next_at=0,last_error=NULL "
                    "WHERE group_id=%s AND status='failed'",
                    (self.group_id,),
                )


def retry_delay(attempt: int, base: float, maximum: float) -> float:
    return min(maximum, base * 2 ** min(attempt - 1, 12) * random.uniform(0.8, 1.2))


def safe_error(exc: Exception) -> str:
    # Never store request URLs, tokens, message text, or API error payloads.
    code = getattr(exc, "code", None)
    return json.dumps({"type": type(exc).__name__, "vk_code": code})

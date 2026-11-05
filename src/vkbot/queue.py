"""Durable inbox/outbox. All identifiers are scoped to a VK community."""

import json
import math
import random
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.network import network_settings

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
    def __init__(self, dsn: str, group_id: int, lease_seconds: int = 120):
        self.dsn = dsn
        self.group_id = group_id
        self.lease_seconds = lease_seconds

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
            conn.execute(
                "INSERT INTO bot_state VALUES (%s,'tracking_started',%s) ON CONFLICT DO NOTHING",
                (self.group_id, Jsonb(time.time())),
            )

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
            for record in records:
                assert record["group_id"] == self.group_id
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
                        self._register_callback(conn, record, row["id"], callback_window, notice_interval, ack_interval)
                    except (KeyError, ValueError, TypeError) as exc:
                        conn.execute(
                            "UPDATE bot_inbox SET status='failed',last_error=%s WHERE id=%s",
                            (safe_error(exc), row["id"]),
                        )
                added += bool(row)
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
        signature = f"{int(obj['conversation_message_id'])}:{action['entrance']}"
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
        remaining = max(0, math.ceil(state.get("accepted_at", 0) + window - now))
        duplicate = bool(previous and not failed and state.get("signature") == signature and (pending or remaining > 0))
        delayed = bool(previous and (previous["last_error"] or previous["delayed"]))
        notice = None
        if duplicate:
            notice = "pending" if pending and (delayed or remaining == 0) else "wait"
        else:
            state.update(signature=signature, canonical_id=inbox_id, accepted_at=now)
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
            "canonical_id": state["canonical_id"],
            "ack_reserved": False,
        }
        stored = dict(obj, _queue_receipt=receipt)
        conn.execute(
            "UPDATE bot_inbox SET payload=%s,status=%s WHERE id=%s",
            (Jsonb(stored), "done" if duplicate else record.get("status", "pending"), inbox_id),
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
            return receipt

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
            row = conn.execute(
                f"""SELECT q.* FROM {table} q
                WHERE q.group_id=%s AND (
                  (q.status='pending' AND q.next_at<=%s) OR
                  (q.status='processing' AND q.lease_until<%s))
                AND NOT EXISTS (SELECT 1 FROM {table} older
                  WHERE older.group_id=q.group_id AND older.peer_id=q.peer_id
                  AND older.id<q.id AND older.status IN ('pending','processing'))
                ORDER BY q.next_at,q.id FOR UPDATE OF q SKIP LOCKED LIMIT 1""",
                (self.group_id, now, now),
            ).fetchone()
            if not row:
                return None
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

    def plan(self, job: dict, effects: list[dict], callback_at: float | None = None) -> None:
        with self.connect() as conn:
            self._owned(conn, "bot_inbox", job)
            for index, effect in enumerate(effects):
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
                "UPDATE bot_inbox SET status='done',owner=NULL,lease_until=NULL,last_error=NULL WHERE id=%s",
                (job["id"],),
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

    def finish(self, job: dict, context: tuple[int, dict] | None = None) -> None:
        with self.connect() as conn:
            self._owned(conn, "bot_outbox", job)
            if context:
                cmid, result = context
                conn.execute(
                    """INSERT INTO bot_message_context VALUES (%s,%s,%s,%s)
                    ON CONFLICT(group_id,peer_id,conversation_message_id) DO UPDATE SET result=excluded.result""",
                    (self.group_id, job["peer_id"], cmid, Jsonb(result)),
                )
            conn.execute(
                "UPDATE bot_outbox SET status='done',owner=NULL,lease_until=NULL,last_error=NULL WHERE id=%s",
                (job["id"],),
            )

    def fail(self, table: str, job: dict, error: str, delay: float, permanent: bool = False) -> None:
        table = self._table(table)
        with self.connect() as conn:
            conn.execute(
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
            )

    def message_context(self, peer: int, cmid: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result FROM bot_message_context WHERE group_id=%s AND peer_id=%s "
                "AND conversation_message_id=%s",
                (self.group_id, peer, cmid),
            ).fetchone()
            return row["result"] if row else None

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

    def retry_failed(self) -> None:
        with self.connect() as conn:
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

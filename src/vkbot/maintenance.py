"""Ограниченные порции очистки; идентификаторы доставки сохраняются."""

import time

from src.vkbot.queue import QueueStore


def compact(store: QueueStore, retention_seconds: float, context_seconds: float, batch: int) -> dict:
    if retention_seconds <= 0 or context_seconds <= 0 or not 1 <= batch <= 10000:
        raise ValueError("Invalid maintenance limits")
    now = time.time()
    with store.connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(20261106,%s)", (store.group_id,))
        rows = db.execute(
            """SELECT i.id,i.event_key FROM bot_inbox i
            WHERE i.group_id=%s AND i.status='done' AND i.completed_at<%s
            AND NOT EXISTS(SELECT 1 FROM bot_outbox o WHERE o.inbox_id=i.id
                AND (o.status<>'done' OR o.completed_at IS NULL OR o.completed_at>=%s))
            ORDER BY i.completed_at,i.id FOR UPDATE OF i SKIP LOCKED LIMIT %s""",
            (store.group_id, now - retention_seconds, now - retention_seconds, batch),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for row in rows:
            db.execute(
                "INSERT INTO bot_receipts VALUES(%s,%s) ON CONFLICT DO NOTHING", (store.group_id, row["event_key"])
            )
        outgoing = db.execute(
            "DELETE FROM bot_outbox WHERE group_id=%s AND inbox_id=ANY(%s)", (store.group_id, ids)
        ).rowcount
        db.execute("DELETE FROM bot_inbox WHERE group_id=%s AND id=ANY(%s)", (store.group_id, ids))
        contexts = db.execute(
            """WITH expired AS (
            SELECT c.group_id,c.peer_id,c.conversation_message_id FROM bot_message_context c
            WHERE c.group_id=%s AND c.updated_at<%s
            AND NOT EXISTS(SELECT 1 FROM bot_inbox i WHERE i.group_id=c.group_id AND i.peer_id=c.peer_id
                AND i.status<>'done')
            AND NOT EXISTS(SELECT 1 FROM bot_outbox o WHERE o.group_id=c.group_id AND o.peer_id=c.peer_id
                AND o.status<>'done')
            ORDER BY c.updated_at LIMIT %s FOR UPDATE OF c SKIP LOCKED)
            DELETE FROM bot_message_context c USING expired e WHERE c.group_id=e.group_id AND c.peer_id=e.peer_id
            AND c.conversation_message_id=e.conversation_message_id RETURNING c.peer_id,c.conversation_message_id""",
            (store.group_id, now - context_seconds, batch),
        ).fetchall()
        for c in contexts:
            db.execute(
                "DELETE FROM bot_state WHERE group_id=%s AND name=%s",
                (store.group_id, f"choices_closed:{c['peer_id']}:{c['conversation_message_id']}"),
            )
    return {"inbox_removed": len(ids), "outbox_removed": outgoing, "contexts_removed": len(contexts)}


def health(store: QueueStore) -> dict:
    now = time.time()
    with store.connect() as db:
        size = db.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"]
        result = {"database_bytes": size, "queues": {}}
        for table in ("bot_inbox", "bot_outbox"):
            column = "created_at" if table == "bot_inbox" else "(SELECT created_at FROM bot_inbox WHERE id=q.inbox_id)"
            rows = db.execute(
                f"SELECT status,count(*) AS count,min({column}) AS oldest FROM {table} q "
                "WHERE group_id=%s GROUP BY status",
                (store.group_id,),
            ).fetchall()
            result["queues"][table] = [
                {
                    "status": r["status"],
                    "count": r["count"],
                    "oldest_age_seconds": round(max(0, now - r["oldest"])) if r["oldest"] else 0,
                }
                for r in rows
            ]
        result["receipt_count"] = db.execute(
            "SELECT count(*) AS n FROM bot_receipts WHERE group_id=%s", (store.group_id,)
        ).fetchone()["n"]
    return result

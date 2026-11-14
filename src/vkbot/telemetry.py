"""Диагностика в существующей БД, с отдельным сроком хранения и без FK на очередь."""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb

from src.infrastructure.database.schema import require_schema
from src.infrastructure.telemetry import settings

logger = logging.getLogger(__name__)


def verify_schema(conn: Any) -> None:
    """Проверить наличие миграции аналитики до её включения, не исполняя DDL."""
    require_schema(conn, telemetry=True)


def identity(group: int, inbox: int) -> str:
    """Получить устойчивый UUID запроса по сообществу и входящему заданию."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"domofomka:{group}:inbox:{inbox}"))


def guarded(conn: Any, operation: Callable) -> None:
    """SAVEPOINT не даёт сбою аналитики отменить транзакцию рабочей очереди."""
    try:
        with conn.transaction():
            previous = conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
            conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(settings.telemetry_db_timeout_ms),))
            operation()
            conn.execute("SELECT set_config('statement_timeout',%s,true)", (previous,))
    except Exception as exc:
        logger.warning("Telemetry incomplete error=%s", type(exc).__name__)


def storage_bytes(conn: Any) -> int:
    """Измерить физический объём таблиц диагностики и их индексов."""
    return conn.execute(
        r"""
        SELECT sum(pg_total_relation_size(t::regclass)) AS n
        FROM unnest(%s::text[]) AS t
        """,
        (["request_log", "request_spans", "search_runs", "network_attempts"],),
    ).fetchone()["n"]


def register(conn: Any, group: int, inbox: int, environment: str) -> bool:
    """Создать диагностическую запись для существующего входящего задания."""
    if conn.execute("SELECT 1 FROM request_log WHERE id=%s", (identity(group, inbox),)).fetchone():
        return True
    row = conn.execute("SELECT * FROM bot_inbox WHERE group_id=%s AND id=%s", (group, inbox)).fetchone()
    if not row:
        return False
    message = row["payload"].get("message", {})
    text = message.get("text")
    charge = 2048 + len((text or "")[: settings.telemetry_query_max_chars].encode())
    if not make_room(conn, group, charge, identity(group, inbox)):
        dropped(conn, group)
        return False
    parent = row["payload"].get("_geo_source_id")
    source = "internal" if row["kind"] == "geo_retry" else row["payload"].get("_event_source", "unknown")
    # A deterministic pseudonym, not anonymisation. The database remains private.
    user = hashlib.sha256(f"{group}:{row['peer_id']}".encode()).hexdigest()
    conn.execute(
        r"""
        INSERT INTO request_log(id,
             group_id,
             inbox_id,
             parent_request_id,
             user_key,
             event_type,
             event_source,
             query_text,
             query_hash,
             query_truncated,
             occurred_at,
             received_at,
             environment,
             app_revision,
             recorded_bytes)
        VALUES(%s,
             %s,
             %s,
             %s,
             %s,
             %s,
             %s,
             %s,
             %s,
             %s,
             to_timestamp(%s),
             to_timestamp(%s),
             %s,
             %s,
             %s) ON CONFLICT(id) DO NOTHING
        """,
        (
            identity(group, inbox),
            group,
            inbox,
            identity(group, parent) if parent else None,
            user,
            "geo" if message.get("geo") else row["kind"],
            source,
            text[: settings.telemetry_query_max_chars] if text else None,
            hashlib.sha256(text.encode()).hexdigest() if text else None,
            bool(text and len(text) > settings.telemetry_query_max_chars),
            row["occurred_at"] if row["kind"] == "message_new" else None,
            row["created_at"],
            environment,
            settings.app_revision,
            charge,
        ),
    )
    return True


def budget_used(conn: Any, group: int) -> int:
    """Подсчитать начисленный объём диагностики сообщества."""
    return conn.execute(
        r"""
        SELECT coalesce(sum(recorded_bytes), 0) AS n
        FROM request_log
        WHERE group_id = %s
        """,
        (group,),
    ).fetchone()["n"]


def make_room(conn: Any, group: int, charge: int, exclude: uuid.UUID | None = None) -> bool:
    """Освободить только необходимую часть старой диагностики завершённых задач."""
    conn.execute("SELECT pg_advisory_xact_lock(20261107,%s)", (group,))
    if charge > settings.telemetry_max_storage_bytes:
        return False
    needed = budget_used(conn, group) + charge - settings.telemetry_max_storage_bytes
    if needed <= 0:
        return True
    rows = conn.execute(
        r"""
        SELECT r.id,
               r.recorded_bytes
        FROM request_log r
        WHERE r.group_id = %s
            AND r.id IS DISTINCT
            FROM %s
            AND NOT EXISTS
                (SELECT 1
                 FROM bot_inbox i
                 WHERE i.group_id = r.group_id
                     AND i.id = r.inbox_id
                     AND i.status <> 'done')
            AND NOT EXISTS
                (SELECT 1
                 FROM bot_outbox o
                 WHERE o.group_id = r.group_id
                     AND o.inbox_id = r.inbox_id
                     AND o.status <> 'done')
        ORDER BY r.updated_at,
                 r.id
        LIMIT %s
        FOR
        UPDATE OF r SKIP LOCKED
        """,
        (group, exclude, settings.telemetry_eviction_batch),
    ).fetchall()
    selected = []
    for row in rows:
        selected.append(row["id"])
        needed -= row["recorded_bytes"]
        if needed <= 0:
            break
    if selected:
        conn.execute("DELETE FROM request_log WHERE id=ANY(%s)", (selected,))
    return needed <= 0


def dropped(conn: Any, group: int) -> None:
    """Отметить пропуск части измерений из-за ограничения бюджета."""
    logger.warning("Telemetry storage budget reached; diagnostic records skipped")
    conn.execute(
        r"""
        INSERT INTO bot_state
        VALUES(%s,'telemetry_dropped','1'::JSONB) ON CONFLICT(group_id, name) DO
        UPDATE
        SET value = to_jsonb(bot_state.value::bigint + 1)
        """,
        (group,),
    )


def refresh(conn: Any, group: int, inbox: int) -> None:
    """Обновить диагностическую сводку по фактическому состоянию очереди."""
    row = conn.execute(
        r"""
        SELECT status,
               last_error,
               payload,
               completed_at,
               event_key
        FROM bot_inbox
        WHERE group_id = %s
            AND id = %s
        """,
        (group, inbox),
    ).fetchone()
    if not row:
        return
    effects = conn.execute(
        r"""
        SELECT id,
               METHOD,
               status,
               completed_at,
               context ->> 'status' AS result_status,
               coalesce(context -> 'data' <> '{}'::JSONB, FALSE) AS has_codes
        FROM bot_outbox
        WHERE group_id = %s
            AND inbox_id = %s
        ORDER BY id
        """,
        (group, inbox),
    ).fetchall()
    receipt = row["payload"].get("_queue_receipt", {})
    action = row["payload"].get("payload", {})
    if isinstance(action, str):
        action = json.loads(action)
    existing = conn.execute("SELECT outcome FROM request_log WHERE id=%s", (identity(group, inbox),)).fetchone()
    skipped = (existing or {}).get("outcome", {}).get("skipped_outbox_ids", [])
    disposition = row["payload"].get("_queue_disposition")
    if disposition:
        skipped = [e["id"] for e in effects]
    state = {
        "queue_disposition": disposition,
        "message_rate_delayed": bool(row["payload"].get("_message_rate_delayed")),
        "hint_suppressed": bool(row["payload"].get("_hint_suppressed")),
        "automatic_code": bool(row["payload"].get("_automatic_code")),
        "inbox_status": row["status"],
        "last_error": row["last_error"],
        "outbox_total": len(effects),
        "outbox_done": sum(e["status"] == "done" and e["id"] not in skipped for e in effects),
        "outbox_skipped": sum(e["id"] in skipped for e in effects),
        "outbox_failed": sum(e["status"] == "failed" for e in effects),
        "completed_at": row["completed_at"],
        "last_delivery_at": max((e["completed_at"] or 0 for e in effects if e["id"] not in skipped), default=0) or None,
        "throttled": bool(receipt.get("throttled")),
        "duplicate": bool(receipt.get("duplicate")),
        "processing_delay_ms": None,
        "effects": [
            {
                "outbox_id": e["id"],
                "method": e["method"],
                "status": e["status"],
                "role": (
                    "code"
                    if "entrance" in action or row["payload"].get("_automatic_code")
                    else (
                        "address_choices"
                        if e["result_status"] == "ambiguous"
                        else "entrance_buttons" if e["has_codes"] else "notice"
                    )
                ),
            }
            for e in effects[:100]
        ],
    }
    child = conn.execute(
        "SELECT status,id FROM bot_inbox WHERE group_id=%s AND event_key=%s", (group, "geo-retry:" + row["event_key"])
    ).fetchone()
    if child:
        state.update(geo_retry_request_id=identity(group, child["id"]), geo_retry_status=child["status"])
    started = conn.execute(
        r"""
        SELECT extract(epoch
                       FROM min(p.started_at)) AS t,
               extract(epoch
                       FROM min(r.received_at)) AS received
        FROM request_log r
        LEFT JOIN request_spans p ON p.request_id = r.id
        AND p.stage = 'inbox.process'
        WHERE r.id = %s
        """,
        (identity(group, inbox),),
    ).fetchone()
    if started and started["t"]:
        state["processing_delay_ms"] = max(0, (float(started["t"]) - float(started["received"])) * 1000)
    if any(e["status"] in {"pending", "processing"} for e in effects):
        state["delivery_status"] = "partial" if state["outbox_done"] else "pending"
    elif state["outbox_failed"]:
        state["delivery_status"] = "failed"
    else:
        state["delivery_status"] = "accepted_by_vk" if effects else "no_delivery"
        if state["outbox_skipped"]:
            state["delivery_status"] = "accepted_with_skips" if state["outbox_done"] else "cancelled"
    if disposition:
        state["delivery_status"] = "cancelled"
    if receipt.get("throttled"):
        conn.execute("UPDATE request_log SET telemetry_state='not_applicable' WHERE id=%s", (identity(group, inbox),))
    conn.execute(
        r"""
        UPDATE request_log
        SET outcome = outcome || %s,
            updated_at = now()
        WHERE id = %s
        """,
        (Jsonb(state), identity(group, inbox)),
    )


def persist(store: Any, queue: str, job: dict, packet: dict, environment: str) -> None:
    """Сохранить ограниченный пакет этапов попытки без отмены рабочей транзакции."""
    inbox = job["id"] if queue == "bot_inbox" else job["inbox_id"]
    with store.connect() as conn:

        def write() -> None:
            # Bound concurrent writers and charge a conservative live-data budget.
            """Записать подготовленное содержимое с сохранением границ текущей операции."""
            conn.execute("SELECT pg_advisory_xact_lock(20261107,%s)", (store.group_id,))
            charge = len(json.dumps(packet, ensure_ascii=False).encode()) + 2048 * len(packet["spans"])
            if not register(conn, store.group_id, inbox, environment):
                return
            rid = identity(store.group_id, inbox)
            if not make_room(conn, store.group_id, charge, rid):
                dropped(conn, store.group_id)
                conn.execute(
                    "UPDATE request_log SET telemetry_state='partial' WHERE id=%s", (identity(store.group_id, inbox),)
                )
                return
            spans, searches, attempts = [], [], []
            for row in packet["spans"]:
                attrs = row["attributes"]
                spans.append(
                    (
                        row["id"],
                        rid,
                        row["parent_id"],
                        queue,
                        job["id"],
                        job["attempts"],
                        row.get("service", "vkbot"),
                        row["stage"],
                        row["started_at"],
                        row["duration_ms"],
                        row["status"],
                        row.get("error_type"),
                        row.get("error_code"),
                        Jsonb(attrs),
                        row.get("instance_id", "unknown"),
                    ),
                )
                if row["kind"] == "search":
                    result = attrs.get("result", {})
                    searches.append(
                        (
                            row["id"],
                            attrs["engine"],
                            result.get("status", row["status"]),
                            attrs.get("query"),
                            (
                                json.dumps(packet["notes"].get("normalized_query"), ensure_ascii=False)
                                if attrs["engine"] == "v2"
                                else None
                            ),
                            result.get("building_id"),
                            result.get("address"),
                            Jsonb(result.get("candidates", [])),
                            Jsonb(attrs),
                        ),
                    )
                if row["kind"] == "network":
                    attempts.append(
                        (
                            row["id"],
                            attrs["target"],
                            attrs["operation"],
                            attrs["route"],
                            attrs["route_reason"],
                            attrs.get("attempt", 1),
                            attrs.get("http_status"),
                            attrs.get("provider_error_code"),
                            Jsonb(attrs),
                        ),
                    )
            with conn.cursor() as cursor:
                cursor.executemany(
                    r"""
                    INSERT INTO request_spans(id,
                         request_id,
                         parent_span_id,
                         queue,
                         job_id,
                         attempt,
                         service,
                         stage,
                         started_at,
                         duration_ms,
                         status,
                         error_type,
                         error_code,
                         attributes,
                         instance_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                    """,
                    spans,
                )
                if searches:
                    cursor.executemany(
                        r"""
                        INSERT INTO search_runs
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                        """,
                        searches,
                    )
                if attempts:
                    cursor.executemany(
                        r"""
                        INSERT INTO network_attempts
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                        """,
                        attempts,
                    )
            notes = packet["notes"]
            if queue == "bot_outbox" and notes.get("decision") == "delivery_obsolete":
                conn.execute(
                    r"""
                    UPDATE request_log
                    SET outcome = jsonb_set(outcome,
                         '{skipped_outbox_ids}',
                         coalesce(outcome -> 'skipped_outbox_ids',
                         '[]'::JSONB) || %s)
                    WHERE id = %s
                    """,
                    (Jsonb([job["id"]]), rid),
                )
            conn.execute(
                r"""
                UPDATE request_log
                SET outcome = outcome || %s,
                    telemetry_state = CASE
                                          WHEN telemetry_state = 'partial' THEN 'partial'
                                          ELSE %s
                                      END,
                                      updated_at = now(),
                                      recorded_bytes = recorded_bytes + %s
                WHERE id = %s
                """,
                (
                    Jsonb(notes),
                    "partial" if packet["dropped"] or notes.get("api_measurements", "ok") != "ok" else "recorded",
                    charge,
                    rid,
                ),
            )
            if notes.get("parent_request_id"):
                conn.execute(
                    "UPDATE request_log SET parent_request_id=%s WHERE id=%s", (notes["parent_request_id"], rid)
                )
            refresh(conn, store.group_id, inbox)

        guarded(conn, write)


def maintain(store: Any, batch: int) -> dict:
    """Освободить избыточную диагностику и обновить незавершённые сводки."""
    with store.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(20261107,%s)", (store.group_id,))
        size = storage_bytes(conn)
        # Refresh terminal transitions made without a worker (expiry, superseding).
        changed = conn.execute(
            r"""
            SELECT r.inbox_id
            FROM request_log r
            JOIN bot_inbox i ON i.id = r.inbox_id
            AND i.group_id = r.group_id
            WHERE r.group_id = %s
                AND (r.outcome ->> 'inbox_status' IS DISTINCT
                     FROM i.status
                     OR coalesce((r.outcome ->> 'outbox_total')::int, 0) <>
                         (SELECT count(*)
                          FROM bot_outbox o
                          WHERE o.inbox_id = i.id)
                     OR coalesce((r.outcome ->> 'outbox_done')::int,
                          0) + coalesce((r.outcome ->> 'outbox_skipped')::int,
                          0) <>
                         (SELECT count(*)
                          FROM bot_outbox o
                          WHERE o.inbox_id = i.id
                              AND o.status = 'done')
                     OR coalesce((r.outcome ->> 'outbox_failed')::int, 0) <>
                         (SELECT count(*)
                          FROM bot_outbox o
                          WHERE o.inbox_id = i.id
                              AND o.status = 'failed'))
            LIMIT %s
            """,
            (store.group_id, batch),
        ).fetchall()
        for row in changed:
            refresh(conn, store.group_id, row["inbox_id"])
            conn.execute(
                "UPDATE request_log SET telemetry_state='partial' WHERE id=%s",
                (identity(store.group_id, row["inbox_id"]),),
            )
        # Enforce a changed budget even when no new diagnostics arrive. Age alone
        # never removes history. Share the write-path eviction policy and lock.
        before = conn.execute("SELECT count(*) AS n FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()[
            "n"
        ]
        make_room(conn, store.group_id, 0)
        deleted = (
            before
            - conn.execute("SELECT count(*) AS n FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()["n"]
        )
        return {
            "bytes": size,
            "requests_removed": deleted,
            "estimated_live_bytes": budget_used(conn, store.group_id),
            "budget_reached": budget_used(conn, store.group_id) >= settings.telemetry_max_storage_bytes,
            "dropped_batches": (
                conn.execute(
                    r"""
            SELECT value
            FROM bot_state
            WHERE group_id = %s
                AND name = 'telemetry_dropped'
            """,
                    (store.group_id,),
                ).fetchone()
                or {}
            ).get("value", 0),
        }


def report(store: Any) -> dict:
    """Только агрегаты; текст запросов и идентификаторы пользователей не выводятся."""
    with store.connect() as conn:
        params = (store.group_id,)
        requests = conn.execute(
            r"""
            SELECT telemetry_state,
                   count(*) AS COUNT
            FROM request_log
            WHERE group_id = %s
            GROUP BY telemetry_state
            """,
            params,
        ).fetchall()
        searches = conn.execute(
            r"""
            SELECT s.engine,
                   s.result,
                   count(*) AS attempts,
                   percentile_cont(0.5) WITHIN GROUP(
                                                     ORDER BY p.duration_ms) AS p50_ms,
                                               percentile_cont(0.95) WITHIN GROUP(
                                                                                  ORDER BY p.duration_ms) AS p95_ms
            FROM search_runs s
            JOIN request_spans p ON p.id = s.span_id
            JOIN request_log r ON r.id = p.request_id
            WHERE r.group_id = %s
            GROUP BY s.engine,
                     s.result
            """,
            params,
        ).fetchall()
        routes = conn.execute(
            r"""
            SELECT n.target,
                   n.operation,
                   n.route,
                   n.route_reason,
                   p.status,
                   count(*) AS attempts,
                   percentile_cont(0.95) WITHIN GROUP(
                                                      ORDER BY p.duration_ms) AS p95_ms
            FROM network_attempts n
            JOIN request_spans p ON p.id = n.span_id
            JOIN request_log r ON r.id = p.request_id
            WHERE r.group_id = %s
            GROUP BY n.target,
                     n.operation,
                     n.route,
                     n.route_reason,
                     p.status
            """,
            params,
        ).fetchall()
        return {
            "schema_version": 1,
            "requests": requests,
            "searches": searches,
            "network": routes,
            "physical_bytes": storage_bytes(conn),
            "estimated_live_bytes": budget_used(conn, store.group_id),
            "budget_bytes": settings.telemetry_max_storage_bytes,
        }

"""Durable inbox/outbox. All identifiers are scoped to a VK community."""

import asyncio
import hashlib
import json
import logging
import math
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from src.infrastructure.database.schema import require_schema_async
from src.infrastructure.network import network_settings
from src.infrastructure.telemetry import active, measured


def event_record(group_id: int, kind: str, obj: dict, now: float | None = None) -> dict:
    """Проверить событие VK и построить устойчивый ключ дедупликации сообщества."""
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
    """Постоянная очередь событий, эффектов и состояния отдельного сообщества."""

    def __init__(
        self,
        dsn: str,
        group_id: int,
        lease_seconds: int = 120,
        telemetry: bool = False,
        *,
        max_pending_messages_per_peer: int | None = None,
        reply_interval_seconds: float = 0,
        message_rate_interval_seconds: float = 0,
        message_rate_burst: int = 2,
        hint_interval_seconds: float = 0,
    ):
        """Задать сообщество, аренду заданий и отдельные лимиты пользователя."""
        if (
            (max_pending_messages_per_peer is not None and max_pending_messages_per_peer < 1)
            or min(reply_interval_seconds, message_rate_interval_seconds, hint_interval_seconds) < 0
            or message_rate_burst < 1
        ):
            raise ValueError("Invalid per-user queue limits")
        self.dsn = dsn
        self.group_id = group_id
        self.lease_seconds = lease_seconds
        self.telemetry = telemetry
        self.environment = "unknown"
        # Runtime supplies the configured limits; maintenance clients need not.
        self.max_pending_messages_per_peer = max_pending_messages_per_peer
        self.reply_interval_seconds = reply_interval_seconds
        self.message_rate_interval_seconds = message_rate_interval_seconds
        self.message_rate_burst = message_rate_burst
        self.hint_interval_seconds = hint_interval_seconds
        self._database_slots = asyncio.Semaphore(network_settings.queue_db_max_concurrent)

    async def _queue_lock(self, conn: psycopg.AsyncConnection[DictRow]) -> None:
        """Сериализовать изменение связанных очередей одного сообщества."""
        await conn.execute(
            r"""
            SELECT
              pg_advisory_xact_lock(20261106, %s)
            """,
            (self.group_id,),
        )

    async def _trim_pending(self, conn: psycopg.AsyncConnection[DictRow], peer: int) -> set[int]:
        """Оставить актуальный хвост неначатых запросов одного пользователя."""
        if self.max_pending_messages_per_peer is None:
            return set()
        # Only whole, unstarted requests can be removed. An attempted or partly
        # delivered outbox must retain its stable random_id and remaining pages.
        rows = await (
            await conn.execute(
                r"""
            WITH waiting_ids AS (
              SELECT
                id
              FROM
                bot_inbox
              WHERE
                group_id = %s
                AND peer_id = %s
                AND kind = 'message_new'
                AND status = 'pending'
              UNION
              SELECT
                inbox_id
              FROM
                bot_outbox
              WHERE
                group_id = %s
                AND peer_id = %s
                AND status = 'pending'
            ),

            waiting AS (
              SELECT
                i.id,
                i.occurred_at,
                coalesce(
                  (i.payload #>> '{message,conversation_message_id}')::bigint,
                  (i.payload #>> '{message,id}')::bigint,
                  i.id
                ) AS message_order,

                jsonb_build_object(
                  'text',
                  btrim(regexp_replace(lower(coalesce(
                    i.payload #>> '{message,text}',
                    ''
                  )),
                  '\s+',
                  ' ',
                  'g')),
                  'geo',
                  i.payload #> '{message,geo}',
                  'attachments',
                  i.payload #> '{message,attachments}',
                  'payload',
                  i.payload #> '{message,payload}',
                  'reply',
                  i.payload #> '{message,reply_message}',
                  'forwarded',
                  i.payload #> '{message,fwd_messages}'
                ) AS signature
              FROM
                waiting_ids AS w
              INNER JOIN
                bot_inbox AS i
                ON i.id = w.id
              WHERE
                i.kind = 'message_new'
                AND (
                  i.status = 'pending'
                  OR (
                    i.status = 'done'
                    AND NOT EXISTS
                    (
                      SELECT
                        1
                      FROM
                        bot_outbox AS o
                      WHERE
                        o.inbox_id = i.id
                        AND (
                          o.status <> 'pending'
                          OR o.attempts > 0
                          OR o.result IS NOT NULL
                        )
                    )
                  )
                )
            ),

            duplicates AS (
              SELECT
                *,
                row_number() OVER (
                  PARTITION BY signature
                  ORDER BY
                    occurred_at DESC,
                    message_order DESC,
                    id DESC
                ) AS duplicate_order
              FROM
                waiting
            ),

            latest AS (
              SELECT
                id,
                row_number() OVER (
                  ORDER BY
                    occurred_at DESC,
                    message_order DESC,
                    id DESC
                ) AS POSITION
              FROM
                duplicates
              WHERE
                duplicate_order = 1
            ),

            obsolete AS (
              SELECT
                id,
                'superseded_duplicate' AS reason
              FROM
                duplicates
              WHERE
                duplicate_order > 1
              UNION ALL
              SELECT
                id,
                'pending_limit' AS reason
              FROM
                latest
              WHERE
                POSITION > %s
            )

            UPDATE bot_inbox i
            SET
              status = 'done',
              completed_at = %s,
              owner = NULL,
              lease_until = NULL,
              payload = jsonb_set(i.payload, '{_queue_disposition}', to_jsonb(o.reason))
            FROM
              obsolete AS o
            WHERE
              i.id = o.id
            RETURNING i.id
            """,
                (self.group_id, peer, self.group_id, peer, self.max_pending_messages_per_peer, time.time()),
            )
        ).fetchall()
        ids = {r["id"] for r in rows}
        if ids:
            await conn.execute(
                r"""
                UPDATE bot_outbox
                SET
                  status = 'done',
                  completed_at = %s
                WHERE
                  inbox_id = any(%s)
                  AND group_id = %s
                """,
                (time.time(), list(ids), self.group_id),
            )
            for inbox_id in ids:
                await self._observe(conn, inbox_id)
        return ids

    async def _reply_gate(self, conn: psycopg.AsyncConnection[DictRow], peer: int) -> float:
        """Рассчитать допуск следующего исходящего действия пользователя."""
        name = f"reply_gate:{peer}"
        row = await (
            await conn.execute(
                r"""
            SELECT
              value
            FROM
              bot_state
            WHERE
              group_id = %s AND name = %s
            """,
                (self.group_id, name),
            )
        ).fetchone()
        return float(row["value"]) if row else 0

    async def _claim_message_budget(self, conn: psycopg.AsyncConnection[DictRow], row: dict, now: float) -> bool:
        """До внешних вызовов; очередь сериализует бюджет отдельно для каждого peer."""
        name = f"message_rate:{row['peer_id']}"
        saved = await (
            await conn.execute(
                r"""
            SELECT
              value
            FROM
              bot_state
            WHERE
              group_id = %s AND name = %s
            """,
                (self.group_id, name),
            )
        ).fetchone()
        state = saved["value"] if saved else {}
        previous = state.get("updated_at", now)
        timestamp = max(now, previous)
        tokens = min(
            self.message_rate_burst,
            state.get("tokens", self.message_rate_burst) + (timestamp - previous) / self.message_rate_interval_seconds,
        )
        limited = tokens + 1e-9 < 1
        if limited or state.get("delayed") or row["payload"].get("_message_rate_delayed"):
            if limited:
                # Keep the trailing-request intent even when queue compaction removes its old row.
                await self._state(conn, name, {"tokens": tokens, "updated_at": timestamp, "delayed": True})
            latest = (
                await (
                    await conn.execute(
                        r"""
                SELECT
                  id
                FROM
                  bot_inbox
                WHERE
                  group_id = %s
                  AND peer_id = %s
                  AND kind = 'message_new'
                  AND status = 'pending'
                  AND attempts = 0
                ORDER BY
                  occurred_at DESC,
                  coalesce(
                    (payload #>> '{message,conversation_message_id}')::bigint,
                    (payload #>> '{message,id}')::bigint,
                    id
                  ) DESC,
                  id DESC
                LIMIT
                  1
                """,
                        (self.group_id, row["peer_id"]),
                    )
                ).fetchone()
            )["id"]
            cancelled = await (
                await conn.execute(
                    r"""
                UPDATE bot_inbox
                SET
                  status = 'done',
                  completed_at = %s,
                  payload = jsonb_set(payload, '{_queue_disposition}', '"message_rate_coalesced"'::JSONB)
                WHERE
                  group_id = %s
                  AND peer_id = %s
                  AND kind = 'message_new'
                  AND status = 'pending'
                  AND attempts = 0
                  AND id <> %s
                RETURNING id
                """,
                    (now, self.group_id, row["peer_id"], latest),
                )
            ).fetchall()
            for cancelled_row in cancelled:
                await self._observe(conn, cancelled_row["id"])
            if limited or latest != row["id"]:
                eligible = timestamp + (1 - tokens) * self.message_rate_interval_seconds if limited else now
                await conn.execute(
                    r"""
                    UPDATE bot_inbox
                    SET
                      next_at = %s,
                      payload = jsonb_set(payload, '{_message_rate_delayed}', 'true')
                    WHERE
                      id = %s
                    """,
                    (eligible, latest),
                )
                return False
        await self._state(conn, name, {"tokens": max(0, tokens - 1), "updated_at": timestamp})
        return True

    async def _allow_hint(self, conn: psycopg.AsyncConnection[DictRow], job: dict, index: int, effect: dict) -> bool:
        """Проверить, не повторяет ли подсказка уже ожидающую или недавнюю отправку."""
        name = f"message_hint:{job['peer_id']}"
        signature = hashlib.sha256(effect["params"]["message"].encode()).hexdigest()
        saved = await (
            await conn.execute(
                r"""
            SELECT
              value
            FROM
              bot_state
            WHERE
              group_id = %s AND name = %s
            """,
                (self.group_id, name),
            )
        ).fetchone()
        state = saved["value"] if saved else {}
        if state.get("signature") == signature:
            previous = await (
                await conn.execute(
                    r"""
                SELECT
                  status,
                  completed_at
                FROM
                  bot_outbox
                WHERE
                  inbox_id = %s
                  AND effect_index = %s
                  AND group_id = %s
                """,
                    (state["inbox_id"], state["effect_index"], self.group_id),
                )
            ).fetchone()
            if previous and (
                previous["status"] in {"pending", "processing"}
                or (
                    previous["status"] == "done"
                    and time.time() < (previous["completed_at"] or 0) + self.hint_interval_seconds
                )
            ):
                await conn.execute(
                    r"""
                    UPDATE bot_inbox
                    SET payload = jsonb_set(payload, '{_hint_suppressed}', 'true')
                    WHERE
                      id = %s
                    """,
                    (job["id"],),
                )
                return False
        await self._state(
            conn,
            name,
            {"signature": signature, "inbox_id": job["id"], "effect_index": index, "updated_at": time.time()},
        )
        return True

    async def _reserve_reply(self, conn: psycopg.AsyncConnection[DictRow], peer: int, now: float) -> None:
        """Зарезервировать интервал исходящего ответа в текущей транзакции."""
        if self.reply_interval_seconds:
            await self._state(
                conn,
                f"reply_gate:{peer}",
                max((await self._reply_gate(conn, peer)), now + self.reply_interval_seconds),
            )

    async def _observe(self, conn: psycopg.AsyncConnection[DictRow], inbox_id: int) -> None:
        # A measured worker flushes once after the operation. Ingest, failures
        # outside that scope and callers without Capture still snapshot here.
        """Сохранить диагностическую сводку задания вне измеряемой попытки."""
        if self.telemetry and not active():
            from src.vkbot.telemetry import guarded, refresh, register

            async def update() -> None:
                """Сохранить связанную сводку внутри текущей транзакции."""
                if await register(conn, self.group_id, inbox_id, self.environment):
                    await refresh(conn, self.group_id, inbox_id)
                    parent = await (
                        await conn.execute(
                            r"""
                        SELECT
                          payload ->> '_geo_source_id' AS id
                        FROM
                          bot_inbox
                        WHERE
                          id = %s
                          AND group_id = %s
                        """,
                            (inbox_id, self.group_id),
                        )
                    ).fetchone()
                    if parent and parent["id"]:
                        await refresh(conn, self.group_id, int(parent["id"]))

            await guarded(conn, update)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        """Открыть соединение очереди с ограничениями подключения, SQL и блокировок."""
        async with asyncio.timeout(network_settings.queue_db_connect_timeout_seconds):
            await self._database_slots.acquire()
        try:
            async with await psycopg.AsyncConnection.connect(self.dsn, **self._connection_options()) as conn:
                yield conn
        finally:
            self._database_slots.release()

    def _connection_options(self) -> dict:
        """Использовать одинаковые бюджеты очереди и отдельной guard-сессии."""
        return {
            "connect_timeout": network_settings.queue_db_connect_timeout_seconds,
            "row_factory": dict_row,
            "options": (
                f"-c statement_timeout={network_settings.queue_db_statement_timeout_ms} "
                f"-c lock_timeout={network_settings.queue_db_lock_timeout_ms}"
            ),
        }

    async def open_guard(self) -> psycopg.AsyncConnection[DictRow]:
        """Открыть асинхронное соединение для задач жизненного цикла runtime."""
        return await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, **self._connection_options())

    async def initialize(self) -> None:
        """Проверить схему и подготовить состояние сообщества без DDL."""
        async with self.connect() as conn:
            await require_schema_async(conn)
            if self.telemetry:
                from src.vkbot.telemetry import verify_schema

                try:
                    async with conn.transaction():
                        await verify_schema(conn)
                except Exception as exc:
                    self.telemetry = False
                    logging.getLogger(__name__).warning("Telemetry schema unavailable error=%s", type(exc).__name__)
            for table in ("bot_inbox", "bot_outbox"):
                await conn.execute(
                    rf"""
                    UPDATE {table}
                    SET completed_at = %s
                    WHERE
                      group_id = %s
                      AND status = 'done'
                      AND completed_at IS NULL
                    """,
                    (time.time(), self.group_id),
                )
            await conn.execute(
                r"""
                INSERT INTO bot_state
                VALUES (%s, 'tracking_started', %s) ON CONFLICT DO NOTHING
                """,
                (self.group_id, Jsonb(time.time())),
            )
            if self.max_pending_messages_per_peer is not None:
                await self._queue_lock(conn)
                peers = await (
                    await conn.execute(
                        r"""
                    SELECT
                      peer_id
                    FROM
                      bot_inbox
                    WHERE
                      group_id = %s
                      AND kind = 'message_new'
                      AND status = 'pending'
                    UNION
                    SELECT
                      peer_id
                    FROM
                      bot_outbox
                    WHERE
                      group_id = %s
                      AND status = 'pending'
                    """,
                        (self.group_id, self.group_id),
                    )
                ).fetchall()
                for row in peers:
                    await self._trim_pending(conn, row["peer_id"])

    async def get_state(self, name: str, default: Any = None) -> Any:
        """Прочитать именованное состояние сообщества с указанным значением по умолчанию."""
        async with self.connect() as conn:
            row = await (
                await conn.execute(
                    r"""
                SELECT
                  value
                FROM
                  bot_state
                WHERE
                  group_id = %s AND name = %s
                """,
                    (self.group_id, name),
                )
            ).fetchone()
            return row["value"] if row else default

    async def set_state(self, name: str, value: Any) -> None:
        """Записать именованное состояние сообщества отдельной транзакцией."""
        async with self.connect() as conn:
            await self._state(conn, name, value)

    async def _state(self, conn: psycopg.AsyncConnection[DictRow], name: str, value: Any) -> None:
        """Обновить именованное состояние в текущей транзакции."""
        await conn.execute(
            r"""
            INSERT INTO bot_state
            VALUES (%s, %s, %s) ON CONFLICT (group_id, name) DO
            UPDATE
              SET value = excluded.value
            """,
            (self.group_id, name, Jsonb(value)),
        )

    async def ingest(
        self,
        records: list[dict],
        cursor: str | None = None,
        callback_window: float = 5,
        notice_interval: float = 10,
        ack_interval: float = 1,
    ) -> int:
        """Атомарно сохранить новые события, дедупликацию и курсор Long Poll."""
        added = 0
        async with self.connect() as conn:
            # Serializes receipt compaction with both collector and history replay.
            await self._queue_lock(conn)
            peers = set()
            for record in records:
                assert record["group_id"] == self.group_id
                if await (
                    await conn.execute(
                        r"""
                    SELECT
                      1
                    FROM
                      bot_receipts
                    WHERE
                      group_id = %s AND event_key = %s
                    """,
                        (self.group_id, record["event_key"]),
                    )
                ).fetchone():
                    continue
                row = await (
                    await conn.execute(
                        r"""
                    INSERT INTO bot_inbox (
                      group_id,
                      event_key,
                      peer_id,
                      kind,
                      payload,
                      occurred_at,
                      created_at,
                      status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (
                      group_id, event_key
                    ) DO NOTHING RETURNING id
                    """,
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
                    )
                ).fetchone()
                if row and record["kind"] == "message_event":
                    try:
                        await self._register_callback(
                            conn,
                            record,
                            row["id"],
                            callback_window,
                            notice_interval,
                            ack_interval,
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        await conn.execute(
                            r"""
                            UPDATE bot_inbox SET status = 'failed', last_error = %s
                            WHERE
                              id = %s
                            """,
                            (safe_error(exc), row["id"]),
                        )
                added += bool(row)
                if row and record["kind"] == "message_new":
                    peers.add(record["peer_id"])
                    await conn.execute(
                        r"""
                        UPDATE bot_inbox
                        SET
                          status = 'done',
                          completed_at = %s
                        WHERE
                          group_id = %s
                          AND peer_id = %s
                          AND kind = 'geo_retry'
                          AND status IN (
                            'deferred',
                            'failed'
                          )
                          AND id < %s
                        """,
                        (time.time(), self.group_id, record["peer_id"], row["id"]),
                    )
                if row and record.get("status") == "done":
                    await conn.execute(
                        r"""
                        UPDATE bot_inbox SET completed_at = %s
                        WHERE
                          id = %s
                        """,
                        (time.time(), row["id"]),
                    )
                if row and self.telemetry:
                    await self._observe(conn, row["id"])
            for peer in sorted(peers):
                await self._trim_pending(conn, peer)
            if cursor is not None:
                await self._state(conn, "longpoll_ts", str(cursor))
        return added

    async def _register_callback(
        self,
        conn: psycopg.AsyncConnection[DictRow],
        record: dict,
        inbox_id: int,
        window: float,
        notice_interval: float,
        ack_interval: float,
    ) -> None:
        """Сохранить допуск нажатия и параметры краткого подтверждения."""
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
        await conn.execute(
            r"""
            INSERT INTO bot_state VALUES (%s, %s, '{}') ON CONFLICT DO NOTHING
            """,
            (self.group_id, name),
        )
        state = (
            await (
                await conn.execute(
                    r"""
            SELECT
              value
            FROM
              bot_state
            WHERE
              group_id = %s AND name = %s
            FOR UPDATE
            """,
                    (self.group_id, name),
                )
            ).fetchone()
        )["value"]
        now = time.time()
        previous = None
        if state.get("canonical_id"):
            previous = await (
                await conn.execute(
                    r"""
                SELECT
                  i.status,
                  i.last_error,
                  exists(
                    SELECT
                      1
                    FROM
                      bot_outbox AS o
                    WHERE
                      o.inbox_id = i.id
                      AND o.status IN (
                        'pending',
                        'processing'
                      )
                  ) AS active,
                  exists(
                    SELECT
                      1
                    FROM
                      bot_outbox AS o
                    WHERE
                      o.inbox_id = i.id
                      AND o.status = 'failed'
                  ) AS failed,
                  exists(
                    SELECT
                      1
                    FROM
                      bot_outbox AS o
                    WHERE
                      o.inbox_id = i.id
                      AND o.status IN (
                        'pending',
                        'processing'
                      )
                      AND o.last_error IS NOT NULL
                  ) AS delayed
                FROM
                  bot_inbox AS i
                WHERE
                  i.id = %s
                  AND i.group_id = %s
                """,
                    (state["canonical_id"], self.group_id),
                )
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
            blocked = (
                await (
                    await conn.execute(
                        r"""
                SELECT
                  exists(
                    SELECT
                      1
                    FROM
                      bot_outbox
                    WHERE
                      group_id = %s
                      AND peer_id = %s
                      AND status IN (
                        'pending',
                        'processing'
                      )
                      AND last_error IS NOT NULL
                  ) AS blocked
                """,
                        (self.group_id, record["peer_id"]),
                    )
                ).fetchone()
            )["blocked"]
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
        await conn.execute(
            r"""
            UPDATE bot_inbox SET payload = %s, status = %s, completed_at = %s
            WHERE
              id = %s
            """,
            (
                Jsonb(stored),
                "done" if throttled else record.get("status", "pending"),
                now if throttled else None,
                inbox_id,
            ),
        )
        await self._state(conn, name, state)

    async def callback_feedback(self, event_key: str) -> dict | None:
        # Reserve once before touching VK; ephemeral acks are never retried.
        """Однократно зарезервировать краткое подтверждение сохранённого нажатия."""
        async with self.connect() as conn:
            row = await (
                await conn.execute(
                    r"""
                SELECT
                  id,
                  payload
                FROM
                  bot_inbox
                WHERE
                  group_id = %s
                  AND event_key = %s
                FOR
                UPDATE
                """,
                    (self.group_id, event_key),
                )
            ).fetchone()
            if not row:
                return None
            obj = row["payload"]
            receipt = obj.get("_queue_receipt", {})
            if receipt.get("ack_reserved") or receipt.get("feedback", "skip") == "skip":
                return None
            receipt["ack_reserved"] = True
            await conn.execute(
                r"""
                UPDATE bot_inbox SET payload = %s
                WHERE
                  id = %s
                """,
                (Jsonb(obj), row["id"]),
            )
            return dict(receipt, inbox_id=row["id"])

    @staticmethod
    def _table(table: str) -> str:
        """Допустить только известные таблицы очереди перед подстановкой имени в SQL."""
        if table not in {"bot_inbox", "bot_outbox"}:
            raise ValueError("Invalid queue table")
        return table

    async def claim(self, table: str) -> dict | None:
        """Взять доступное задание с арендой и учётом порядка пользователя."""
        table = self._table(table)
        now = time.time()
        owner = str(uuid.uuid4())
        async with self.connect() as conn:
            if (
                self.max_pending_messages_per_peer is not None
                or self.reply_interval_seconds
                or self.message_rate_interval_seconds
            ):
                await self._queue_lock(conn)
            if table == "bot_inbox":
                await conn.execute(
                    r"""
                    UPDATE bot_inbox i
                    SET
                      status = 'done',
                      completed_at = %s
                    WHERE
                      i.group_id = %s
                      AND i.status = 'deferred'
                      AND EXISTS
                      (
                        SELECT
                          1
                        FROM
                          bot_inbox AS newer
                        WHERE
                          newer.group_id = i.group_id
                          AND newer.peer_id = i.peer_id
                          AND newer.kind = 'message_new'
                          AND newer.id > (i.payload ->> '_geo_source_id')::bigint
                      )
                    """,
                    (now, self.group_id),
                )
                await conn.execute(
                    r"""
                    UPDATE bot_inbox
                    SET
                      status = 'failed',
                      last_error = %s
                    WHERE
                      group_id = %s
                      AND status = 'deferred'
                      AND (payload ->> '_geo_deadline')::double precision <= %s
                    """,
                    (json.dumps({"type": "GeoRetryExpired"}), self.group_id, now),
                )
            while True:
                row = await (
                    await conn.execute(
                        rf"""
                    SELECT
                      q.*
                    FROM
                      {table} AS q
                    WHERE
                      q.group_id = %s
                      AND (
                        (q.status IN ('pending', 'deferred') AND q.next_at <= %s)
                        OR (q.status = 'processing' AND q.lease_until < %s)
                      )
                      AND NOT EXISTS (
                        SELECT
                          1
                        FROM
                          {table} AS older
                        WHERE
                          older.group_id = q.group_id
                          AND older.peer_id = q.peer_id
                          AND older.id < q.id
                          AND older.status IN ('pending', 'processing')
                      )
                    ORDER BY
                      q.next_at,
                      q.id
                    FOR UPDATE OF q SKIP LOCKED
                    LIMIT
                      1
                    """,
                        (self.group_id, now, now),
                    )
                ).fetchone()
                if not row:
                    return None
                removed = await self._trim_pending(conn, row["peer_id"])
                if row["id" if table == "bot_inbox" else "inbox_id"] in removed:
                    continue
                if (
                    table == "bot_inbox"
                    and row["kind"] == "message_new"
                    and row["attempts"] == 0
                    and self.message_rate_interval_seconds
                    and not (await self._claim_message_budget(conn, row, now))
                ):
                    # The delayed user's row is now in the future; immediately try another user.
                    continue
                break
            now = time.time()
            if table == "bot_outbox" and self.reply_interval_seconds:
                eligible = await self._reply_gate(conn, row["peer_id"])
                if eligible > now:
                    await conn.execute(
                        r"""
                        UPDATE bot_outbox
                        SET
                          status = 'pending',
                          owner = NULL,
                          lease_until = NULL,
                          next_at = %s
                        WHERE
                          id = %s
                        """,
                        (eligible, row["id"]),
                    )
                    return None
                await self._reserve_reply(conn, row["peer_id"], now)
            await conn.execute(
                rf"""
                UPDATE {table}
                SET
                  status = 'processing',
                  owner = %s,
                  lease_until = %s,
                  attempts = attempts + 1
                WHERE
                  id = %s
                """,
                (owner, now + self.lease_seconds, row["id"]),
            )
            row.update(owner=owner, attempts=row["attempts"] + 1)
            return row

    async def renew(self, table: str, job: dict) -> bool:
        """Продлить ещё действующую аренду задания текущего worker."""
        table = self._table(table)
        async with self.connect() as conn:
            return bool(
                await (
                    await conn.execute(
                        rf"""
                UPDATE {table}
                SET lease_until = %s
                WHERE
                  id = %s
                  AND group_id = %s
                  AND owner = %s
                  AND status = 'processing'
                RETURNING id
                """,
                        (time.time() + self.lease_seconds, job["id"], self.group_id, job["owner"]),
                    )
                ).fetchone()
            )

    @measured("outbox.persist")
    async def plan(self, job: dict, effects: list[dict], callback_at: float | None = None) -> None:
        """Атомарно сохранить эффекты, контекст и завершение входящего задания."""
        async with self.connect() as conn:
            if self.max_pending_messages_per_peer is not None or self.hint_interval_seconds:
                await self._queue_lock(conn)
            await self._owned(conn, "bot_inbox", job)
            if job["kind"] == "geo_retry" and (await self._geo_superseded(conn, job["peer_id"], job["payload"])):
                effects = []
            if any(effect.get("automatic_code") for effect in effects):
                await conn.execute(
                    r"""
                    UPDATE bot_inbox
                    SET payload = jsonb_set(payload, '{_automatic_code}', 'true')
                    WHERE
                      id = %s
                    """,
                    (job["id"],),
                )
            for index, effect in enumerate(effects):
                if (
                    effect.get("hint")
                    and self.hint_interval_seconds
                    and not (await self._allow_hint(conn, job, index, effect))
                ):
                    continue
                if effect.get("defer_geo"):
                    retry = effect["defer_geo"]
                    payload = dict(
                        job["payload"],
                        _geo_source_id=job["id"],
                        _geo_deadline=time.time() + retry["max_age"],
                    )
                    await conn.execute(
                        r"""
                        INSERT INTO bot_inbox (
                          group_id,
                          event_key,
                          peer_id,
                          kind,
                          payload,
                          occurred_at,
                          created_at,
                          status,
                          next_at
                        )
                        VALUES (
                          %s, %s, %s, 'geo_retry', %s, %s, %s, 'deferred', %s
                        ) ON CONFLICT DO NOTHING
                        """,
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
                    await self._state(conn, f"choices_closed:{job['peer_id']}:{cmid}", True)
                await conn.execute(
                    r"""
                    INSERT INTO bot_outbox (inbox_id, group_id, peer_id, effect_index, METHOD, params, context)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (
                      inbox_id, effect_index
                    ) DO NOTHING
                    """,
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
                await self._state(conn, f"callback:{job['peer_id']}", callback_at)
            await conn.execute(
                r"""
                UPDATE bot_inbox
                SET
                  status = 'done',
                  owner = NULL,
                  lease_until = NULL,
                  last_error = NULL,
                  completed_at = %s
                WHERE
                  id = %s
                """,
                (time.time(), job["id"]),
            )
            if not effects:
                await self._callback_completed(conn, job["peer_id"], job["id"])
            await self._observe(conn, job["id"])
            await self._trim_pending(conn, job["peer_id"])

    async def _geo_current(self, conn: psycopg.AsyncConnection[DictRow], peer: int, payload: dict) -> bool:
        """Проверить актуальность геозапроса внутри рабочей транзакции."""
        if time.time() >= payload["_geo_deadline"]:
            return False
        return not (
            await (
                await conn.execute(
                    r"""
            SELECT
              1
            FROM
              bot_inbox
            WHERE
              group_id = %s
              AND peer_id = %s
              AND kind = 'message_new'
              AND id > %s
            LIMIT
              1
            """,
                    (self.group_id, peer, payload["_geo_source_id"]),
                )
            ).fetchone()
        )

    async def geo_retry_current(self, job: dict) -> bool:
        """Проверить, не заменён ли отложенный геозапрос новым сообщением."""
        async with self.connect() as conn:
            return await self._geo_current(conn, job["peer_id"], job["payload"])

    async def outbox_geo_current(self, job: dict) -> bool:
        """Не допустить доставку устаревшего ответа на геометку."""
        async with self.connect() as conn:
            parent = await (
                await conn.execute(
                    r"""
                SELECT
                  kind,
                  payload
                FROM
                  bot_inbox
                WHERE
                  group_id = %s AND id = %s
                """,
                    (self.group_id, job["inbox_id"]),
                )
            ).fetchone()
            return parent["kind"] != "geo_retry" or not (
                await self._geo_superseded(conn, job["peer_id"], parent["payload"])
            )

    async def defer_geo_retry(self, job: dict, delay: float) -> None:
        """Сохранить ограниченный повтор геолокации без блокировки нового текста."""
        async with self.connect() as conn:
            await self._owned(conn, "bot_inbox", job)
            current = await self._geo_current(conn, job["peer_id"], job["payload"])
            expired = not current and not (await self._geo_superseded(conn, job["peer_id"], job["payload"]))
            await conn.execute(
                r"""
                UPDATE bot_inbox
                SET
                  status = %s,
                  next_at = %s,
                  owner = NULL,
                  lease_until = NULL,
                  completed_at = %s,
                  last_error = %s
                WHERE
                  id = %s
                """,
                (
                    "deferred" if current else ("failed" if expired else "done"),
                    min(time.time() + delay, job["payload"]["_geo_deadline"]),
                    None if current or expired else time.time(),
                    json.dumps({"type": "GeoRetryExpired"}) if expired else None,
                    job["id"],
                ),
            )
            await self._observe(conn, job["id"])

    async def _geo_superseded(self, conn: psycopg.AsyncConnection[DictRow], peer: int, payload: dict) -> bool:
        """Проверить замену геометки более новым пользовательским запросом."""
        return bool(
            await (
                await conn.execute(
                    r"""
            SELECT
              1
            FROM
              bot_inbox
            WHERE
              group_id = %s
              AND peer_id = %s
              AND kind = 'message_new'
              AND id > %s
            LIMIT
              1
            """,
                    (self.group_id, peer, payload["_geo_source_id"]),
                )
            ).fetchone()
        )

    async def _owned(self, conn: psycopg.AsyncConnection[DictRow], table: str, job: dict) -> None:
        """Подтвердить владение действующей арендой и заблокировать строку задания."""
        row = await (
            await conn.execute(
                rf"""
            SELECT
              id
            FROM
              {table}
            WHERE
              id = %s
              AND group_id = %s
              AND owner = %s
              AND status = 'processing'
              AND lease_until > %s
            FOR
            UPDATE
            """,
                (job["id"], self.group_id, job["owner"], time.time()),
            )
        ).fetchone()
        if not row:
            raise RuntimeError("Queue lease lost")

    async def save_result(self, job: dict, result: Any) -> None:
        """Сохранить промежуточный результат за владельцем аренды."""
        async with self.connect() as conn:
            await self._owned(conn, "bot_outbox", job)
            await conn.execute(
                r"""
                UPDATE bot_outbox SET result = %s
                WHERE
                  id = %s
                """,
                (Jsonb(result), job["id"]),
            )

    @measured("outbox.finish")
    async def finish(self, job: dict, context: tuple[int, dict] | None = None) -> None:
        """Завершить арендованное задание и сохранить подтверждённый результат."""
        async with self.connect() as conn:
            if self.reply_interval_seconds:
                await self._queue_lock(conn)
            await self._owned(conn, "bot_outbox", job)
            if context:
                cmid, result = context
                if result.get("_reopen_choices") is True:
                    result = dict(result)
                    result.pop("_reopen_choices")
                    if result.get("status") == "ambiguous" and result.get("candidates"):
                        await self._state(conn, f"choices_closed:{job['peer_id']}:{cmid}", False)
                await conn.execute(
                    r"""
                    INSERT INTO bot_message_context (group_id, peer_id, conversation_message_id, RESULT, updated_at)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (
                      group_id, peer_id, conversation_message_id
                    ) DO
                    UPDATE
                      SET
                        RESULT = excluded.result,
                        updated_at = excluded.updated_at
                    """,
                    (self.group_id, job["peer_id"], cmid, Jsonb(result), time.time()),
                )
            await conn.execute(
                r"""
                UPDATE bot_outbox
                SET
                  status = 'done',
                  owner = NULL,
                  lease_until = NULL,
                  last_error = NULL,
                  completed_at = %s
                WHERE
                  id = %s
                """,
                (time.time(), job["id"]),
            )
            await self._callback_completed(conn, job["peer_id"], job["inbox_id"])
            await self._reserve_reply(conn, job["peer_id"], time.time())
            await self._observe(conn, job["inbox_id"])

    async def _callback_completed(self, conn: psycopg.AsyncConnection[DictRow], peer: int, inbox_id: int) -> None:
        """Обновить состояние ограничения после завершения канонического нажатия."""
        name = f"callback_gate:{peer}"
        row = await (
            await conn.execute(
                r"""
            SELECT
              value
            FROM
              bot_state
            WHERE
              group_id = %s AND name = %s
            FOR UPDATE
            """,
                (self.group_id, name),
            )
        ).fetchone()
        if not row or row["value"].get("canonical_id") != inbox_id:
            return
        pending = await (
            await conn.execute(
                r"""
            SELECT
              1
            FROM
              bot_outbox
            WHERE
              inbox_id = %s
              AND status IN (
                'pending',
                'processing'
              )
            LIMIT
              1
            """,
                (inbox_id,),
            )
        ).fetchone()
        if not pending:
            state = row["value"]
            state["completed_at"] = time.time()
            await self._state(conn, name, state)

    async def fail(self, table: str, job: dict, error: str, delay: float, permanent: bool = False) -> None:
        """Сохранить ошибку и назначить повтор либо окончательный статус задания."""
        table = self._table(table)
        async with self.connect() as conn:
            if self.reply_interval_seconds:
                await self._queue_lock(conn)
            changed = (
                await conn.execute(
                    rf"""
                UPDATE {table}
                SET
                  status = %s,
                  next_at = %s,
                  last_error = %s,
                  owner = NULL,
                  lease_until = NULL
                WHERE
                  id = %s
                  AND group_id = %s
                  AND owner = %s
                  AND status = 'processing'
                """,
                    (
                        "failed" if permanent else "pending",
                        time.time() + delay,
                        error[:300],
                        job["id"],
                        self.group_id,
                        job["owner"],
                    ),
                )
            ).rowcount
            if changed and table == "bot_outbox":
                await self._reserve_reply(conn, job["peer_id"], time.time())
            await self._observe(conn, job["id"] if table == "bot_inbox" else job["inbox_id"])

    async def message_context(self, peer: int, cmid: int) -> dict | None:
        """Прочитать сохранённый результат и навигацию сообщения."""
        async with self.connect() as conn:
            row = await (
                await conn.execute(
                    r"""
                SELECT
                  RESULT
                FROM
                  bot_message_context
                WHERE
                  group_id = %s
                  AND peer_id = %s
                  AND conversation_message_id = %s
                """,
                    (self.group_id, peer, cmid),
                )
            ).fetchone()
            return row["result"] if row else None

    async def choices_closed(self, peer: int, cmid: int) -> bool:
        """Проверить, закрыт ли список адресов данного сообщения."""
        return (await self.get_state(f"choices_closed:{peer}:{cmid}", False)) is True

    async def consume_fault(self, name: str) -> bool:
        """Израсходовать одно разрешённое dev-срабатывание подстановки отказа."""
        async with self.connect() as conn:
            row = await (
                await conn.execute(
                    r"""
                SELECT
                  value
                FROM
                  bot_state
                WHERE
                  group_id = %s AND name = %s
                FOR UPDATE
                """,
                    (self.group_id, "fault:" + name),
                )
            ).fetchone()
            if not row or not row["value"]:
                return False
            count = int(row["value"])
            if count > 0:
                await self._state(conn, "fault:" + name, count - 1)
            return True

    async def stats(self) -> dict:
        """Собрать количества заданий по состояниям текущего сообщества."""
        result = {}
        async with self.connect() as conn:
            for table in ("bot_inbox", "bot_outbox"):
                result[table] = await (
                    await conn.execute(
                        rf"""
                    SELECT
                      status,
                      count(*) AS COUNT
                    FROM
                      {table}
                    WHERE
                      group_id = %s
                    GROUP BY
                      status
                    """,
                        (self.group_id,),
                    )
                ).fetchall()
            result["last_reconciliation"] = await self.get_state("reconcile_watermark")
        return result

    async def retry_failed(self, geo_max_age: float = 1800) -> None:
        """Вернуть допустимые ошибочные задания в очередь для ручного повтора."""
        async with self.connect() as conn:
            await conn.execute(
                r"""
                UPDATE bot_inbox
                SET payload = jsonb_set(payload, '{_geo_deadline}', %s)
                WHERE
                  group_id = %s
                  AND kind = 'geo_retry'
                  AND status = 'failed'
                """,
                (Jsonb(time.time() + geo_max_age), self.group_id),
            )
            for table in ("bot_inbox", "bot_outbox"):
                await conn.execute(
                    rf"""
                    UPDATE {table}
                    SET
                      status = 'pending',
                      next_at = 0,
                      last_error = NULL
                    WHERE
                      group_id = %s
                      AND status = 'failed'
                    """,
                    (self.group_id,),
                )


def retry_delay(attempt: int, base: float, maximum: float) -> float:
    """Рассчитать ограниченную задержку следующей попытки с разбросом."""
    return min(maximum, base * 2 ** min(attempt - 1, 12) * random.uniform(0.8, 1.2))


def safe_error(exc: Exception) -> str:
    # Never store request URLs, tokens, message text, or API error payloads.
    """Получить безопасное описание ошибки без сетевых секретов."""
    code = getattr(exc, "code", None)
    return json.dumps({"type": type(exc).__name__, "vk_code": code})

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll

from src.network import network_settings
from src.telemetry import (
    Capture,
    note,
    settings as telemetry_settings,
    span,
)
from src.vkbot.processor import Processor, RateGate, RateSession
from src.vkbot.queue import QueueStore, event_record, retry_delay, safe_error
from src.vkbot.recovery import Reconciler

logger = logging.getLogger(__name__)


def validate_environment(settings: Any) -> None:
    if str(settings.vk_group_id) != str(settings.expected_vk_group_id):
        raise ValueError("VK_GROUP_ID does not match EXPECTED_VK_GROUP_ID")
    if settings.dev_faults_enabled and (
        settings.app_environment != "development" or str(settings.vk_group_id) != "223949627"
    ):
        raise ValueError("Fault injection is restricted to the dedicated development community")
    if settings.queue_workers < 1 or settings.queue_lease_seconds < 3:
        raise ValueError("Invalid queue worker or lease configuration")


class Runtime:
    def __init__(self, settings: Any):
        validate_environment(settings)
        self.settings = settings
        self.store = QueueStore(
            settings.queue_database_url,
            int(settings.vk_group_id),
            settings.queue_lease_seconds,
            telemetry=telemetry_settings.telemetry_enabled,
            max_pending_messages_per_peer=getattr(settings, "queue_max_pending_messages_per_peer", 3),
            reply_interval_seconds=getattr(settings, "outbox_peer_min_interval_seconds", 5),
        )
        self.store.environment = settings.app_environment
        self.gate = RateGate(settings.vk_min_interval_seconds)

    def verify_token(self) -> None:
        # The token must be authorized for the configured community before any
        # history reads or sends can happen, including on a misconfigured dev.
        session = RateSession(self.gate)
        try:
            vk = VkApi(token=self.settings.vk_group_token, api_version="5.199", session=session)
            vk.method("groups.getLongPollServer", {"group_id": self.store.group_id})
        finally:
            session.close()

    async def run(self) -> None:
        await asyncio.to_thread(self.verify_token)
        await asyncio.to_thread(self.store.initialize)
        # One collector/runtime per community. This is an operator-controlled
        # standby, not an automatic two-server failover implementation.
        guard = await asyncio.to_thread(self.store.connect)
        guard.autocommit = True
        row = await asyncio.to_thread(
            lambda: guard.execute(
                "SELECT pg_try_advisory_lock(20261021,%s) AS locked", (self.store.group_id,)
            ).fetchone()
        )
        if not row["locked"]:
            guard.close()
            raise RuntimeError("Another runtime already owns this VK community")
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._guard(guard))
                tasks.create_task(self._collect())
                tasks.create_task(self._reconcile())
                tasks.create_task(self._maintenance())
                for index in range(self.settings.queue_workers):
                    tasks.create_task(self._worker("bot_inbox", index))
                    tasks.create_task(self._worker("bot_outbox", index))
        finally:
            guard.close()

    async def _maintenance(self) -> None:
        from src.vkbot.maintenance import compact, health

        while True:
            try:
                result = await asyncio.to_thread(
                    compact,
                    self.store,
                    self.settings.queue_retention_days * 86400,
                    self.settings.queue_context_retention_days * 86400,
                    self.settings.queue_maintenance_batch,
                )
                snapshot = await asyncio.to_thread(health, self.store)
                if self.store.telemetry:
                    from src.vkbot.telemetry import maintain

                    snapshot["telemetry"] = await asyncio.to_thread(
                        maintain, self.store, self.settings.queue_maintenance_batch
                    )
                logger.info("Queue maintenance result=%s health=%s", result, snapshot)
                import shutil

                free = shutil.disk_usage(".").free
                stale = any(
                    row["status"] in {"pending", "processing"}
                    and row["oldest_age_seconds"] > self.settings.queue_oldest_pending_warn_seconds
                    for rows in snapshot["queues"].values()
                    for row in rows
                )
                failed = any(row["status"] == "failed" for rows in snapshot["queues"].values() for row in rows)
                if (
                    free < self.settings.queue_disk_free_min_bytes
                    or snapshot["database_bytes"] > self.settings.queue_database_warn_bytes
                    or stale
                    or failed
                    or snapshot.get("telemetry", {}).get("dropped_batches", 0)
                ):
                    logger.warning(
                        "Operational warning free_bytes=%s database_bytes=%s stale_jobs=%s failed_jobs=%s",
                        free,
                        snapshot["database_bytes"],
                        stale,
                        failed,
                    )
            except Exception as exc:
                logger.warning("Queue maintenance unavailable error=%s", safe_error(exc))
            await asyncio.sleep(self.settings.queue_maintenance_interval_seconds)

    async def _guard(self, connection: Any) -> None:
        while True:
            await asyncio.sleep(self.settings.queue_guard_interval_seconds)
            await asyncio.to_thread(lambda: connection.execute("SELECT 1").fetchone())

    def _longpoll(self) -> VkBotLongPoll:
        vk = VkApi(token=self.settings.vk_group_token, api_version="5.199", session=RateSession(self.gate))
        poll = VkBotLongPoll(vk=vk, group_id=self.store.group_id, wait=network_settings.vk_longpoll_wait_seconds)
        poll.session.close()
        # Long Poll supplies its own wait+10 timeout; API calls share the rate gate.
        poll.session = RateSession(self.gate, longpoll=True)
        cursor = self.store.get_state("longpoll_ts")
        if cursor is not None:
            poll.ts = cursor
        return poll

    def _acknowledge(self, records: list[dict]) -> None:
        # Ephemeral acknowledgement is best effort and only follows durable ingest.
        session = RateSession(self.gate)
        try:
            vk = VkApi(token=self.settings.vk_group_token, api_version="5.199", session=session).get_api()
            for record in records:
                if record["kind"] != "message_event":
                    continue
                if self.store.get_state("vk_paused_until", 0) > time.time():
                    break
                receipt = self.store.callback_feedback(record["event_key"])
                if receipt is None:
                    continue
                obj = record["payload"]
                params = {"event_id": obj["event_id"], "user_id": obj["user_id"], "peer_id": obj["peer_id"]}
                if receipt["feedback"] != "silent":
                    text = "Запрос сохранён, повторно нажимать не нужно."
                    if receipt["feedback"] == "wait":
                        text = f"Подождите {receipt['wait_seconds']} сек."
                    params["event_data"] = json.dumps({"type": "show_snackbar", "text": text}, ensure_ascii=False)
                try:
                    if self.store.telemetry is True:
                        self._observed_job(
                            lambda job, params=params: vk.messages.sendMessageEventAnswer(**params),
                            "callback_ack",
                            {"id": receipt["inbox_id"], "inbox_id": receipt["inbox_id"], "attempts": 1},
                        )
                    else:
                        vk.messages.sendMessageEventAnswer(**params)
                except Exception as exc:
                    logger.info("Callback acknowledgement unavailable error=%s", safe_error(exc))
                    if getattr(exc, "code", None) in {6, 9, 29}:
                        self.store.set_state("vk_paused_until", time.time() + self.settings.vk_error_pause_seconds)
                        break
        finally:
            session.close()

    async def _collect(self) -> None:
        poll = None
        while True:
            try:
                if poll is None:
                    poll = await asyncio.to_thread(self._longpoll)
                events = await asyncio.to_thread(poll.check)
                records = []
                for event in events:
                    kind = event.type.value if hasattr(event.type, "value") else str(event.type)
                    if kind in {"message_new", "message_event"}:
                        try:
                            record = event_record(self.store.group_id, kind, dict(event.obj))
                            record["payload"]["_event_source"] = "longpoll"
                            records.append(record)
                        except (KeyError, ValueError):
                            logger.warning("Unsupported incoming event kind=%s", kind)
                # Do not fetch the next batch until this batch AND cursor are durable.
                while True:
                    try:
                        count = await asyncio.to_thread(
                            self.store.ingest,
                            records,
                            str(poll.ts),
                            callback_window=self.settings.anti_spam_time,
                            notice_interval=self.settings.callback_notice_interval_seconds,
                            ack_interval=self.settings.callback_ack_interval_seconds,
                        )
                        if count:
                            logger.info("Stored incoming events count=%d", count)
                        break
                    except Exception as exc:
                        logger.error("Inbox persistence unavailable error=%s", safe_error(exc))
                        await asyncio.sleep(network_settings.bot_restart_delay_seconds)
                if any(record["kind"] == "message_event" for record in records):
                    await asyncio.to_thread(self._acknowledge, records)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Long Poll unavailable error=%s", safe_error(exc))
                await asyncio.sleep(network_settings.bot_restart_delay_seconds)

    async def _reconcile(self) -> None:
        processor = await asyncio.to_thread(Processor, self.settings, self.store, self.gate)
        reconciler = Reconciler(processor.vk, self.store, self.settings)
        try:
            while True:
                try:
                    added = await asyncio.to_thread(reconciler.run)
                    logger.info("History reconciliation complete inserted=%d", added)
                except Exception as exc:
                    logger.warning("History reconciliation deferred error=%s", safe_error(exc))
                await asyncio.sleep(self.settings.recovery_interval_seconds)
        finally:
            processor.close()

    async def _heartbeat(self, table: str, job: dict) -> None:
        while True:
            await asyncio.sleep(max(self.settings.queue_heartbeat_min_seconds, self.settings.queue_lease_seconds / 3))
            if not await asyncio.to_thread(self.store.renew, table, job):
                return

    async def _worker(self, table: str, index: int) -> None:
        processor = await asyncio.to_thread(Processor, self.settings, self.store, self.gate)
        try:
            while True:
                try:
                    if await asyncio.to_thread(self.store.get_state, "paused", False):
                        await asyncio.sleep(self.settings.queue_poll_seconds)
                        continue
                    paused_until = await asyncio.to_thread(self.store.get_state, "vk_paused_until", 0)
                    if time.time() < paused_until:
                        await asyncio.sleep(self.settings.queue_poll_seconds)
                        continue
                    job = await asyncio.to_thread(self.store.claim, table)
                    if not job:
                        await asyncio.sleep(self.settings.queue_poll_seconds)
                        continue
                    heartbeat = asyncio.create_task(self._heartbeat(table, job))
                    try:
                        operation = processor.plan if table == "bot_inbox" else processor.deliver
                        await asyncio.to_thread(self._observed_job, operation, table, job)
                        logger.info("Queue job complete queue=%s id=%s worker=%s", table, job["id"], index)
                    except Exception as exc:
                        code = getattr(exc, "code", None)
                        permanent = isinstance(exc, (ValueError, KeyError, TypeError)) or code in {
                            7,
                            15,
                            100,
                            901,
                            902,
                            909,
                            914,
                            917,
                        }
                        delay = retry_delay(
                            job["attempts"],
                            self.settings.queue_retry_base_seconds,
                            self.settings.queue_retry_max_seconds,
                        )
                        await asyncio.to_thread(self.store.fail, table, job, safe_error(exc), delay, permanent)
                        if code in {5, 6, 9, 27, 29}:
                            await asyncio.to_thread(
                                self.store.set_state,
                                "vk_paused_until",
                                time.time() + max(self.settings.vk_error_pause_seconds, delay),
                            )
                        logger.warning(
                            "Queue job deferred queue=%s id=%s permanent=%s error=%s",
                            table,
                            job["id"],
                            permanent,
                            safe_error(exc),
                        )
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Queue worker unavailable queue=%s error=%s", table, safe_error(exc))
                    await asyncio.sleep(network_settings.bot_restart_delay_seconds)
        finally:
            processor.close()

    def _observed_job(self, operation: Any, table: str, job: dict) -> None:
        if not self.store.telemetry:
            operation(job)
            return
        from src.vkbot.telemetry import persist

        with Capture() as capture:
            note(
                worker_started_at=time.time(),
                scheduled_at=job.get("next_at"),
                queue=table,
                worker_attempt=job["attempts"],
            )
            try:
                with span(
                    {"bot_inbox": "inbox.process", "bot_outbox": "outbox.deliver", "callback_ack": "vk.callback_ack"}[
                        table
                    ],
                    eligible_at=job.get("next_at"),
                    attempt=job["attempts"],
                    app_revision=telemetry_settings.app_revision,
                ):
                    operation(job)
            finally:
                try:
                    persist(self.store, table, job, capture.packet(), self.settings.app_environment)
                except Exception as exc:
                    logger.warning("Telemetry persistence unavailable error=%s", type(exc).__name__)

"""Атомарность, ограничение соединений и отмена нативной async-очереди."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.infrastructure.network import network_settings
from src.vkbot.processor import Processor
from src.vkbot.queue import QueueStore, event_record


def incoming(store: QueueStore, number: int = 1) -> dict:
    return event_record(
        store.group_id,
        "message_new",
        {"message": {"id": number, "peer_id": 10, "from_id": 10, "text": "test", "date": time.time()}},
    )


@pytest.mark.asyncio
async def test_cancelled_plan_rolls_back_outbox_and_inbox_completion(store, monkeypatch):
    await store.ingest([incoming(store)])
    job = await store.claim("bot_inbox")
    written = asyncio.Event()

    async def stop_before_commit(*args: object):
        written.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_observe", stop_before_commit)
    task = asyncio.create_task(store.plan(job, [{"method": "send", "params": {"peer_id": 10}}]))
    await asyncio.wait_for(written.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store.connect() as db:
        row = await (await db.execute("SELECT status FROM bot_inbox WHERE id=%s", (job["id"],))).fetchone()
        assert row["status"] == "processing"
        count = await (
            await db.execute("SELECT count(*) AS n FROM bot_outbox WHERE inbox_id=%s", (job["id"],))
        ).fetchone()
        assert count["n"] == 0
    monkeypatch.setattr(store, "_observe", AsyncMock())
    await store.plan(job, [{"method": "send", "params": {"peer_id": 10}}])
    assert (await store.claim("bot_outbox"))["inbox_id"] == job["id"]


@pytest.mark.asyncio
async def test_async_transactions_respect_connection_limit(store):
    active = maximum = 0

    async def query():
        nonlocal active, maximum
        async with store.connect() as db:
            active += 1
            maximum = max(maximum, active)
            try:
                await db.execute("SELECT pg_sleep(0.02)")
            finally:
                active -= 1

    await asyncio.gather(*(query() for _ in range(network_settings.queue_db_max_concurrent * 2)))
    assert maximum == network_settings.queue_db_max_concurrent
    assert active == 0


@pytest.mark.asyncio
async def test_cancelled_connection_waiter_leaves_owner_and_slot_usable(store):
    store._database_slots = asyncio.Semaphore(1)
    async with store.connect() as db:
        task = asyncio.create_task(store.get_state("paused"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.closed
        await db.execute("SELECT 1")
    await store.set_state("paused", False)
    assert await store.get_state("paused") is False


@pytest.mark.asyncio
async def test_cancelled_send_retries_same_random_id_after_expired_lease(store):
    await store.ingest([incoming(store)])
    await store.plan(await store.claim("bot_inbox"), [{"method": "send", "params": {"peer_id": 10}}])
    job = await store.claim("bot_outbox")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    sends = []
    accepted = {}

    def send(**params: object):
        identity = params["random_id"]
        sends.append(identity)
        accepted.setdefault(identity, 77)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return accepted[identity]

    processor = object.__new__(Processor)
    processor.store = store
    processor.settings = SimpleNamespace(dev_faults_enabled=False)
    processor.vk = SimpleNamespace(messages=SimpleNamespace(send=send))
    task = asyncio.create_task(processor.deliver(job))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store.connect() as db:
        await db.execute("UPDATE bot_outbox SET lease_until=0 WHERE id=%s", (job["id"],))
    retry = await store.claim("bot_outbox")
    await processor.deliver(retry)
    assert sends == [job["id"], job["id"]]
    assert len(accepted) == 1
    assert await store.claim("bot_outbox") is None

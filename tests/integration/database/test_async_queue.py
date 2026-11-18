"""Атомарность, ограничение соединений и отмена нативной async-очереди."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from requests import Response, Session

from src.infrastructure.network import network_settings
from src.vkbot.http import RateGate
from src.vkbot.processor import Processor
from src.vkbot.queue import QueueStore, event_record
from src.vkbot.runtime import Runtime


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
async def test_vk_pause_cannot_be_shortened_by_a_concurrent_response(store):
    await asyncio.gather(*(store.pause_vk_until(deadline) for deadline in (120, 140, 130)))
    assert await store.get_state("vk_paused_until") == 140
    await store.pause_vk_until(129)
    assert await store.get_state("vk_paused_until") == 140


@pytest.mark.asyncio
async def test_vk_error_six_releases_worker_and_defers_persisted_send(store, monkeypatch):
    await store.ingest([incoming(store)])
    await store.plan(await store.claim("bot_inbox"), [{"method": "send", "params": {"peer_id": 10}}])
    sent = []
    deferred = asyncio.Event()
    fail = store.fail

    def send(_session, request, **_options: object):
        sent.append(parse_qs(request.body)["random_id"][0])
        response = Response()
        response.status_code = 200
        response._content = b'{"error":{"error_code":6,"error_msg":"Too many requests"}}'
        return response

    async def save_failure(table, job, error, delay, permanent):
        await fail(table, job, error, delay, permanent)
        deferred.set()

    monkeypatch.setattr(Session, "send", send)
    monkeypatch.setattr(store, "fail", save_failure)
    runtime = object.__new__(Runtime)
    runtime.store = store
    runtime.gate = RateGate(0)
    runtime.settings = SimpleNamespace(
        vk_group_token="synthetic",
        dev_faults_enabled=False,
        queue_poll_seconds=0.01,
        queue_lease_seconds=3,
        queue_heartbeat_min_seconds=1,
        queue_retry_base_seconds=5,
        queue_retry_max_seconds=30,
        vk_error_pause_seconds=30,
    )
    task = asyncio.create_task(runtime._worker("bot_outbox", 0))
    try:
        await asyncio.wait_for(deferred.wait(), timeout=5)
        assert await store.get_state("vk_paused_until") > time.time()
        assert await store.claim("bot_outbox") is None
        async with store.connect() as db:
            job = await (
                await db.execute(
                    "SELECT id, status, attempts, owner, result FROM bot_outbox WHERE group_id=%s", (store.group_id,)
                )
            ).fetchone()
        assert sent == [str(job["id"])]
        assert job["status"] == "pending"
        assert job["attempts"] == 1
        assert job["owner"] is None
        assert job["result"] is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


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

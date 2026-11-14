"""Асинхронная блокировка runtime без запросов к VK и без активных транзакций."""

import asyncio
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from src.vkbot.runtime import Runtime


@pytest.mark.asyncio
async def test_runtime_guard_excludes_second_instance_and_releases_on_cancellation(store, monkeypatch):
    started = asyncio.Event()
    connections = []
    connect = store.connect_async

    async def tracked(**options: Any):
        connection = await connect(**options)
        connections.append(connection)
        return connection

    async def idle(*args: Any):
        started.set()
        await asyncio.Event().wait()

    runtime = object.__new__(Runtime)
    runtime.store = store
    runtime.settings = SimpleNamespace(queue_workers=1, queue_guard_interval_seconds=0.01)
    monkeypatch.setattr(store, "connect_async", tracked)
    monkeypatch.setattr(store, "initialize", lambda: None)
    runtime.verify_token = lambda: None
    for name in ("_collect", "_reconcile", "_maintenance", "_worker"):
        setattr(runtime, name, idle)

    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.03)
        with pytest.raises(RuntimeError, match="Another runtime"):
            await runtime.run()
        assert connections[1].closed
        assert not connections[0].closed
        assert connections[0].pgconn.transaction_status == psycopg.pq.TransactionStatus.IDLE
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(connection.closed for connection in connections)
    async with await store.connect_async(autocommit=True) as connection:
        row = await (
            await connection.execute("SELECT pg_try_advisory_lock(20261021, %s) AS locked", (store.group_id,))
        ).fetchone()
        assert row["locked"]

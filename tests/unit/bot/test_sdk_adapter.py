"""Граница async-worker и синхронного SDK при отмене внешнего запроса."""

import asyncio
import threading

import pytest

from src.vkbot.http import call_vk


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_cancellation_waits_for_sdk_completion_even_when_cancelled_again(fails):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    completed = []

    def send():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        completed.append(True)
        if fails:
            raise TimeoutError("SDK completed with an uncertain result")
        return 77

    task = asyncio.create_task(call_vk(send))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed == [True]

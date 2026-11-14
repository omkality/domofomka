import asyncio
import time
from types import SimpleNamespace

import pytest

from src.vkbot.queue import QueueStore, event_record


def record(store: QueueStore, peer: int = 10, cmid: int = 1) -> dict:
    return event_record(
        store.group_id,
        "message_new",
        {
            "message": {
                "id": cmid,
                "peer_id": peer,
                "from_id": peer,
                "conversation_message_id": cmid,
                "text": "test",
                "date": time.time(),
            }
        },
    )


async def limited(store, *, maximum=3, interval=0, initialize=True):
    result = QueueStore(
        store.dsn,
        store.group_id,
        lease_seconds=3,
        max_pending_messages_per_peer=maximum,
        reply_interval_seconds=interval,
        telemetry=store.telemetry,
    )
    if initialize:
        await result.initialize()
    return result


def request(store, number, peer=10, text=None):
    result = record(store, peer, number)
    result["payload"]["message"]["text"] = text or f"адрес {number}"
    result["occurred_at"] = time.time() - 43200 + number
    result["payload"]["message"]["date"] = result["occurred_at"]
    return result


async def pending_messages(store):
    async with store.connect() as db:
        return await (
            await db.execute(
                "SELECT payload#>>'{message,text}' AS text,status,payload->>'_queue_disposition' AS reason "
                "FROM bot_inbox WHERE group_id=%s ORDER BY occurred_at,id",
                (store.group_id,),
            )
        ).fetchall()


@pytest.mark.asyncio
async def test_backlog_keeps_latest_three_by_message_time_and_replay_does_not_resurrect_them(
    store,
):
    queue = await limited(store)
    batch = [request(queue, number) for number in range(8, 0, -1)]
    assert (await queue.ingest(batch, cursor="after-offline")) == 8
    rows = await pending_messages(queue)
    assert [r["text"] for r in rows if r["status"] == "pending"] == [
        "адрес 6",
        "адрес 7",
        "адрес 8",
    ]
    assert all(r["reason"] == "pending_limit" for r in rows[:5])
    restarted = await limited(store)
    assert (await restarted.ingest(batch)) == 0
    assert (await pending_messages(restarted)) == rows
    assert (await restarted.get_state("longpoll_ts")) == "after-offline"


@pytest.mark.asyncio
async def test_pending_duplicates_keep_only_latest_occurrence_and_do_not_use_all_slots(store):
    queue = await limited(store)
    texts = ["Адрес A", "  АДРЕС   A ", "Адрес B", "Адрес C", "Адрес A", "Адрес D"]
    await queue.ingest([request(queue, i, text=text) for i, text in enumerate(texts, 1)])
    rows = await pending_messages(queue)
    assert [r["text"] for r in rows if r["status"] == "pending"] == texts[-3:]
    assert [r["reason"] for r in rows[:3]] == [
        "superseded_duplicate",
        "superseded_duplicate",
        "pending_limit",
    ]


@pytest.mark.asyncio
async def test_history_after_half_day_offline_is_bounded_and_longpoll_replay_is_deduplicated(store):
    from src.vkbot.recovery import Reconciler

    queue = await limited(store)
    messages = [request(queue, n)["payload"]["message"] for n in range(6, 0, -1)]
    messages_api = SimpleNamespace(
        getConversations=lambda **kw: {
            "count": 1,
            "items": [{"conversation": {"peer": {"id": 10, "type": "user"}}, "last_message": messages[0]}],
        },
        getHistory=lambda **kw: {"count": 6, "items": messages},
    )
    settings = SimpleNamespace(recovery_overlap_seconds=900, recovery_bootstrap_seconds=86400)
    assert (await Reconciler(SimpleNamespace(messages=messages_api), queue, settings).run()) == 6
    assert [r["text"] for r in (await pending_messages(queue)) if r["status"] == "pending"] == [
        "адрес 4",
        "адрес 5",
        "адрес 6",
    ]
    assert (await queue.ingest([event_record(queue.group_id, "message_new", {"message": m}) for m in messages])) == 0
    assert len(await pending_messages(queue)) == 6


@pytest.mark.asyncio
async def test_failed_batch_does_not_drop_previous_requests_or_advance_checkpoint(store):
    queue = await limited(store, maximum=1)
    await queue.ingest([request(queue, 1)], cursor="before")
    invalid = request(queue, 3)
    invalid["group_id"] += 1
    with pytest.raises(AssertionError):
        await queue.ingest([request(queue, 2), invalid], cursor="must-not-commit")
    assert [r["text"] for r in (await pending_messages(queue)) if r["status"] == "pending"] == ["адрес 1"]
    assert (await queue.get_state("longpoll_ts")) == "before"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["geo", "attachments", "payload", "reply_message", "fwd_messages"])
async def test_different_message_context_is_never_treated_as_a_duplicate(store, field):
    queue = await limited(store)
    first, second = [request(queue, n, text="одинаковый текст") for n in (1, 2)]
    second["payload"]["message"][field] = {"different": True}
    await queue.ingest([first, second])
    assert len([r for r in (await pending_messages(queue)) if r["status"] == "pending"]) == 2


@pytest.mark.asyncio
async def test_new_limits_also_cancel_whole_unstarted_outbox_requests_on_restart(store):
    await store.ingest([request(store, n) for n in range(1, 7)])
    for _ in range(6):
        job = await store.claim("bot_inbox")
        await store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "prepared"}}])
    queue = await limited(store)
    async with queue.connect() as db:
        rows = await (
            await db.execute(
                "SELECT status,attempts FROM bot_outbox WHERE group_id=%s ORDER BY id",
                (queue.group_id,),
            )
        ).fetchall()
    assert [r["status"] for r in rows] == ["done"] * 3 + ["pending"] * 3
    assert all(r["attempts"] == 0 for r in rows)
    assert (await queue.claim("bot_outbox"))["inbox_id"] > job["id"] - 3


@pytest.mark.asyncio
async def test_a_send_with_unknown_result_retains_its_id_and_is_not_cancelled_by_new_requests(
    store,
):
    await store.ingest([request(store, 1)])
    original = await store.claim("bot_inbox")
    await store.plan(
        original,
        [{"method": "send", "params": {"peer_id": 10, "message": "uncertain"}}],
    )
    attempted = await store.claim("bot_outbox")
    await store.fail("bot_outbox", attempted, "timeout after possible send", 0)
    queue = await limited(store)
    await queue.ingest([request(queue, n) for n in range(2, 8)])
    retry = await queue.claim("bot_outbox")
    assert retry["id"] == attempted["id"]
    assert retry["attempts"] == 2
    assert retry["params"] == attempted["params"]
    assert len([r for r in (await pending_messages(queue)) if r["status"] == "pending"]) == 3


@pytest.mark.asyncio
async def test_remaining_pages_of_a_partially_delivered_response_are_not_cancelled(store):
    await store.ingest([request(store, 1)])
    await store.plan(
        (await store.claim("bot_inbox")),
        [{"method": "send", "params": {"peer_id": 10, "message": str(page)}} for page in (1, 2)],
    )
    await store.finish(await store.claim("bot_outbox"))
    queue = await limited(store)
    await queue.ingest([request(queue, n) for n in range(2, 6)])
    remaining = await queue.claim("bot_outbox")
    assert remaining["params"]["message"] == "2"
    assert remaining["attempts"] == 1


@pytest.mark.asyncio
async def test_claim_enforces_limits_even_when_adopting_an_old_queue_without_initialization(
    store,
):
    await store.ingest([request(store, n) for n in range(1, 7)])
    queue = await limited(store, initialize=False)
    job = await queue.claim("bot_inbox")
    assert job["payload"]["message"]["text"] == "адрес 4"
    assert len([r for r in (await pending_messages(queue)) if r["reason"] == "pending_limit"]) == 3


@pytest.mark.asyncio
async def test_concurrent_ingestion_limits_each_peer_independently(store):
    queue = await limited(store)
    results = await asyncio.gather(
        *((queue.ingest([request(queue, n, peer) for n in range(1, 5)])) for peer in (10, 20))
    )
    assert sum(results) == 8
    async with queue.connect() as db:
        rows = await (
            await db.execute(
                "SELECT peer_id,count(*) AS count FROM bot_inbox "
                "WHERE group_id=%s AND status='pending' GROUP BY peer_id",
                (queue.group_id,),
            )
        ).fetchall()
    assert {r["peer_id"]: r["count"] for r in rows} == {10: 3, 20: 3}


@pytest.mark.asyncio
async def test_outgoing_spacing_survives_restart_and_does_not_block_other_peers(store, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("src.vkbot.queue.time.time", lambda: now[0])
    queue = await limited(store, interval=5)
    await queue.ingest([request(queue, 1), request(queue, 2, peer=20)])
    for _ in range(2):
        job = await queue.claim("bot_inbox")
        pages = 2 if job["peer_id"] == 10 else 1
        await queue.plan(
            job,
            [{"method": "send", "params": {"peer_id": job["peer_id"]}} for _ in range(pages)],
        )
    first = await queue.claim("bot_outbox")
    assert first["peer_id"] == 10
    now[0] += 1
    await queue.finish(first)
    assert (await queue.claim("bot_outbox")) is None
    other = await queue.claim("bot_outbox")
    assert other["peer_id"] == 20
    await queue.finish(other)
    restarted = await limited(store, interval=5)
    now[0] += 4.9
    assert (await restarted.claim("bot_outbox")) is None
    now[0] += 0.1
    assert (await restarted.claim("bot_outbox"))["peer_id"] == 10


@pytest.mark.asyncio
async def test_failed_delivery_attempt_also_obeys_the_per_peer_interval(store, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("src.vkbot.queue.time.time", lambda: now[0])
    queue = await limited(store, interval=5)
    await queue.ingest([request(queue, 1)])
    await queue.plan((await queue.claim("bot_inbox")), [{"method": "send", "params": {"peer_id": 10}}])
    outgoing = await queue.claim("bot_outbox")
    await queue.fail("bot_outbox", outgoing, "temporary", 0)
    assert (await queue.claim("bot_outbox")) is None
    restarted = await limited(store, interval=5)
    now[0] += 5
    retry = await restarted.claim("bot_outbox")
    assert retry["id"] == outgoing["id"]
    assert retry["attempts"] == 2


@pytest.mark.asyncio
async def test_concurrent_send_claim_and_new_requests_never_cancel_an_attempted_effect(store):
    queue = await limited(store, maximum=1)
    await queue.ingest([request(queue, 1)])
    await queue.plan((await queue.claim("bot_inbox")), [{"method": "send", "params": {"peer_id": 10}}])
    outgoing, _ = await asyncio.gather((queue.claim("bot_outbox")), (queue.ingest([request(queue, 2)])))
    async with queue.connect() as db:
        row = await (
            await db.execute(
                "SELECT status,attempts FROM bot_outbox WHERE group_id=%s",
                (queue.group_id,),
            )
        ).fetchone()
    if outgoing:
        assert row == {"status": "processing", "attempts": 1}
    else:
        assert row == {"status": "done", "attempts": 0}


@pytest.mark.asyncio
async def test_cancelled_prepared_response_is_not_reported_as_accepted_by_vk(store):
    store.telemetry = True
    queue = await limited(store, maximum=1)
    try:
        await queue.ingest([request(queue, 1)])
        original = await queue.claim("bot_inbox")
        await queue.plan(original, [{"method": "send", "params": {"peer_id": 10}}])
        await queue.ingest([request(queue, 2)])
        async with queue.connect() as db:
            outcome = (
                await (
                    await db.execute(
                        "SELECT outcome FROM request_log WHERE group_id=%s AND inbox_id=%s",
                        (queue.group_id, original["id"]),
                    )
                ).fetchone()
            )["outcome"]
        assert outcome["delivery_status"] == "cancelled"
        assert outcome["queue_disposition"] == "pending_limit"
        assert outcome["outbox_done"] == 0
        assert outcome["outbox_skipped"] == 1
    finally:
        async with queue.connect() as db:
            await db.execute("DELETE FROM request_log WHERE group_id=%s", (queue.group_id,))

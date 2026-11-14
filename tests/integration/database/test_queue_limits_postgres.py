import time
from concurrent.futures import ThreadPoolExecutor
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


def limited(store, *, maximum=3, interval=0, initialize=True):
    result = QueueStore(
        store.dsn,
        store.group_id,
        lease_seconds=3,
        max_pending_messages_per_peer=maximum,
        reply_interval_seconds=interval,
        telemetry=store.telemetry,
    )
    if initialize:
        result.initialize()
    return result


def request(store, number, peer=10, text=None):
    result = record(store, peer, number)
    result["payload"]["message"]["text"] = text or f"адрес {number}"
    result["occurred_at"] = time.time() - 43200 + number
    result["payload"]["message"]["date"] = result["occurred_at"]
    return result


def pending_messages(store):
    with store.connect() as db:
        return db.execute(
            "SELECT payload#>>'{message,text}' AS text,status,payload->>'_queue_disposition' AS reason "
            "FROM bot_inbox WHERE group_id=%s ORDER BY occurred_at,id",
            (store.group_id,),
        ).fetchall()


def test_backlog_keeps_latest_three_by_message_time_and_replay_does_not_resurrect_them(
    store,
):
    queue = limited(store)
    batch = [request(queue, number) for number in range(8, 0, -1)]
    assert queue.ingest(batch, cursor="after-offline") == 8
    rows = pending_messages(queue)
    assert [r["text"] for r in rows if r["status"] == "pending"] == [
        "адрес 6",
        "адрес 7",
        "адрес 8",
    ]
    assert all(r["reason"] == "pending_limit" for r in rows[:5])
    restarted = limited(store)
    assert restarted.ingest(batch) == 0
    assert pending_messages(restarted) == rows
    assert restarted.get_state("longpoll_ts") == "after-offline"


def test_pending_duplicates_keep_only_latest_occurrence_and_do_not_use_all_slots(store):
    queue = limited(store)
    texts = ["Адрес A", "  АДРЕС   A ", "Адрес B", "Адрес C", "Адрес A", "Адрес D"]
    queue.ingest([request(queue, i, text=text) for i, text in enumerate(texts, 1)])
    rows = pending_messages(queue)
    assert [r["text"] for r in rows if r["status"] == "pending"] == texts[-3:]
    assert [r["reason"] for r in rows[:3]] == [
        "superseded_duplicate",
        "superseded_duplicate",
        "pending_limit",
    ]


def test_history_after_half_day_offline_is_bounded_and_longpoll_replay_is_deduplicated(store):
    from src.vkbot.recovery import Reconciler

    queue = limited(store)
    messages = [request(queue, n)["payload"]["message"] for n in range(6, 0, -1)]
    messages_api = SimpleNamespace(
        getConversations=lambda **kw: {
            "count": 1,
            "items": [{"conversation": {"peer": {"id": 10, "type": "user"}}, "last_message": messages[0]}],
        },
        getHistory=lambda **kw: {"count": 6, "items": messages},
    )
    settings = SimpleNamespace(recovery_overlap_seconds=900, recovery_bootstrap_seconds=86400)
    assert Reconciler(SimpleNamespace(messages=messages_api), queue, settings).run() == 6
    assert [r["text"] for r in pending_messages(queue) if r["status"] == "pending"] == ["адрес 4", "адрес 5", "адрес 6"]
    assert queue.ingest([event_record(queue.group_id, "message_new", {"message": m}) for m in messages]) == 0
    assert len(pending_messages(queue)) == 6


def test_failed_batch_does_not_drop_previous_requests_or_advance_checkpoint(store):
    queue = limited(store, maximum=1)
    queue.ingest([request(queue, 1)], cursor="before")
    invalid = request(queue, 3)
    invalid["group_id"] += 1
    with pytest.raises(AssertionError):
        queue.ingest([request(queue, 2), invalid], cursor="must-not-commit")
    assert [r["text"] for r in pending_messages(queue) if r["status"] == "pending"] == ["адрес 1"]
    assert queue.get_state("longpoll_ts") == "before"


@pytest.mark.parametrize("field", ["geo", "attachments", "payload", "reply_message", "fwd_messages"])
def test_different_message_context_is_never_treated_as_a_duplicate(store, field):
    queue = limited(store)
    first, second = [request(queue, n, text="одинаковый текст") for n in (1, 2)]
    second["payload"]["message"][field] = {"different": True}
    queue.ingest([first, second])
    assert len([r for r in pending_messages(queue) if r["status"] == "pending"]) == 2


def test_new_limits_also_cancel_whole_unstarted_outbox_requests_on_restart(store):
    store.ingest([request(store, n) for n in range(1, 7)])
    for _ in range(6):
        job = store.claim("bot_inbox")
        store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "prepared"}}])
    queue = limited(store)
    with queue.connect() as db:
        rows = db.execute(
            "SELECT status,attempts FROM bot_outbox WHERE group_id=%s ORDER BY id",
            (queue.group_id,),
        ).fetchall()
    assert [r["status"] for r in rows] == ["done"] * 3 + ["pending"] * 3
    assert all(r["attempts"] == 0 for r in rows)
    assert queue.claim("bot_outbox")["inbox_id"] > job["id"] - 3


def test_a_send_with_unknown_result_retains_its_id_and_is_not_cancelled_by_new_requests(
    store,
):
    store.ingest([request(store, 1)])
    original = store.claim("bot_inbox")
    store.plan(
        original,
        [{"method": "send", "params": {"peer_id": 10, "message": "uncertain"}}],
    )
    attempted = store.claim("bot_outbox")
    store.fail("bot_outbox", attempted, "timeout after possible send", 0)
    queue = limited(store)
    queue.ingest([request(queue, n) for n in range(2, 8)])
    retry = queue.claim("bot_outbox")
    assert retry["id"] == attempted["id"]
    assert retry["attempts"] == 2
    assert retry["params"] == attempted["params"]
    assert len([r for r in pending_messages(queue) if r["status"] == "pending"]) == 3


def test_remaining_pages_of_a_partially_delivered_response_are_not_cancelled(store):
    store.ingest([request(store, 1)])
    store.plan(
        store.claim("bot_inbox"),
        [{"method": "send", "params": {"peer_id": 10, "message": str(page)}} for page in (1, 2)],
    )
    store.finish(store.claim("bot_outbox"))
    queue = limited(store)
    queue.ingest([request(queue, n) for n in range(2, 6)])
    remaining = queue.claim("bot_outbox")
    assert remaining["params"]["message"] == "2"
    assert remaining["attempts"] == 1


def test_claim_enforces_limits_even_when_adopting_an_old_queue_without_initialization(
    store,
):
    store.ingest([request(store, n) for n in range(1, 7)])
    queue = limited(store, initialize=False)
    job = queue.claim("bot_inbox")
    assert job["payload"]["message"]["text"] == "адрес 4"
    assert len([r for r in pending_messages(queue) if r["reason"] == "pending_limit"]) == 3


def test_concurrent_ingestion_limits_each_peer_independently(store):
    queue = limited(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(queue.ingest, [request(queue, n, peer) for n in range(1, 5)]) for peer in (10, 20)]
        assert sum(t.result() for t in tasks) == 8
    with queue.connect() as db:
        rows = db.execute(
            "SELECT peer_id,count(*) AS count FROM bot_inbox WHERE group_id=%s AND status='pending' GROUP BY peer_id",
            (queue.group_id,),
        ).fetchall()
    assert {r["peer_id"]: r["count"] for r in rows} == {10: 3, 20: 3}


def test_outgoing_spacing_survives_restart_and_does_not_block_other_peers(store, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("src.vkbot.queue.time.time", lambda: now[0])
    queue = limited(store, interval=5)
    queue.ingest([request(queue, 1), request(queue, 2, peer=20)])
    for _ in range(2):
        job = queue.claim("bot_inbox")
        pages = 2 if job["peer_id"] == 10 else 1
        queue.plan(
            job,
            [{"method": "send", "params": {"peer_id": job["peer_id"]}} for _ in range(pages)],
        )
    first = queue.claim("bot_outbox")
    assert first["peer_id"] == 10
    now[0] += 1
    queue.finish(first)
    assert queue.claim("bot_outbox") is None
    other = queue.claim("bot_outbox")
    assert other["peer_id"] == 20
    queue.finish(other)
    restarted = limited(store, interval=5)
    now[0] += 4.9
    assert restarted.claim("bot_outbox") is None
    now[0] += 0.1
    assert restarted.claim("bot_outbox")["peer_id"] == 10


def test_failed_delivery_attempt_also_obeys_the_per_peer_interval(store, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("src.vkbot.queue.time.time", lambda: now[0])
    queue = limited(store, interval=5)
    queue.ingest([request(queue, 1)])
    queue.plan(queue.claim("bot_inbox"), [{"method": "send", "params": {"peer_id": 10}}])
    outgoing = queue.claim("bot_outbox")
    queue.fail("bot_outbox", outgoing, "temporary", 0)
    assert queue.claim("bot_outbox") is None
    restarted = limited(store, interval=5)
    now[0] += 5
    retry = restarted.claim("bot_outbox")
    assert retry["id"] == outgoing["id"]
    assert retry["attempts"] == 2


def test_concurrent_send_claim_and_new_requests_never_cancel_an_attempted_effect(store):
    queue = limited(store, maximum=1)
    queue.ingest([request(queue, 1)])
    queue.plan(queue.claim("bot_inbox"), [{"method": "send", "params": {"peer_id": 10}}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(queue.claim, "bot_outbox")
        adding = pool.submit(queue.ingest, [request(queue, 2)])
        outgoing = sending.result()
        adding.result()
    with queue.connect() as db:
        row = db.execute(
            "SELECT status,attempts FROM bot_outbox WHERE group_id=%s",
            (queue.group_id,),
        ).fetchone()
    if outgoing:
        assert row == {"status": "processing", "attempts": 1}
    else:
        assert row == {"status": "done", "attempts": 0}


def test_cancelled_prepared_response_is_not_reported_as_accepted_by_vk(store):
    store.telemetry = True
    queue = limited(store, maximum=1)
    try:
        queue.ingest([request(queue, 1)])
        original = queue.claim("bot_inbox")
        queue.plan(original, [{"method": "send", "params": {"peer_id": 10}}])
        queue.ingest([request(queue, 2)])
        with queue.connect() as db:
            outcome = db.execute(
                "SELECT outcome FROM request_log WHERE group_id=%s AND inbox_id=%s",
                (queue.group_id, original["id"]),
            ).fetchone()["outcome"]
        assert outcome["delivery_status"] == "cancelled"
        assert outcome["queue_disposition"] == "pending_limit"
        assert outcome["outbox_done"] == 0
        assert outcome["outbox_skipped"] == 1
    finally:
        with queue.connect() as db:
            db.execute("DELETE FROM request_log WHERE group_id=%s", (queue.group_id,))

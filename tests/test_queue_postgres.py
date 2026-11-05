import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.vkbot.queue import QueueStore, event_record


@pytest.fixture
def store() -> QueueStore:
    dsn = os.getenv("TEST_QUEUE_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_QUEUE_DATABASE_URL to run real PostgreSQL integration tests")
    # Each test owns a different namespace; development data is never truncated.
    group = 1_000_000_000 + uuid.uuid4().int % 1_000_000_000
    queue = QueueStore(dsn, group, lease_seconds=3)
    queue.initialize()
    yield queue
    with queue.connect() as conn:
        for table in ("bot_outbox", "bot_inbox", "bot_state", "bot_message_context"):
            conn.execute(f"DELETE FROM {table} WHERE group_id=%s", (group,))


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


def test_duplicate_batches_and_checkpoint_are_atomic(store: QueueStore) -> None:
    assert store.ingest([record(store)], "100") == 1
    assert store.ingest([record(store)], "101") == 0
    assert store.get_state("longpoll_ts") == "101"


def test_failed_batch_rolls_back_earlier_records_and_cursor(store: QueueStore) -> None:
    store.set_state("longpoll_ts", "old")
    invalid = record(store, cmid=2)
    invalid["payload"] = {"invalid": object()}
    with pytest.raises(TypeError):
        store.ingest([record(store), invalid], "new")
    assert store.get_state("longpoll_ts") == "old"
    assert store.claim("bot_inbox") is None


def test_parallel_claims_do_not_duplicate_jobs(store: QueueStore) -> None:
    store.ingest([record(store, peer=peer) for peer in range(10, 20)])
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = list(pool.map(lambda _: store.claim("bot_inbox"), range(10)))
    assert len({job["id"] for job in jobs if job}) == 10


def test_same_peer_is_ordered_while_other_peer_can_progress(store: QueueStore) -> None:
    store.ingest([record(store, 10, 1), record(store, 10, 2), record(store, 20, 1)])
    first = store.claim("bot_inbox")
    second = store.claim("bot_inbox")
    assert first["peer_id"] == 10
    assert second["peer_id"] == 20
    assert store.claim("bot_inbox") is None
    store.plan(first, [])
    assert store.claim("bot_inbox")["peer_id"] == 10


def test_expired_lease_is_reclaimed_and_old_owner_cannot_finish(store: QueueStore) -> None:
    store.ingest([record(store)])
    old = store.claim("bot_inbox")
    with store.connect() as conn:
        conn.execute("UPDATE bot_inbox SET lease_until=0 WHERE id=%s", (old["id"],))
    new = store.claim("bot_inbox")
    assert new["id"] == old["id"]
    assert new["owner"] != old["owner"]
    with pytest.raises(RuntimeError):
        store.plan(old, [])
    store.plan(new, [])


def test_delivery_result_survives_process_recreation(store: QueueStore) -> None:
    store.ingest([record(store)])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "test"}}])
    effect = store.claim("bot_outbox")
    store.save_result(effect, 77)
    store.fail("bot_outbox", effect, "temporary", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    retry = recreated.claim("bot_outbox")
    assert retry["id"] == effect["id"]
    assert retry["result"] == 77
    recreated.finish(retry)
    assert recreated.claim("bot_outbox") is None


def test_captured_callback_and_edit_survive_recreation(store: QueueStore) -> None:
    obj = {
        "peer_id": 10,
        "user_id": 10,
        "event_id": "click",
        "conversation_message_id": 12,
        "payload": {"entrance": "1", "ent_slice": ["1"]},
    }
    store.ingest([event_record(store.group_id, "message_event", obj)])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}])
    effect = store.claim("bot_outbox")
    store.fail("bot_outbox", effect, "timeout", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    retry = recreated.claim("bot_outbox")
    assert retry["params"] == effect["params"]
    recreated.finish(retry)


def test_communities_do_not_share_checkpoints_or_jobs(store: QueueStore) -> None:
    other = QueueStore(store.dsn, store.group_id + 2_000_000_000)
    store.ingest([record(store)], "100")
    assert other.get_state("longpoll_ts") is None
    assert other.claim("bot_inbox") is None


def click(store: QueueStore, name: str, entrance: str = "1", cmid: int = 12, peer: int = 10) -> dict:
    return event_record(
        store.group_id,
        "message_event",
        {
            "peer_id": peer,
            "user_id": peer,
            "event_id": name,
            "conversation_message_id": cmid,
            "payload": {"entrance": entrance, "ent_slice": ["1", "2"]},
        },
    )


def test_button_burst_creates_one_job_and_bounded_feedback(store: QueueStore) -> None:
    from unittest.mock import patch

    records = [click(store, f"burst-{i}") for i in range(40)]
    with patch("src.vkbot.queue.time.time", return_value=100):
        assert store.ingest(records, "batch-ts") == 40
    with store.connect() as c:
        rows = c.execute(
            "SELECT status,payload FROM bot_inbox WHERE group_id=%s ORDER BY id", (store.group_id,)
        ).fetchall()
    assert sum(row["status"] == "pending" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "wait" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "silent" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "skip" for row in rows) == 38
    assert store.get_state("longpoll_ts") == "batch-ts"
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}])
    assert store.claim("bot_inbox") is None
    assert store.claim("bot_outbox") is not None
    assert store.claim("bot_outbox") is None


def test_quick_entrance_changes_and_return_to_previous_are_preserved(store: QueueStore) -> None:
    store.ingest([click(store, "a", "1"), click(store, "b", "2"), click(store, "c", "1")])
    for entrance in ("1", "2", "1"):
        job = store.claim("bot_inbox")
        assert job["payload"]["payload"]["entrance"] == entrance
        store.plan(job, [])
    assert store.claim("bot_inbox") is None


def test_delayed_same_click_stays_coalesced_after_cooldown_and_restart(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        store.ingest([click(store, "a")])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}])
    effect = store.claim("bot_outbox")
    store.fail("bot_outbox", effect, "timeout", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=120):
        recreated.ingest([click(store, "b")])
    receipt = recreated.callback_feedback("callback:10:b")
    assert receipt["duplicate"]
    assert receipt["feedback"] == "pending"
    assert recreated.callback_feedback("callback:10:b") is None
    assert recreated.claim("bot_inbox") is None
    retry = recreated.claim("bot_outbox")
    assert retry["id"] == effect["id"]
    recreated.finish(retry)


def test_same_click_is_accepted_after_successful_cooldown(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        store.ingest([click(store, "a")])
    job = store.claim("bot_inbox")
    store.plan(job, [])
    with patch("src.vkbot.queue.time.time", return_value=106):
        store.ingest([click(store, "b")])
    assert store.claim("bot_inbox")["payload"]["event_id"] == "b"


def test_failed_click_can_be_explicitly_retried_without_waiting(store: QueueStore) -> None:
    store.ingest([click(store, "a")])
    job = store.claim("bot_inbox")
    store.fail("bot_inbox", job, "invalid", 0, permanent=True)
    store.ingest([click(store, "b")])
    assert store.claim("bot_inbox")["payload"]["event_id"] == "b"


def test_replayed_batch_does_not_reset_feedback_or_gate(store: QueueStore) -> None:
    records = [click(store, "a"), click(store, "b")]
    store.ingest(records, "1")
    receipt = store.callback_feedback("callback:10:b")
    assert receipt["feedback"] == "wait"
    previous = store.get_state("callback_gate:10")
    assert store.ingest(records, "2") == 0
    assert store.callback_feedback("callback:10:b") is None
    assert store.get_state("callback_gate:10") == previous


def test_feedback_budget_is_per_user_and_survives_restart(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        store.ingest([click(store, "a"), click(store, "b"), click(store, "c", peer=20), click(store, "d", peer=20)])
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=103):
        recreated.ingest([click(store, "e")])
    assert recreated.callback_feedback("callback:10:b")["feedback"] == "wait"
    assert recreated.callback_feedback("callback:20:d")["feedback"] == "wait"
    assert recreated.callback_feedback("callback:10:e")["feedback"] == "silent"
    with patch("src.vkbot.queue.time.time", return_value=111):
        recreated.ingest([click(store, "f")])
    assert recreated.callback_feedback("callback:10:f")["feedback"] == "pending"


def test_malformed_callback_does_not_block_batch_or_cursor(store: QueueStore) -> None:
    bad = click(store, "bad")
    bad["payload"]["payload"] = {}
    assert store.ingest([bad, click(store, "good")], "next") == 2
    assert store.get_state("longpoll_ts") == "next"
    assert store.claim("bot_inbox")["payload"]["event_id"] == "good"


def test_concurrent_click_ingestion_does_not_create_parallel_edits(store: QueueStore) -> None:
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(lambda i: store.ingest([click(store, f"parallel-{i}")]), range(15))) == 15
    with store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) AS n FROM bot_inbox WHERE group_id=%s AND status='pending'", (store.group_id,)
            ).fetchone()["n"]
            == 1
        )


def test_callback_gate_and_cursor_roll_back_with_failed_batch(store: QueueStore) -> None:
    invalid = record(store, cmid=2)
    invalid["payload"] = {"invalid": object()}
    store.set_state("longpoll_ts", "old")
    with pytest.raises(TypeError):
        store.ingest([click(store, "a"), invalid], "new")
    assert store.get_state("callback_gate:10") is None
    assert store.get_state("longpoll_ts") == "old"
    assert store.claim("bot_inbox") is None


def test_building_selection_is_a_durable_callback(store: QueueStore) -> None:
    item = event_record(
        store.group_id,
        "message_event",
        {
            "user_id": 10,
            "peer_id": 10,
            "conversation_message_id": 9,
            "event_id": "select-address",
            "payload": {"select_building": 0},
        },
    )
    assert store.ingest([item]) == 1
    claimed = store.claim("bot_inbox")
    assert claimed["payload"]["payload"] == {"select_building": 0}

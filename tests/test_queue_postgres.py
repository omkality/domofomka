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
        for table in ("bot_outbox", "bot_inbox", "bot_state", "bot_message_context", "bot_receipts"):
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


def test_retention_preserves_delivery_receipts_and_active_work(store):
    from src.vkbot.maintenance import compact

    first = record(store, 10, 1)
    store.ingest([first, record(store, 20, 2)])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "answer"}}])
    # Planned but undelivered work must survive regardless of inbox age.
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s AND status='done'", (store.group_id,))
    assert compact(store, 60, 60, 50)["inbox_removed"] == 0
    outgoing = store.claim("bot_outbox")
    store.finish(outgoing)
    with store.connect() as db:
        db.execute("UPDATE bot_outbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
    assert compact(store, 60, 60, 50) == {"inbox_removed": 1, "outbox_removed": 1, "contexts_removed": 0}
    assert store.ingest([first], cursor="retained") == 0
    assert store.get_state("longpoll_ts") == "retained"
    pending = store.claim("bot_inbox")
    assert pending["peer_id"] == 20
    store.fail("bot_inbox", pending, "test", 0, permanent=True)
    assert compact(store, 60, 60, 50)["inbox_removed"] == 0


def test_retention_and_concurrent_replay_do_not_resurrect_a_completed_message(store):
    from src.vkbot.maintenance import compact

    first = record(store)
    store.ingest([first])
    store.plan(store.claim("bot_inbox"), [])
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(compact, store, 60, 60, 50), pool.submit(store.ingest, [first])]
        for task in tasks:
            task.result()
    assert store.ingest([first]) == 0
    assert store.claim("bot_inbox") is None


def deferred_geo(store):
    store.ingest([record(store)])
    original = store.claim("bot_inbox")
    store.plan(
        original,
        [
            {
                "method": "send",
                "params": {"peer_id": 10, "message": "saved"},
                "defer_geo": {"delay": 3600, "max_age": 7200},
            }
        ],
    )
    with store.connect() as db:
        child = db.execute(
            "SELECT * FROM bot_inbox WHERE group_id=%s AND kind='geo_retry'", (store.group_id,)
        ).fetchone()
    return original, child


def test_deferred_geo_survives_restart_and_new_text_supersedes_it(store):
    _, child = deferred_geo(store)
    recreated = QueueStore(store.dsn, store.group_id)
    assert recreated.claim("bot_inbox") is None
    store.ingest([record(store, cmid=2)])
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    latest = recreated.claim("bot_inbox")
    assert latest["kind"] == "message_new"
    with store.connect() as db:
        assert db.execute("SELECT status FROM bot_inbox WHERE id=%s", (child["id"],)).fetchone()["status"] == "done"


def test_deferred_geo_retries_without_new_notices_and_expires(store):
    _, child = deferred_geo(store)
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = store.claim("bot_inbox")
    assert store.geo_retry_current(retry)
    store.defer_geo_retry(retry, 60)
    assert store.claim("bot_inbox") is None
    with store.connect() as db:
        db.execute(
            "UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1'),next_at=0 WHERE id=%s",
            (child["id"],),
        )
    assert store.claim("bot_inbox") is None


def test_new_request_cancels_already_prepared_geo_answer_before_delivery(store):
    _, child = deferred_geo(store)
    notice = store.claim("bot_outbox")
    store.finish(notice)
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = store.claim("bot_inbox")
    store.plan(retry, [{"method": "send", "params": {"peer_id": 10, "message": "old geo result"}}])
    store.ingest([record(store, cmid=2)])
    outgoing = store.claim("bot_outbox")
    assert store.outbox_geo_current(outgoing) is False


def test_expired_geo_is_retained_as_failure_and_can_be_explicitly_retried(store):
    _, child = deferred_geo(store)
    with store.connect() as db:
        db.execute(
            "UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1'),next_at=0 WHERE id=%s",
            (child["id"],),
        )
    assert store.claim("bot_inbox") is None
    with store.connect() as db:
        expired = db.execute("SELECT status,last_error FROM bot_inbox WHERE id=%s", (child["id"],)).fetchone()
        assert expired["status"] == "failed"
        assert "GeoRetryExpired" in expired["last_error"]
    store.retry_failed(60)
    retry = store.claim("bot_inbox")
    assert retry["id"] == child["id"]
    assert store.geo_retry_current(retry)


def test_prepared_geo_answer_survives_retry_deadline_if_not_superseded(store):
    _, child = deferred_geo(store)
    store.finish(store.claim("bot_outbox"))
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = store.claim("bot_inbox")
    with store.connect() as db:
        db.execute("UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1') WHERE id=%s", (child["id"],))
    store.plan(retry, [{"method": "send", "params": {"peer_id": 10, "message": "ready answer"}}])
    outgoing = store.claim("bot_outbox")
    assert store.outbox_geo_current(outgoing) is True


def test_retention_expires_context_but_does_not_remove_pending_peer_context(store):
    from src.vkbot.maintenance import compact

    with store.connect() as db:
        for peer in (10, 20):
            db.execute("INSERT INTO bot_message_context VALUES(%s,%s,42,'{}',1)", (store.group_id, peer))
            db.execute("INSERT INTO bot_state VALUES(%s,%s,'true')", (store.group_id, f"choices_closed:{peer}:42"))
    store.ingest([record(store, 20)])
    assert compact(store, 60, 60, 50)["contexts_removed"] == 1
    assert store.message_context(10, 42) is None
    assert store.choices_closed(10, 42) is False
    assert store.message_context(20, 42) == {}
    assert store.choices_closed(20, 42) is True


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


def test_rapid_alternating_buttons_share_one_user_cooldown(store: QueueStore) -> None:
    records = [click(store, f"alternate-{i}", str(i % 3 + 1), cmid=12 + i % 2) for i in range(40)]
    store.ingest(records)
    job = store.claim("bot_inbox")
    assert job["payload"]["event_id"] == "alternate-0"
    store.plan(job, [])
    assert store.claim("bot_inbox") is None
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM bot_inbox WHERE group_id=%s ORDER BY id", (store.group_id,)).fetchall()
    assert sum(r["payload"]["_queue_receipt"]["throttled"] for r in rows) == 39


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
    with patch("src.vkbot.queue.time.time", return_value=101):
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


@pytest.mark.parametrize("payload", [{"dismiss_choices": True}, {"choice_page": 1}, {"select_building": 0}])
def test_choice_actions_are_durable_and_deduplicate(store, payload):
    first = click(store, "choice-first")
    first["payload"]["payload"] = payload
    second = click(store, "choice-repeat")
    second["payload"]["payload"] = payload
    store.ingest([first, second])
    job = store.claim("bot_inbox")
    assert job["payload"]["payload"] == payload
    assert store.claim("bot_inbox") is None


def test_closing_choices_is_atomic_with_the_outbox_plan(store):
    store.ingest([record(store)])
    job = store.claim("bot_inbox")
    effect = {
        "method": "edit",
        "params": {"conversation_message_id": 42},
        "context": {"status": "dismissed"},
        "close_choices": True,
    }
    with pytest.raises(TypeError):
        store.plan(job, [effect, {"method": "send", "params": {"invalid": object()}}])
    assert store.choices_closed(10, 42) is False
    assert store.claim("bot_outbox") is None
    store.plan(job, [effect])
    recreated = QueueStore(store.dsn, store.group_id)
    assert recreated.choices_closed(10, 42) is True
    assert recreated.choices_closed(10, 43) is False
    assert recreated.claim("bot_outbox")["context"]["status"] == "dismissed"


def test_late_page_cannot_restore_a_rejected_menu_before_delivery(store):
    from unittest.mock import MagicMock

    from src.vkbot.processor import Processor

    # An old page edit may still be in outbox when rejection is planned.
    store.ingest([record(store)])
    job = store.claim("bot_inbox")
    choices = {"status": "ambiguous", "candidates": [{"building_id": "a", "address": "Дом"}]}
    store.plan(job, [{"method": "edit", "params": {"conversation_message_id": 12}, "context": choices}])
    old_page = store.claim("bot_outbox")
    store.finish(old_page, (12, choices))
    reject = click(store, "reject")
    reject["payload"]["payload"] = {"dismiss_choices": True}
    store.ingest([reject])
    processor = object.__new__(Processor)
    processor.store = store
    processor._get = MagicMock()
    processor.plan(store.claim("bot_inbox"))
    assert store.choices_closed(10, 12)
    # Context is still the delivered old menu until the rejection is delivered.
    assert store.message_context(10, 12)["status"] == "ambiguous"
    stale_page = click(store, "late-page")
    stale_page["payload"]["payload"] = {"choice_page": 0}
    store.ingest([stale_page])
    assert store.claim("bot_inbox") is None
    rejection = store.claim("bot_outbox")
    store.finish(rejection, (12, rejection["context"]))
    assert store.message_context(10, 12)["status"] == "dismissed"
    assert store.claim("bot_outbox") is None
    processor._get.assert_not_called()


def test_back_reopens_choices_only_after_confirmed_edit_and_survives_retry(store):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.vkbot.processor import Processor, address_choices

    choices = {
        "status": "ambiguous",
        "candidates": [{"building_id": str(i), "address": f"Город {i}, дом 18"} for i in range(8)],
    }
    store.ingest([record(store)])
    store.plan(store.claim("bot_inbox"), [{"method": "send", "params": {}, "context": choices}])
    store.finish(store.claim("bot_outbox"), (42, choices))
    processor = object.__new__(Processor)
    processor.store = store
    processor.settings = SimpleNamespace(domofomka_api_host="api", domofomka_api_port=8000, dev_faults_enabled=False)
    processor.vk = MagicMock()
    processor._get = MagicMock()
    processor._get.return_value.status_code = 200
    processor._get.return_value.json.return_value = {"address": "Город 6, дом 18", "data": {"1": [["TEST", "test"]]}}

    def plan(action, name):
        item = click(store, name, cmid=42)
        item["payload"]["payload"] = action
        store.ingest([item], callback_window=0)
        processor.plan(store.claim("bot_inbox"))
        return store.claim("bot_outbox")

    selected = plan({"select_building": 6}, "select-six")
    processor.deliver(selected)
    assert store.choices_closed(10, 42)
    assert store.message_context(10, 42)["address"] == "Город 6, дом 18"
    back = plan({"return_to_choices": True}, "back")
    assert back["params"]["message"] == address_choices(choices, 1)[0]
    assert store.choices_closed(10, 42)
    processor.vk.messages.edit.side_effect = [TimeoutError(), 1]
    with pytest.raises(TimeoutError):
        processor.deliver(back)
    store.fail("bot_outbox", back, "timeout", delay=0)
    recreated = QueueStore(store.dsn, store.group_id, lease_seconds=3)
    processor.store = recreated
    assert recreated.choices_closed(10, 42)
    assert "_address_choice" in recreated.message_context(10, 42)
    retry = recreated.claim("bot_outbox")
    assert retry["params"] == back["params"]
    processor.deliver(retry)
    assert recreated.message_context(10, 42) == choices
    assert recreated.choices_closed(10, 42) is False
    assert processor._get.call_count == 1
    processor.vk.messages.edit.side_effect = None
    plan({"select_building": 3}, "select-three")
    assert processor._get.call_args.args[0].endswith("/api/v2/buildings/3")


@pytest.mark.parametrize("action", [{"return_to_choices": True}, {"entrance_page": 1}])
def test_new_navigation_callbacks_share_existing_cooldown(store, action):
    records = []
    for i in range(3):
        item = click(store, f"navigation-{i}")
        item["payload"]["payload"] = action
        records.append(item)
    store.ingest(records)
    assert store.claim("bot_inbox") is not None
    assert store.claim("bot_inbox") is None
    assert store.callback_feedback("callback:10:navigation-1")["throttled"]


def test_cooldown_starts_after_delivery_not_before_a_slow_request(store):
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        store.ingest([click(store, "slow")])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12}}])
    outbox = store.claim("bot_outbox")
    with patch("src.vkbot.queue.time.time", return_value=120):
        store.finish(outbox)
    with patch("src.vkbot.queue.time.time", return_value=121):
        store.ingest([click(store, "too-soon", "2")])
    receipt = store.callback_feedback("callback:10:too-soon")
    assert receipt["throttled"]
    assert receipt["wait_seconds"] == 4
    assert store.claim("bot_inbox") is None
    with patch("src.vkbot.queue.time.time", return_value=126):
        store.ingest([click(store, "after-pause", "2")])
    assert store.claim("bot_inbox")["payload"]["event_id"] == "after-pause"


def test_pending_callback_blocks_different_buttons_after_restart(store):
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        store.ingest([click(store, "pending")])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12}}])
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=120):
        recreated.ingest([click(store, "other-button", "2", cmid=99)])
    assert recreated.claim("bot_inbox") is None
    assert recreated.callback_feedback("callback:10:other-button")["feedback"] == "pending"

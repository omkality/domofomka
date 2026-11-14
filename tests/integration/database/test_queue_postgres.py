import asyncio
import time

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


@pytest.mark.asyncio
async def test_retention_preserves_delivery_receipts_and_active_work(store):
    from src.vkbot.maintenance import compact

    first = record(store, 10, 1)
    await store.ingest([first, record(store, 20, 2)])
    job = await store.claim("bot_inbox")
    await store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "answer"}}])
    # Planned but undelivered work must survive regardless of inbox age.
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s AND status='done'", (store.group_id,))
    assert (await compact(store, 60, 60, 50))["inbox_removed"] == 0
    outgoing = await store.claim("bot_outbox")
    await store.finish(outgoing)
    async with store.connect() as db:
        await db.execute("UPDATE bot_outbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
    assert (await compact(store, 60, 60, 50)) == {"inbox_removed": 1, "outbox_removed": 1, "contexts_removed": 0}
    assert (await store.ingest([first], cursor="retained")) == 0
    assert (await store.get_state("longpoll_ts")) == "retained"
    pending = await store.claim("bot_inbox")
    assert pending["peer_id"] == 20
    await store.fail("bot_inbox", pending, "test", 0, permanent=True)
    assert (await compact(store, 60, 60, 50))["inbox_removed"] == 0


@pytest.mark.asyncio
async def test_retention_and_concurrent_replay_do_not_resurrect_a_completed_message(store):
    from src.vkbot.maintenance import compact

    first = record(store)
    await store.ingest([first])
    await store.plan((await store.claim("bot_inbox")), [])
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
    await asyncio.gather(compact(store, 60, 60, 50), (store.ingest([first])))
    assert (await store.ingest([first])) == 0
    assert (await store.claim("bot_inbox")) is None


async def deferred_geo(store):
    await store.ingest([record(store)])
    original = await store.claim("bot_inbox")
    await store.plan(
        original,
        [
            {
                "method": "send",
                "params": {"peer_id": 10, "message": "saved"},
                "defer_geo": {"delay": 3600, "max_age": 7200},
            }
        ],
    )
    async with store.connect() as db:
        child = await (
            await db.execute("SELECT * FROM bot_inbox WHERE group_id=%s AND kind='geo_retry'", (store.group_id,))
        ).fetchone()
    return original, child


@pytest.mark.asyncio
async def test_deferred_geo_survives_restart_and_new_text_supersedes_it(store):
    _, child = await deferred_geo(store)
    recreated = QueueStore(store.dsn, store.group_id)
    assert (await recreated.claim("bot_inbox")) is None
    await store.ingest([record(store, cmid=2)])
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    latest = await recreated.claim("bot_inbox")
    assert latest["kind"] == "message_new"
    async with store.connect() as db:
        assert (await (await db.execute("SELECT status FROM bot_inbox WHERE id=%s", (child["id"],))).fetchone())[
            "status"
        ] == "done"


@pytest.mark.asyncio
async def test_deferred_geo_retries_without_new_notices_and_expires(store):
    _, child = await deferred_geo(store)
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = await store.claim("bot_inbox")
    assert await store.geo_retry_current(retry)
    await store.defer_geo_retry(retry, 60)
    assert (await store.claim("bot_inbox")) is None
    async with store.connect() as db:
        await db.execute(
            "UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1'),next_at=0 WHERE id=%s",
            (child["id"],),
        )
    assert (await store.claim("bot_inbox")) is None


@pytest.mark.asyncio
async def test_new_request_cancels_already_prepared_geo_answer_before_delivery(store):
    _, child = await deferred_geo(store)
    notice = await store.claim("bot_outbox")
    await store.finish(notice)
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = await store.claim("bot_inbox")
    await store.plan(retry, [{"method": "send", "params": {"peer_id": 10, "message": "old geo result"}}])
    await store.ingest([record(store, cmid=2)])
    outgoing = await store.claim("bot_outbox")
    assert (await store.outbox_geo_current(outgoing)) is False


@pytest.mark.asyncio
async def test_expired_geo_is_retained_as_failure_and_can_be_explicitly_retried(store):
    _, child = await deferred_geo(store)
    async with store.connect() as db:
        await db.execute(
            "UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1'),next_at=0 WHERE id=%s",
            (child["id"],),
        )
    assert (await store.claim("bot_inbox")) is None
    async with store.connect() as db:
        expired = await (
            await db.execute("SELECT status,last_error FROM bot_inbox WHERE id=%s", (child["id"],))
        ).fetchone()
        assert expired["status"] == "failed"
        assert "GeoRetryExpired" in expired["last_error"]
    await store.retry_failed(60)
    retry = await store.claim("bot_inbox")
    assert retry["id"] == child["id"]
    assert await store.geo_retry_current(retry)


@pytest.mark.asyncio
async def test_prepared_geo_answer_survives_retry_deadline_if_not_superseded(store):
    _, child = await deferred_geo(store)
    await store.finish(await store.claim("bot_outbox"))
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET next_at=0 WHERE id=%s", (child["id"],))
    retry = await store.claim("bot_inbox")
    async with store.connect() as db:
        await db.execute(
            "UPDATE bot_inbox SET payload=jsonb_set(payload,'{_geo_deadline}','1') WHERE id=%s", (child["id"],)
        )
    await store.plan(retry, [{"method": "send", "params": {"peer_id": 10, "message": "ready answer"}}])
    outgoing = await store.claim("bot_outbox")
    assert (await store.outbox_geo_current(outgoing)) is True


@pytest.mark.asyncio
async def test_retention_expires_context_but_does_not_remove_pending_peer_context(store):
    from src.vkbot.maintenance import compact

    async with store.connect() as db:
        for peer in (10, 20):
            await db.execute("INSERT INTO bot_message_context VALUES(%s,%s,42,'{}',1)", (store.group_id, peer))
            await db.execute(
                "INSERT INTO bot_state VALUES(%s,%s,'true')", (store.group_id, f"choices_closed:{peer}:42")
            )
    await store.ingest([record(store, 20)])
    assert (await compact(store, 60, 60, 50))["contexts_removed"] == 1
    assert (await store.message_context(10, 42)) is None
    assert (await store.choices_closed(10, 42)) is False
    assert (await store.message_context(20, 42)) == {}
    assert (await store.choices_closed(20, 42)) is True


@pytest.mark.asyncio
async def test_duplicate_batches_and_checkpoint_are_atomic(store: QueueStore) -> None:
    assert (await store.ingest([record(store)], "100")) == 1
    assert (await store.ingest([record(store)], "101")) == 0
    assert (await store.get_state("longpoll_ts")) == "101"


@pytest.mark.asyncio
async def test_failed_batch_rolls_back_earlier_records_and_cursor(store: QueueStore) -> None:
    await store.set_state("longpoll_ts", "old")
    invalid = record(store, cmid=2)
    invalid["payload"] = {"invalid": object()}
    with pytest.raises(TypeError):
        await store.ingest([record(store), invalid], "new")
    assert (await store.get_state("longpoll_ts")) == "old"
    assert (await store.claim("bot_inbox")) is None


@pytest.mark.asyncio
async def test_parallel_claims_do_not_duplicate_jobs(store: QueueStore) -> None:
    await store.ingest([record(store, peer=peer) for peer in range(10, 20)])
    jobs = await asyncio.gather(*((store.claim("bot_inbox")) for _ in range(10)))
    assert len({job["id"] for job in jobs if job}) == 10


@pytest.mark.asyncio
async def test_same_peer_is_ordered_while_other_peer_can_progress(store: QueueStore) -> None:
    await store.ingest([record(store, 10, 1), record(store, 10, 2), record(store, 20, 1)])
    first = await store.claim("bot_inbox")
    second = await store.claim("bot_inbox")
    assert first["peer_id"] == 10
    assert second["peer_id"] == 20
    assert (await store.claim("bot_inbox")) is None
    await store.plan(first, [])
    assert (await store.claim("bot_inbox"))["peer_id"] == 10


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_owner_cannot_finish(store: QueueStore) -> None:
    await store.ingest([record(store)])
    old = await store.claim("bot_inbox")
    async with store.connect() as conn:
        await conn.execute("UPDATE bot_inbox SET lease_until=0 WHERE id=%s", (old["id"],))
    new = await store.claim("bot_inbox")
    assert new["id"] == old["id"]
    assert new["owner"] != old["owner"]
    with pytest.raises(RuntimeError):
        await store.plan(old, [])
    await store.plan(new, [])


@pytest.mark.asyncio
async def test_delivery_result_survives_process_recreation(store: QueueStore) -> None:
    await store.ingest([record(store)])
    job = await store.claim("bot_inbox")
    await store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "test"}}])
    effect = await store.claim("bot_outbox")
    await store.save_result(effect, 77)
    await store.fail("bot_outbox", effect, "temporary", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    retry = await recreated.claim("bot_outbox")
    assert retry["id"] == effect["id"]
    assert retry["result"] == 77
    await recreated.finish(retry)
    assert (await recreated.claim("bot_outbox")) is None


@pytest.mark.asyncio
async def test_captured_callback_and_edit_survive_recreation(store: QueueStore) -> None:
    obj = {
        "peer_id": 10,
        "user_id": 10,
        "event_id": "click",
        "conversation_message_id": 12,
        "payload": {"entrance": "1", "ent_slice": ["1"]},
    }
    await store.ingest([event_record(store.group_id, "message_event", obj)])
    job = await store.claim("bot_inbox")
    await store.plan(
        job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}]
    )
    effect = await store.claim("bot_outbox")
    await store.fail("bot_outbox", effect, "timeout", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    retry = await recreated.claim("bot_outbox")
    assert retry["params"] == effect["params"]
    await recreated.finish(retry)


@pytest.mark.asyncio
async def test_communities_do_not_share_checkpoints_or_jobs(store: QueueStore) -> None:
    other = QueueStore(store.dsn, store.group_id + 2_000_000_000)
    await store.ingest([record(store)], "100")
    assert (await other.get_state("longpoll_ts")) is None
    assert (await other.claim("bot_inbox")) is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [2, 5])
@pytest.mark.parametrize(
    "action",
    [
        {"entrance": "1", "ent_slice": ["1", "2"]},
        {"select_building": 0},
        {"choice_page": 1},
        {"entrance_page": 1},
        {"return_to_choices": True},
        {"dismiss_choices": True},
    ],
)
async def test_callback_and_reply_pause_start_at_delivery_and_survive_restart(store, interval, action):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, reply_interval_seconds=interval)

    async def incoming(name, peer=10):
        item = click(limited, name, peer=peer)
        item["payload"]["payload"] = action
        await limited.ingest([item], callback_window=interval)
        return item

    async def plan(job):
        await limited.plan(
            job,
            [{"method": "edit", "params": {"peer_id": job["peer_id"], "conversation_message_id": 12}}],
        )

    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        await incoming("initial")
        first = await limited.claim("bot_inbox")
        await plan(first)
        outgoing = await limited.claim("bot_outbox")

        # Passing the nominal pause does not admit more work while delivery is active.
        clock.return_value = 110
        await incoming("still-delivering")
        assert (await limited.claim("bot_inbox")) is None
        assert (await limited.callback_feedback("callback:10:still-delivering"))["feedback"] == "pending"

        await incoming("another-user", peer=20)
        other = await limited.claim("bot_inbox")
        assert other["peer_id"] == 20
        await limited.plan(other, [])
        await limited.finish(outgoing)

        recreated = QueueStore(store.dsn, store.group_id, reply_interval_seconds=interval)
        clock.return_value = 110 + interval - 0.01
        await incoming("just-before-boundary")
        assert (await recreated.claim("bot_inbox")) is None
        assert (await recreated.claim("bot_outbox")) is None

        clock.return_value = 110 + interval
        await incoming("at-boundary")
        accepted = await recreated.claim("bot_inbox")
        assert accepted["payload"]["event_id"] == "at-boundary"
        await plan(accepted)
        next_outgoing = await recreated.claim("bot_outbox")
        assert next_outgoing["inbox_id"] == accepted["id"]
        await recreated.finish(next_outgoing)


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [2, 5])
async def test_outbox_pause_preserves_retry_identity_and_does_not_delay_other_users(store, interval):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, reply_interval_seconds=interval)
    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        await limited.ingest([record(limited, peer=10), record(limited, peer=20)])
        for _ in range(2):
            job = await limited.claim("bot_inbox")
            await limited.plan(job, [{"method": "send", "params": {"peer_id": job["peer_id"], "message": "test"}}])
        first = await limited.claim("bot_outbox")
        assert first["peer_id"] == 10
        await limited.save_result(first, 77)
        await limited.fail("bot_outbox", first, "lost acknowledgement", delay=0)
        other = await limited.claim("bot_outbox")
        assert other["peer_id"] == 20
        await limited.finish(other)

        recreated = QueueStore(store.dsn, store.group_id, reply_interval_seconds=interval)
        clock.return_value = 100 + interval - 0.01
        assert (await recreated.claim("bot_outbox")) is None
        clock.return_value = 100 + interval
        retried = await recreated.claim("bot_outbox")
        assert retried["id"] == first["id"]
        assert retried["result"] == 77
        await recreated.finish(retried)
        assert (await recreated.claim("bot_outbox")) is None


@pytest.mark.asyncio
async def test_message_rate_keeps_latest_correction_without_blocking_other_users(store):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, message_rate_interval_seconds=5, message_rate_burst=2)

    def incoming(number, peer=10):
        item = record(limited, peer, number)
        item["payload"]["message"]["text"] = f"Запрос {number}"
        return item

    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        for i in [1, 2]:
            await limited.ingest([incoming(i)])
            job = await limited.claim("bot_inbox")
            assert job is not None
            await limited.plan(job, [])
        await limited.ingest([(incoming(3)), (incoming(4)), (incoming(1, peer=20))])
        other = await limited.claim("bot_inbox")
        assert other["peer_id"] == 20
        await limited.plan(other, [])
        assert (await limited.claim("bot_inbox")) is None
        clock.return_value = 104
        await limited.ingest([incoming(5)])
        assert (await limited.claim("bot_inbox")) is None
        clock.return_value = 105
        latest = await limited.claim("bot_inbox")
        assert latest["payload"]["message"]["id"] == 5
        await limited.plan(latest, [])
        async with limited.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT payload->>'_queue_disposition' AS reason FROM bot_inbox WHERE group_id=%s",
                    (store.group_id,),
                )
            ).fetchall()
        assert sum(r["reason"] == "message_rate_coalesced" for r in rows) == 2


@pytest.mark.asyncio
async def test_message_budget_survives_restart_without_charging_retries(store):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, message_rate_interval_seconds=5, message_rate_burst=1)
    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        await limited.ingest([record(limited)])
        first = await limited.claim("bot_inbox")
        await limited.fail("bot_inbox", first, "timeout", delay=0)
        recreated = QueueStore(store.dsn, store.group_id, message_rate_interval_seconds=5, message_rate_burst=1)
        retry = await recreated.claim("bot_inbox")
        assert retry["id"] == first["id"]
        assert retry["attempts"] == 2
        await recreated.plan(retry, [])
        await recreated.ingest([record(recreated, cmid=2)])
        assert (await recreated.claim("bot_inbox")) is None
        clock.return_value = 105
        assert (await recreated.claim("bot_inbox")) is not None


@pytest.mark.asyncio
async def test_message_rate_keeps_latest_when_pending_cap_removes_the_delayed_row(store):
    from unittest.mock import patch

    limited = QueueStore(
        store.dsn,
        store.group_id,
        message_rate_interval_seconds=5,
        message_rate_burst=2,
        max_pending_messages_per_peer=3,
    )

    def incoming(number):
        item = record(limited, cmid=number)
        item["payload"]["message"]["text"] = f"Разный запрос {number}"
        return item

    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        for i in [1, 2]:
            await limited.ingest([incoming(i)])
            await limited.plan((await limited.claim("bot_inbox")), [])
        await limited.ingest([incoming(3)])
        assert (await limited.claim("bot_inbox")) is None
        clock.return_value = 104
        await limited.ingest([(incoming(4)), (incoming(5)), (incoming(6))])
        clock.return_value = 105
        assert (await limited.claim("bot_inbox"))["payload"]["message"]["id"] == 6


@pytest.mark.asyncio
async def test_identical_hints_are_limited_per_user_but_codes_are_not(store):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, hint_interval_seconds=15)
    hint = {"method": "send", "params": {"message": "Укажите адрес"}, "hint": True}
    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        await limited.ingest([record(limited)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        await limited.finish(await limited.claim("bot_outbox"))
        clock.return_value = 101
        await limited.ingest([record(limited, cmid=2)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        assert (await limited.claim("bot_outbox")) is None
        await limited.ingest([record(limited, peer=20)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        assert (await limited.claim("bot_outbox"))["peer_id"] == 20
        await limited.ingest([record(limited, cmid=3)])
        await limited.plan(
            (await limited.claim("bot_inbox")), [{"method": "send", "params": {"message": "Адрес и коды"}}]
        )
        assert (await limited.claim("bot_outbox"))["params"]["message"] == "Адрес и коды"


@pytest.mark.asyncio
async def test_pending_hint_prevents_duplicates_even_after_notice_interval(store):
    from unittest.mock import patch

    limited = QueueStore(store.dsn, store.group_id, hint_interval_seconds=15)
    hint = {"method": "send", "params": {"message": "Укажите адрес"}, "hint": True}
    with patch("src.vkbot.queue.time.time", return_value=100) as clock:
        await limited.ingest([record(limited)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        clock.return_value = 200
        await limited.ingest([record(limited, cmid=2)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        outgoing = await limited.claim("bot_outbox")
        await limited.finish(outgoing)
        assert (await limited.claim("bot_outbox")) is None
        clock.return_value = 216
        await limited.ingest([record(limited, cmid=3)])
        await limited.plan((await limited.claim("bot_inbox")), [hint])
        assert (await limited.claim("bot_outbox")) is not None


@pytest.mark.asyncio
async def test_retention_removes_only_inactive_expired_message_gates(store):
    from psycopg.types.json import Jsonb

    from src.vkbot.maintenance import compact

    await store.ingest([record(store, peer=10), record(store, peer=20)])
    for _ in range(2):
        await store.plan((await store.claim("bot_inbox")), [])
    async with store.connect() as db:
        await db.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
        for peer in [10, 20]:
            for prefix in ["message_rate", "message_hint"]:
                await db.execute(
                    "INSERT INTO bot_state VALUES(%s,%s,%s)",
                    (store.group_id, f"{prefix}:{peer}", Jsonb({"updated_at": 1})),
                )
    await store.ingest([record(store, peer=10, cmid=2)])
    await compact(store, 60, 60, 50)
    assert (await store.get_state("message_rate:20")) is None
    assert (await store.get_state("message_hint:20")) is None
    assert (await store.get_state("message_rate:10")) is not None
    assert (await store.get_state("message_hint:10")) is not None


@pytest.mark.asyncio
async def test_button_burst_creates_one_job_and_bounded_feedback(store: QueueStore) -> None:
    from unittest.mock import patch

    records = [click(store, f"burst-{i}") for i in range(40)]
    with patch("src.vkbot.queue.time.time", return_value=100):
        assert (await store.ingest(records, "batch-ts")) == 40
    async with store.connect() as c:
        rows = await (
            await c.execute("SELECT status,payload FROM bot_inbox WHERE group_id=%s ORDER BY id", (store.group_id,))
        ).fetchall()
    assert sum(row["status"] == "pending" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "wait" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "silent" for row in rows) == 1
    assert sum(row["payload"]["_queue_receipt"]["feedback"] == "skip" for row in rows) == 38
    assert (await store.get_state("longpoll_ts")) == "batch-ts"
    job = await store.claim("bot_inbox")
    await store.plan(
        job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}]
    )
    assert (await store.claim("bot_inbox")) is None
    assert (await store.claim("bot_outbox")) is not None
    assert (await store.claim("bot_outbox")) is None


@pytest.mark.asyncio
async def test_rapid_alternating_buttons_share_one_user_cooldown(store: QueueStore) -> None:
    records = [click(store, f"alternate-{i}", str(i % 3 + 1), cmid=12 + i % 2) for i in range(40)]
    await store.ingest(records)
    job = await store.claim("bot_inbox")
    assert job["payload"]["event_id"] == "alternate-0"
    await store.plan(job, [])
    assert (await store.claim("bot_inbox")) is None
    async with store.connect() as db:
        rows = await (
            await db.execute("SELECT payload FROM bot_inbox WHERE group_id=%s ORDER BY id", (store.group_id,))
        ).fetchall()
    assert sum(r["payload"]["_queue_receipt"]["throttled"] for r in rows) == 39


@pytest.mark.asyncio
async def test_delayed_same_click_stays_coalesced_after_cooldown_and_restart(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        await store.ingest([click(store, "a")])
    job = await store.claim("bot_inbox")
    await store.plan(
        job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12, "message": "codes"}}]
    )
    effect = await store.claim("bot_outbox")
    await store.fail("bot_outbox", effect, "timeout", 0)
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=120):
        await recreated.ingest([click(store, "b")])
    receipt = await recreated.callback_feedback("callback:10:b")
    assert receipt["duplicate"]
    assert receipt["feedback"] == "pending"
    assert (await recreated.callback_feedback("callback:10:b")) is None
    assert (await recreated.claim("bot_inbox")) is None
    retry = await recreated.claim("bot_outbox")
    assert retry["id"] == effect["id"]
    await recreated.finish(retry)


@pytest.mark.asyncio
async def test_same_click_is_accepted_after_successful_cooldown(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        await store.ingest([click(store, "a")])
    job = await store.claim("bot_inbox")
    with patch("src.vkbot.queue.time.time", return_value=101):
        await store.plan(job, [])
    with patch("src.vkbot.queue.time.time", return_value=106):
        await store.ingest([click(store, "b")])
    assert (await store.claim("bot_inbox"))["payload"]["event_id"] == "b"


@pytest.mark.asyncio
async def test_failed_click_can_be_explicitly_retried_without_waiting(store: QueueStore) -> None:
    await store.ingest([click(store, "a")])
    job = await store.claim("bot_inbox")
    await store.fail("bot_inbox", job, "invalid", 0, permanent=True)
    await store.ingest([click(store, "b")])
    assert (await store.claim("bot_inbox"))["payload"]["event_id"] == "b"


@pytest.mark.asyncio
async def test_replayed_batch_does_not_reset_feedback_or_gate(store: QueueStore) -> None:
    records = [click(store, "a"), click(store, "b")]
    await store.ingest(records, "1")
    receipt = await store.callback_feedback("callback:10:b")
    assert receipt["feedback"] == "wait"
    previous = await store.get_state("callback_gate:10")
    assert (await store.ingest(records, "2")) == 0
    assert (await store.callback_feedback("callback:10:b")) is None
    assert (await store.get_state("callback_gate:10")) == previous


@pytest.mark.asyncio
async def test_feedback_budget_is_per_user_and_survives_restart(store: QueueStore) -> None:
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        await store.ingest(
            [click(store, "a"), click(store, "b"), click(store, "c", peer=20), click(store, "d", peer=20)]
        )
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=103):
        await recreated.ingest([click(store, "e")])
    assert (await recreated.callback_feedback("callback:10:b"))["feedback"] == "wait"
    assert (await recreated.callback_feedback("callback:20:d"))["feedback"] == "wait"
    assert (await recreated.callback_feedback("callback:10:e"))["feedback"] == "silent"
    with patch("src.vkbot.queue.time.time", return_value=111):
        await recreated.ingest([click(store, "f")])
    assert (await recreated.callback_feedback("callback:10:f"))["feedback"] == "pending"


@pytest.mark.asyncio
async def test_malformed_callback_does_not_block_batch_or_cursor(store: QueueStore) -> None:
    bad = click(store, "bad")
    bad["payload"]["payload"] = {}
    assert (await store.ingest([bad, click(store, "good")], "next")) == 2
    assert (await store.get_state("longpoll_ts")) == "next"
    assert (await store.claim("bot_inbox"))["payload"]["event_id"] == "good"


@pytest.mark.asyncio
async def test_concurrent_click_ingestion_does_not_create_parallel_edits(store: QueueStore) -> None:
    assert sum(await asyncio.gather(*((store.ingest([click(store, f"parallel-{i}")])) for i in range(15)))) == 15
    async with store.connect() as c:
        assert (
            await (
                await c.execute(
                    "SELECT count(*) AS n FROM bot_inbox WHERE group_id=%s AND status='pending'", (store.group_id,)
                )
            ).fetchone()
        )["n"] == 1


@pytest.mark.asyncio
async def test_callback_gate_and_cursor_roll_back_with_failed_batch(store: QueueStore) -> None:
    invalid = record(store, cmid=2)
    invalid["payload"] = {"invalid": object()}
    await store.set_state("longpoll_ts", "old")
    with pytest.raises(TypeError):
        await store.ingest([click(store, "a"), invalid], "new")
    assert (await store.get_state("callback_gate:10")) is None
    assert (await store.get_state("longpoll_ts")) == "old"
    assert (await store.claim("bot_inbox")) is None


@pytest.mark.asyncio
async def test_building_selection_is_a_durable_callback(store: QueueStore) -> None:
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
    assert (await store.ingest([item])) == 1
    claimed = await store.claim("bot_inbox")
    assert claimed["payload"]["payload"] == {"select_building": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"dismiss_choices": True}, {"choice_page": 1}, {"select_building": 0}])
async def test_choice_actions_are_durable_and_deduplicate(store, payload):
    first = click(store, "choice-first")
    first["payload"]["payload"] = payload
    second = click(store, "choice-repeat")
    second["payload"]["payload"] = payload
    await store.ingest([first, second])
    job = await store.claim("bot_inbox")
    assert job["payload"]["payload"] == payload
    assert (await store.claim("bot_inbox")) is None


@pytest.mark.asyncio
async def test_closing_choices_is_atomic_with_the_outbox_plan(store):
    await store.ingest([record(store)])
    job = await store.claim("bot_inbox")
    effect = {
        "method": "edit",
        "params": {"conversation_message_id": 42},
        "context": {"status": "dismissed"},
        "close_choices": True,
    }
    with pytest.raises(TypeError):
        await store.plan(job, [effect, {"method": "send", "params": {"invalid": object()}}])
    assert (await store.choices_closed(10, 42)) is False
    assert (await store.claim("bot_outbox")) is None
    await store.plan(job, [effect])
    recreated = QueueStore(store.dsn, store.group_id)
    assert (await recreated.choices_closed(10, 42)) is True
    assert (await recreated.choices_closed(10, 43)) is False
    assert (await recreated.claim("bot_outbox"))["context"]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_late_page_cannot_restore_a_rejected_menu_before_delivery(store):
    from unittest.mock import AsyncMock, MagicMock

    from src.vkbot.processor import Processor

    # An old page edit may still be in outbox when rejection is planned.
    await store.ingest([record(store)])
    job = await store.claim("bot_inbox")
    choices = {"status": "ambiguous", "candidates": [{"building_id": "a", "address": "Дом"}]}
    await store.plan(job, [{"method": "edit", "params": {"conversation_message_id": 12}, "context": choices}])
    old_page = await store.claim("bot_outbox")
    await store.finish(old_page, (12, choices))
    reject = click(store, "reject")
    reject["payload"]["payload"] = {"dismiss_choices": True}
    await store.ingest([reject])
    processor = object.__new__(Processor)
    processor.store = store
    processor._get = AsyncMock(return_value=MagicMock())
    await processor.plan(await store.claim("bot_inbox"))
    assert await store.choices_closed(10, 12)
    # Context is still the delivered old menu until the rejection is delivered.
    assert (await store.message_context(10, 12))["status"] == "ambiguous"
    stale_page = click(store, "late-page")
    stale_page["payload"]["payload"] = {"choice_page": 0}
    await store.ingest([stale_page])
    assert (await store.claim("bot_inbox")) is None
    rejection = await store.claim("bot_outbox")
    await store.finish(rejection, (12, rejection["context"]))
    assert (await store.message_context(10, 12))["status"] == "dismissed"
    assert (await store.claim("bot_outbox")) is None
    processor._get.assert_not_called()


@pytest.mark.asyncio
async def test_back_reopens_choices_only_after_confirmed_edit_and_survives_retry(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from src.vkbot.processor import Processor, address_choices

    choices = {
        "status": "ambiguous",
        "candidates": [{"building_id": str(i), "address": f"Город {i}, дом 18"} for i in range(8)],
    }
    await store.ingest([record(store)])
    await store.plan((await store.claim("bot_inbox")), [{"method": "send", "params": {}, "context": choices}])
    await store.finish((await store.claim("bot_outbox")), (42, choices))
    processor = object.__new__(Processor)
    processor.store = store
    processor.settings = SimpleNamespace(domofomka_api_host="api", domofomka_api_port=8000, dev_faults_enabled=False)
    processor.vk = MagicMock()
    processor._get = AsyncMock(return_value=MagicMock())
    processor._get.return_value.status_code = 200
    processor._get.return_value.json.return_value = {"address": "Город 6, дом 18", "data": {"1": [["TEST", "test"]]}}

    async def plan(action, name):
        item = click(store, name, cmid=42)
        item["payload"]["payload"] = action
        await store.ingest([item], callback_window=0)
        await processor.plan(await store.claim("bot_inbox"))
        return await store.claim("bot_outbox")

    selected = await plan({"select_building": 6}, "select-six")
    await processor.deliver(selected)
    assert await store.choices_closed(10, 42)
    assert (await store.message_context(10, 42))["address"] == "Город 6, дом 18"
    back = await plan({"return_to_choices": True}, "back")
    assert back["params"]["message"] == address_choices(choices, 1)[0]
    assert await store.choices_closed(10, 42)
    processor.vk.messages.edit.side_effect = [TimeoutError(), 1]
    with pytest.raises(TimeoutError):
        await processor.deliver(back)
    await store.fail("bot_outbox", back, "timeout", delay=0)
    recreated = QueueStore(store.dsn, store.group_id, lease_seconds=3)
    processor.store = recreated
    assert await recreated.choices_closed(10, 42)
    assert "_address_choice" in (await recreated.message_context(10, 42))
    retry = await recreated.claim("bot_outbox")
    assert retry["params"] == back["params"]
    await processor.deliver(retry)
    assert (await recreated.message_context(10, 42)) == choices
    assert (await recreated.choices_closed(10, 42)) is False
    assert processor._get.call_count == 1
    processor.vk.messages.edit.side_effect = None
    await plan({"select_building": 3}, "select-three")
    assert processor._get.call_args.args[0].endswith("/api/v2/buildings/3")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [{"return_to_choices": True}, {"entrance_page": 1}])
async def test_new_navigation_callbacks_share_existing_cooldown(store, action):
    records = []
    for i in range(3):
        item = click(store, f"navigation-{i}")
        item["payload"]["payload"] = action
        records.append(item)
    await store.ingest(records)
    assert (await store.claim("bot_inbox")) is not None
    assert (await store.claim("bot_inbox")) is None
    assert (await store.callback_feedback("callback:10:navigation-1"))["throttled"]


@pytest.mark.asyncio
async def test_cooldown_starts_after_delivery_not_before_a_slow_request(store):
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        await store.ingest([click(store, "slow")])
    job = await store.claim("bot_inbox")
    await store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12}}])
    outbox = await store.claim("bot_outbox")
    with patch("src.vkbot.queue.time.time", return_value=120):
        await store.finish(outbox)
    with patch("src.vkbot.queue.time.time", return_value=121):
        await store.ingest([click(store, "too-soon", "2")])
    receipt = await store.callback_feedback("callback:10:too-soon")
    assert receipt["throttled"]
    assert receipt["wait_seconds"] == 4
    assert (await store.claim("bot_inbox")) is None
    with patch("src.vkbot.queue.time.time", return_value=126):
        await store.ingest([click(store, "after-pause", "2")])
    assert (await store.claim("bot_inbox"))["payload"]["event_id"] == "after-pause"


@pytest.mark.asyncio
async def test_pending_callback_blocks_different_buttons_after_restart(store):
    from unittest.mock import patch

    with patch("src.vkbot.queue.time.time", return_value=100):
        await store.ingest([click(store, "pending")])
    job = await store.claim("bot_inbox")
    await store.plan(job, [{"method": "edit", "params": {"peer_id": 10, "conversation_message_id": 12}}])
    recreated = QueueStore(store.dsn, store.group_id)
    with patch("src.vkbot.queue.time.time", return_value=120):
        await recreated.ingest([click(store, "other-button", "2", cmid=99)])
    assert (await recreated.claim("bot_inbox")) is None
    assert (await recreated.callback_feedback("callback:10:other-button"))["feedback"] == "pending"

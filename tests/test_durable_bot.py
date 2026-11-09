import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from requests.exceptions import ReadTimeout

from src.vkbot.processor import LookupUnavailable, Processor, RateGate, RateSession, code_answer
from src.vkbot.queue import event_record
from src.vkbot.recovery import Reconciler
from src.vkbot.runtime import Runtime, validate_environment


@pytest.fixture
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        vk_group_token="test-token",
        vk_group_id="223949627",
        expected_vk_group_id="223949627",
        app_environment="development",
        dev_faults_enabled=True,
        vk_error_pause_seconds=30,
        queue_workers=2,
        queue_lease_seconds=120,
        domofomka_api_host="api",
        domofomka_api_port=8000,
        anti_spam_time=5,
        geo_retry_delay_seconds=60,
        geo_retry_max_age_seconds=1800,
        recovery_overlap_seconds=15,
        recovery_bootstrap_seconds=100,
    )


@pytest.fixture
def processor(settings: SimpleNamespace) -> Processor:
    store = MagicMock()
    store.renew.return_value = True
    store.consume_fault.return_value = False
    store.get_state.return_value = 0
    p = Processor(settings, store, RateGate(0))
    p.vk = MagicMock()
    p.vk.groups.isMember.return_value = 1
    p.vk.messages.send.return_value = 77
    p.vk.messages.getById.return_value = {"items": [{"conversation_message_id": 12}]}
    return p


def test_message_identity_matches_longpoll_and_history() -> None:
    message = {"id": 42, "peer_id": 100, "from_id": 100, "conversation_message_id": 7, "date": 1}
    a = event_record(1, "message_new", {"message": message}, now=2)
    b = event_record(1, "message_new", {"message": dict(message)}, now=3)
    assert a["event_key"] == b["event_key"]
    assert a["occurred_at"] == 1


def test_callback_identity_preserves_payload_for_replay() -> None:
    obj = {"peer_id": 100, "user_id": 100, "event_id": "event-a", "payload": {"entrance": "1"}}
    record = event_record(1, "message_event", obj, now=10)
    assert record["payload"] == obj
    assert record["event_key"] == "callback:100:event-a"


def test_outgoing_messages_are_not_reprocessed() -> None:
    with pytest.raises(ValueError, match="incoming private"):
        event_record(1, "message_new", {"message": {"peer_id": 1, "from_id": 1, "out": 1}})


def test_failed_send_is_retried_with_persistent_random_id(processor: Processor) -> None:
    job = {
        "id": 123,
        "owner": "lease",
        "params": {"peer_id": 100, "message": "reply"},
        "method": "send",
        "context": None,
        "result": None,
    }
    processor.vk.messages.send.side_effect = [ReadTimeout(), 77]
    with pytest.raises(ReadTimeout):
        processor.deliver(job)
    processor.store.finish.assert_not_called()
    processor.deliver(job)
    assert [c.kwargs["random_id"] for c in processor.vk.messages.send.call_args_list] == [123, 123]
    processor.store.finish.assert_called_once()


def test_saved_send_result_does_not_send_again(processor: Processor) -> None:
    job = {"id": 123, "params": {"peer_id": 100}, "method": "send", "context": {"data": {}}, "result": 77}
    processor.deliver(job)
    processor.vk.messages.send.assert_not_called()
    processor.store.finish.assert_called_once_with(job, (12, {"data": {}}))


def test_timeout_after_vk_acceptance_preserves_same_send_identity(processor: Processor) -> None:
    job = {"id": 123, "params": {"peer_id": 100, "message": "reply"}, "method": "send", "context": None, "result": None}
    faults = {"vk-send-after": 1}

    def consume(name: str) -> bool:
        if faults.get(name, 0):
            faults[name] -= 1
            return True
        return False

    processor.store.consume_fault.side_effect = consume
    with pytest.raises(TimeoutError):
        processor.deliver(job)
    processor.store.save_result.assert_not_called()
    processor.deliver(job)
    assert processor.vk.messages.send.call_count == 2
    assert all(c.kwargs["random_id"] == 123 for c in processor.vk.messages.send.call_args_list)


def test_received_click_can_retry_identical_edit(processor: Processor) -> None:
    params = {"peer_id": 100, "conversation_message_id": 12, "message": "codes", "keyboard": "{}"}
    job = {"id": 55, "method": "edit", "params": params, "context": None}
    processor.vk.messages.edit.side_effect = [ReadTimeout(), 1]
    with pytest.raises(ReadTimeout):
        processor.deliver(job)
    processor.deliver(job)
    assert processor.vk.messages.edit.call_args_list[0] == processor.vk.messages.edit.call_args_list[1]
    processor.store.finish.assert_called_once()


def test_callback_uses_durable_context_without_redis(processor: Processor) -> None:
    result = {"address": "address", "data": {"1": [["code", "yaeda"]]}}
    processor.store.message_context.return_value = result
    job = {
        "peer_id": 100,
        "occurred_at": 100,
        "kind": "message_event",
        "payload": {
            "peer_id": 100,
            "user_id": 100,
            "conversation_message_id": 12,
            "payload": {"entrance": "1", "ent_slice": ["1"]},
            "event_id": "a",
        },
    }
    processor.plan(job)
    effects = processor.store.plan.call_args.args[1]
    assert effects[0]["method"] == "edit"
    assert "YAEDA:" in effects[0]["params"]["message"]
    processor.vk.messages.getByConversationMessageId.assert_not_called()


def test_missing_old_context_rebuilds_from_original_message(processor: Processor) -> None:
    processor.store.message_context.return_value = None
    processor.vk.messages.getByConversationMessageId.return_value = {
        "items": [{"text": "address\nПодъезд 1\nold codes"}]
    }
    processor.lookup = MagicMock(return_value={"address": "address", "data": {"1": [["code", "delivery"]]}})
    job = {
        "peer_id": 100,
        "occurred_at": 100,
        "kind": "message_event",
        "payload": {"conversation_message_id": 12, "payload": {"entrance": "1", "ent_slice": ["1"]}},
    }
    processor.plan(job)
    processor.lookup.assert_called_once_with("address")
    assert processor.store.plan.call_args.args[1][0]["method"] == "edit"


def test_lease_loss_prevents_dispatch(processor: Processor) -> None:
    processor.store.renew.return_value = False
    with pytest.raises(RuntimeError):
        processor.deliver({"params": {}, "method": "send"})
    processor.vk.messages.send.assert_not_called()


def test_environment_guard_blocks_production_token_mixup(settings: SimpleNamespace) -> None:
    settings.vk_group_id = "213361259"
    with pytest.raises(ValueError, match="match|Fault injection"):
        validate_environment(settings)


def test_faults_cannot_be_enabled_on_production(settings: SimpleNamespace) -> None:
    settings.app_environment = "production"
    with pytest.raises(ValueError, match="match|Fault injection"):
        validate_environment(settings)


def test_unknown_code_type_does_not_crash_formatting() -> None:
    assert "OTHER:" in code_answer({"address": "a", "data": {"1": [["x", "other"]]}}, "1")


def test_recovery_uses_watermark_and_preserves_untracked_new_requests(settings: SimpleNamespace) -> None:
    store = MagicMock(group_id=1)
    store.get_state.side_effect = lambda key, default=None: {"tracking_started": 100, "reconcile_watermark": 200}.get(
        key, default
    )
    vk = MagicMock()
    vk.messages.getConversations.return_value = {
        "count": 1,
        "items": [{"conversation": {"peer": {"type": "user", "id": 10}}, "last_message": {"date": 205}}],
    }
    vk.messages.getHistory.return_value = {
        "count": 3,
        "items": [
            {"id": 3, "peer_id": 10, "from_id": 10, "date": 205, "out": 1},
            {"id": 2, "peer_id": 10, "from_id": 10, "date": 195, "out": 0},
            {"id": 1, "peer_id": 10, "from_id": 10, "date": 180, "out": 0},
        ],
    }
    store.ingest.return_value = 1
    assert Reconciler(vk, store, settings).run() == 1
    records = store.ingest.call_args.args[0]
    assert len(records) == 1
    assert records[0]["payload"]["message"]["id"] == 2
    assert records[0].get("status", "pending") == "pending"


def test_recovery_does_not_advance_checkpoint_on_failure(settings: SimpleNamespace) -> None:
    store = MagicMock(group_id=1)
    store.get_state.return_value = time.time()
    vk = MagicMock()
    vk.messages.getConversations.side_effect = ReadTimeout()
    with pytest.raises(ReadTimeout):
        Reconciler(vk, store, settings).run()
    store.set_state.assert_not_called()


def test_fast_distinct_click_is_not_discarded(processor: Processor) -> None:
    processor.store.get_state.return_value = 100
    processor.store.message_context.return_value = {"address": "a", "data": {"2": [["c", "yaeda"]]}}
    job = {
        "peer_id": 100,
        "occurred_at": 101,
        "kind": "message_event",
        "payload": {"conversation_message_id": 12, "payload": {"entrance": "2", "ent_slice": ["2"]}},
    }
    processor.plan(job)
    assert len(processor.store.plan.call_args.args[1]) == 1


def test_wrong_community_token_stops_before_queue_or_workers(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    runtime = object.__new__(Runtime)
    runtime.settings = settings
    runtime.store = MagicMock(group_id=223949627)
    runtime.gate = RateGate(0)
    client = MagicMock()
    client.method.side_effect = ValueError("token belongs to another community")
    monkeypatch.setattr("src.vkbot.runtime.VkApi", MagicMock(return_value=client))
    with pytest.raises(ValueError, match="another community"):
        asyncio.run(runtime.run())
    runtime.store.initialize.assert_not_called()
    runtime.store.connect.assert_not_called()


def test_expired_callback_ack_does_not_abort_durable_processing(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = object.__new__(Runtime)
    runtime.settings = settings
    runtime.gate = RateGate(0)
    runtime.store = MagicMock()
    runtime.store.get_state.return_value = 0
    runtime.store.callback_feedback.return_value = {"feedback": "silent"}
    client = MagicMock()
    client.get_api.return_value.messages.sendMessageEventAnswer.side_effect = ReadTimeout()
    monkeypatch.setattr("src.vkbot.runtime.VkApi", MagicMock(return_value=client))
    runtime._acknowledge(
        [
            {
                "kind": "message_event",
                "event_key": "callback:100:x",
                "payload": {"event_id": "x", "user_id": 100, "peer_id": 100},
            }
        ]
    )
    client.get_api.return_value.messages.sendMessageEventAnswer.assert_called_once()


@pytest.mark.parametrize(
    ("feedback", "text"),
    [("silent", None), ("wait", "Подождите 4 сек."), ("pending", "Запрос сохранён, повторно нажимать не нужно.")],
)
def test_callback_feedback_only_shows_text_when_needed(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, feedback: str, text: str | None
) -> None:
    import json

    runtime = object.__new__(Runtime)
    runtime.settings = settings
    runtime.gate = RateGate(0)
    runtime.store = MagicMock()
    runtime.store.get_state.return_value = 0
    runtime.store.callback_feedback.return_value = {"feedback": feedback, "wait_seconds": 4}
    client = MagicMock()
    monkeypatch.setattr("src.vkbot.runtime.VkApi", MagicMock(return_value=client))
    runtime._acknowledge(
        [
            {
                "kind": "message_event",
                "event_key": "callback:100:x",
                "payload": {"event_id": "x", "user_id": 100, "peer_id": 100},
            }
        ]
    )
    params = client.get_api.return_value.messages.sendMessageEventAnswer.call_args.kwargs
    if text is None:
        assert "event_data" not in params
    else:
        assert json.loads(params["event_data"])["text"] == text


def test_callback_feedback_rate_limit_does_not_call_vk(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = object.__new__(Runtime)
    runtime.settings = settings
    runtime.gate = RateGate(0)
    runtime.store = MagicMock()
    runtime.store.get_state.return_value = 0
    runtime.store.callback_feedback.return_value = None
    client = MagicMock()
    monkeypatch.setattr("src.vkbot.runtime.VkApi", MagicMock(return_value=client))
    runtime._acknowledge([{"kind": "message_event", "event_key": "callback:100:x"}])
    client.get_api.return_value.messages.sendMessageEventAnswer.assert_not_called()


def test_rate_gate_applies_to_each_actual_http_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from requests import Response, Session

    gate = MagicMock()
    response = Response()
    response.status_code = 200
    response._content = b"{}"
    send = MagicMock(side_effect=[ReadTimeout(), response])
    monkeypatch.setattr(Session, "send", send)
    monkeypatch.setattr("src.vkbot.http.time.sleep", MagicMock())
    result = RateSession(gate).post("https://api.vk.com/method/messages.send", data={"peer_id": 100, "random_id": 99})
    assert result is response
    assert gate.wait.call_count == 2


def test_acknowledgement_flood_response_pauses_vk_and_stops_batch(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = object.__new__(Runtime)
    runtime.settings = settings
    runtime.gate = RateGate(0)
    runtime.store = MagicMock()
    runtime.store.get_state.return_value = 0
    runtime.store.callback_feedback.return_value = {"feedback": "silent"}
    error = RuntimeError("rate limited")
    error.code = 6
    client = MagicMock()
    client.get_api.return_value.messages.sendMessageEventAnswer.side_effect = error
    monkeypatch.setattr("src.vkbot.runtime.VkApi", MagicMock(return_value=client))
    events = [
        {
            "kind": "message_event",
            "event_key": f"callback:100:{i}",
            "payload": {"event_id": str(i), "peer_id": 100, "user_id": 100},
        }
        for i in range(2)
    ]
    runtime._acknowledge(events)
    client.get_api.return_value.messages.sendMessageEventAnswer.assert_called_once()
    assert runtime.store.set_state.call_args.args[0] == "vk_paused_until"


def test_geolocation_failure_plans_text_fallback_instead_of_blocking_peer(processor: Processor) -> None:
    processor.lookup = MagicMock(side_effect=LookupUnavailable())
    job = {
        "kind": "message_new",
        "peer_id": 100,
        "payload": {"message": {"id": 42, "text": "", "geo": {"coordinates": {"latitude": 1, "longitude": 2}}}},
    }
    processor.plan(job)
    effects = processor.store.plan.call_args.args[1]
    assert effects[0]["method"] == "send"
    assert "адрес текстом" in effects[0]["params"]["message"]


@pytest.mark.parametrize("kind", ["message_new", "geo_retry"])
def test_geo_access_failure_finishes_with_notice_instead_of_enqueuing_retries(processor, kind):
    processor._get = MagicMock()
    processor._get.return_value.status_code = 503
    processor._get.return_value.json.return_value = {"extra": {"geocoding_retryable": False}}
    job = {
        "kind": kind,
        "peer_id": 100,
        "payload": {"message": {"id": 42, "text": "", "geo": {"coordinates": {"latitude": 1, "longitude": 2}}}},
    }
    processor.plan(job)
    effects = processor.store.plan.call_args.args[1]
    assert len(effects) == 1
    assert "временно недоступно" in effects[0]["params"]["message"]
    assert "defer_geo" not in effects[0]
    processor.store.defer_geo_retry.assert_not_called()


def test_access_error_marker_does_not_disable_text_retries(processor):
    processor._get = MagicMock()
    processor._get.return_value.status_code = 503
    processor._get.return_value.json.return_value = {"extra": {"geocoding_retryable": False}}
    with pytest.raises(LookupUnavailable):
        processor.lookup("Москва Тестовая 1")


def test_text_lookup_failure_keeps_durable_retry_behavior(processor: Processor) -> None:
    processor.lookup = MagicMock(side_effect=LookupUnavailable())
    with pytest.raises(LookupUnavailable):
        processor.plan({"kind": "message_new", "peer_id": 100, "payload": {"message": {"id": 42, "text": "address"}}})
    processor.store.plan.assert_not_called()


def test_lookup_passes_internal_query_to_http_client(processor: Processor) -> None:
    processor._get = MagicMock()
    response = processor._get.return_value
    response.status_code = 200
    response.json.return_value = {"address": "a", "data": {}}
    assert processor.lookup("address") == {"address": "a", "data": {}}
    processor._get.assert_called_once_with("http://api:8000/api/v1/codes/msg", {"message": "address"}, timeout=None)


def test_lookup_status_failure_is_retryable_without_requests_dependency(processor: Processor) -> None:
    processor._get = MagicMock()
    processor._get.return_value.status_code = 503
    with pytest.raises(LookupUnavailable):
        processor.lookup(geo={"latitude": 1, "longitude": 2})


def test_known_house_without_codes_explains_result(processor: Processor) -> None:
    processor.lookup = MagicMock(return_value={"status": "no_codes", "address": "Known house", "data": {}})
    processor.plan({"kind": "message_new", "peer_id": 100, "payload": {"message": {"id": 1, "text": "house"}}})
    effect = processor.store.plan.call_args.args[1][0]
    assert "Known house" in effect["params"]["message"]
    assert effect["method"] == "send"


@pytest.mark.parametrize("entrance_count", [1, 13])
def test_ambiguous_choice_edits_the_same_message(processor: Processor, entrance_count: int) -> None:
    processor.store.message_context.return_value = {
        "candidates": [{"building_id": "gar:test", "address": "Known house"}]
    }
    processor._get = MagicMock()
    response = processor._get.return_value
    response.status_code = 200
    response.json.return_value = {
        "address": "Known house",
        "data": {str(i): [["TEST", "test"]] for i in range(1, entrance_count + 1)},
    }
    processor.plan(
        {
            "kind": "message_event",
            "peer_id": 100,
            "payload": {"conversation_message_id": 42, "payload": {"select_building": 0}},
        }
    )
    effect = processor.store.plan.call_args.args[1][0]
    assert effect["method"] == "edit"
    assert effect["params"]["conversation_message_id"] == 42
    assert effect["context"]["data"]
    effects = processor.store.plan.call_args.args[1]
    labels = {
        button["action"]["label"]
        for effect in effects
        for row in json.loads(effect["params"]["keyboard"])["buttons"]
        for button in row
    }
    assert labels >= {str(i) for i in range(1, min(entrance_count, 7) + 1)}
    assert "Назад к адресам" in labels
    assert ("Следующие подъезды" in labels) is (entrance_count > 7)
    assert len(effects) == 1


def test_address_choices_can_be_paged_without_repeating_lookup(processor: Processor) -> None:
    from src.vkbot.processor import address_choices

    result = {
        "status": "ambiguous",
        "candidates": [{"building_id": str(i), "address": f"City {i}, house"} for i in range(12)],
    }
    text, keyboard = address_choices(result, 1)
    assert "City 5" in text
    assert "City 0" not in text
    assert "choice_page" in keyboard
    processor.store.message_context.return_value = result
    processor.plan(
        {
            "kind": "message_event",
            "peer_id": 100,
            "payload": {"conversation_message_id": 42, "payload": {"choice_page": 1}},
        }
    )
    effect = processor.store.plan.call_args.args[1][0]
    assert effect["method"] == "edit"
    assert effect["params"]["conversation_message_id"] == 42


def test_long_query_does_not_poison_retry_queue(processor: Processor) -> None:
    assert processor.lookup("x" * 501)["status"] == "invalid_query"


@pytest.mark.parametrize(("limit", "length", "rejected"), [(20, 21, True), (600, 501, False)])
def test_bot_query_length_uses_environment_setting(processor, monkeypatch, limit, length, rejected):
    for key, value in {
        "VK_GROUP_TOKEN": "unused",
        "VK_GROUP_ID": "223949627",
        "EXPECTED_VK_GROUP_ID": "223949627",
        "QUEUE_DATABASE_URL": "postgresql://unused",
    }.items():
        monkeypatch.setenv(key, value)
    from src.vkbot.config import VKBotSettings

    monkeypatch.setenv("SEARCH_MAX_QUERY_LENGTH", str(limit))
    processor.settings = VKBotSettings(
        _env_file=None,
        vk_group_token="unused",
        vk_group_id="223949627",
        expected_vk_group_id="223949627",
        cache_expire_time=1200,
        anti_spam_time=5,
        domofomka_api_host="api",
        domofomka_api_port=8000,
        queue_database_url="postgresql://unused",
    )
    processor._get = MagicMock()
    processor._get.return_value.status_code = 200
    processor._get.return_value.json.return_value = {"status": "not_found"}
    answer = processor.lookup("x" * length)
    assert (answer["status"] == "invalid_query") is rejected
    assert processor._get.called is not rejected


@pytest.mark.parametrize("count", [1, 5, 6, 12, 100])
def test_choice_pages_have_rejection_and_fit_button_budget(count):
    from src.vkbot.processor import address_choices

    result = {
        "status": "ambiguous",
        "candidates": [{"address": f"Город {i}, дом", "building_id": str(i)} for i in range(count)],
    }
    seen = []
    for page in range((count + 4) // 5):
        text, encoded = address_choices(result, page)
        rows = json.loads(encoded)["buttons"]
        buttons = [b for row in rows for b in row]
        assert len(buttons) <= 10
        assert all(0 < len(row) <= 5 for row in rows)
        assert sum(json.loads(b["action"]["payload"]).get("dismiss_choices") is True for b in buttons) == 1
        seen.extend(
            json.loads(b["action"]["payload"])["select_building"]
            for b in buttons
            if "select_building" in json.loads(b["action"]["payload"])
        )
        assert "адрес целиком одним сообщением" in text
        assert "Можно добавить город" not in text
    assert seen == list(range(count))


@pytest.mark.parametrize("context", [None, {"status": "ambiguous", "candidates": [{"building_id": "a"}]}])
def test_rejecting_addresses_clears_buttons_in_the_same_message(processor, context):
    processor.store.message_context.return_value = context
    processor.plan(
        {
            "kind": "message_event",
            "peer_id": 100,
            "payload": {"conversation_message_id": 42, "payload": {"dismiss_choices": True}},
        }
    )
    effect = processor.store.plan.call_args.args[1][0]
    assert effect["method"] == "edit"
    assert effect["params"]["conversation_message_id"] == 42
    assert json.loads(effect["params"]["keyboard"])["buttons"] == []
    assert "одним сообщением" in effect["params"]["message"]
    assert effect["close_choices"] is True
    assert effect["context"]["status"] == "dismissed"


@pytest.mark.parametrize("action", [{"dismiss_choices": True}, {"choice_page": 1}, {"select_building": 0}])
@pytest.mark.parametrize("context", [{"status": "found", "data": {"3": []}}, {"data": {"3": []}}])
def test_old_choice_ignores_completed_context_without_close_marker(processor, action, context):
    processor._get = MagicMock()
    processor.store.message_context.return_value = context
    processor.store.choices_closed.return_value = False
    processor.plan(
        {"kind": "message_event", "peer_id": 100, "payload": {"conversation_message_id": 42, "payload": action}}
    )
    assert processor.store.plan.call_args.args[1] == []
    processor._get.assert_not_called()


@pytest.mark.parametrize("action", [{"dismiss_choices": True}, {"choice_page": 1}, {"select_building": 0}])
def test_old_choices_cannot_reopen_a_finalized_menu(processor, action):
    processor.store.message_context.return_value = {"status": "ambiguous", "candidates": [{"building_id": "a"}]}
    processor.store.choices_closed.return_value = True
    processor.plan(
        {"kind": "message_event", "peer_id": 100, "payload": {"conversation_message_id": 42, "payload": action}}
    )
    assert processor.store.plan.call_args.args[1] == []


@pytest.mark.parametrize("count", [10, 11, 17, 26, 100])
def test_every_entrance_is_kept_in_direct_responses(processor, count):
    processor.lookup = MagicMock(
        return_value={
            "status": "found",
            "address": "Адрес",
            "data": {str(i): [["CODE", "test"]] for i in range(1, count + 1)},
        }
    )
    processor.plan({"kind": "message_new", "peer_id": 100, "payload": {"message": {"id": 1, "text": "адрес"}}})
    effects = processor.store.plan.call_args.args[1]
    labels = []
    for effect in effects:
        rows = json.loads(effect["params"]["keyboard"])["buttons"]
        assert sum(map(len, rows)) <= 10
        assert all(len(row) <= 5 for row in rows)
        labels.extend(b["action"]["label"] for row in rows for b in row)
    assert labels == [str(i) for i in range(1, count + 1)]


def test_inline_keyboard_rejects_overflow_before_sending_to_vk():
    from src.vkbot.processor import inline_keyboard

    with pytest.raises(ValueError, match="button budget"):
        inline_keyboard([str(i) for i in range(11)])


@pytest.mark.parametrize("status", ["incomplete_query", "no_codes_ambiguous"])
def test_incomplete_or_empty_address_search_does_not_emit_buttons(processor, status):
    processor.lookup = MagicMock(return_value={"status": status, "data": {}})
    processor.plan({"kind": "message_new", "peer_id": 100, "payload": {"message": {"id": 1, "text": "город"}}})
    effects = processor.store.plan.call_args.args[1]
    assert len(effects) == 1
    assert "целиком одним сообщением" in effects[0]["params"]["message"]
    assert "keyboard" not in effects[0]["params"]

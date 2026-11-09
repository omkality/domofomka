"""Навигация редактируемого ответа без повторного поиска и потери списка адресов."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.vkbot.processor import Processor, RateGate, address_choices


@pytest.fixture
def processor():
    settings = SimpleNamespace(
        vk_group_token="test", domofomka_api_host="api", domofomka_api_port=8000, dev_faults_enabled=False
    )
    store = MagicMock()
    store.choices_closed.return_value = False
    processor = Processor(settings, store, RateGate(0))
    processor.vk._vk.http.close()
    processor.vk = MagicMock()
    processor._get = MagicMock()
    processor._get.return_value.status_code = 200
    processor._get.return_value.json.return_value = {
        "status": "found",
        "address": "Город, улица Космонавтов, дом 18",
        "data": {"1": [["TEST", "test"]]},
    }
    return processor


def choices(count=12):
    return {
        "status": "ambiguous",
        "candidates": [
            {
                "building_id": str(i),
                "address": f"Город {i}, улица Космонавтов, дом 18",
                "locality_context": f"город {i} область",
            }
            for i in range(count)
        ],
    }


def click(processor, context, payload):
    processor.store.message_context.return_value = context
    processor.plan(
        {
            "kind": "message_event",
            "peer_id": 100,
            "occurred_at": 100,
            "payload": {"conversation_message_id": 42, "payload": payload},
        }
    )
    return processor.store.plan.call_args.args[1]


def buttons(effect):
    rows = json.loads(effect["params"]["keyboard"])["buttons"]
    assert sum(map(len, rows)) <= 10
    assert all(0 < len(row) <= 5 for row in rows)
    return [button for row in rows for button in row]


def payload_for(effect, key):
    return next(
        json.loads(b["action"]["payload"]) for b in buttons(effect) if key in json.loads(b["action"]["payload"])
    )


def test_menu_does_not_show_internal_normalized_locality():
    text, _ = address_choices(choices())
    assert "1. Город 0, улица Космонавтов, дом 18\n" in text
    assert "(город" not in text


def test_back_restores_original_page_after_selection_and_code(processor):
    original = choices()
    chosen = click(processor, original, {"select_building": 6})[0]
    processor.store.choices_closed.return_value = True
    entrance = payload_for(chosen, "entrance")
    code = click(processor, chosen["context"], entrance)[0]
    assert "TEST" in code["params"]["message"]
    back = payload_for(code, "return_to_choices")
    restored = click(processor, code["context"], back)[0]
    expected_text, expected_keyboard = address_choices(original, 1)
    assert restored["method"] == "edit"
    assert restored["params"]["conversation_message_id"] == 42
    assert restored["params"]["message"] == expected_text
    assert restored["params"]["keyboard"] == expected_keyboard
    assert processor._get.call_count == 1


@pytest.mark.parametrize("count", [0, 1, 7, 8, 10, 13, 100])
def test_selected_building_paginates_entrances_and_keeps_back(processor, count):
    processor._get.return_value.json.return_value = {
        "address": "Адрес",
        "data": {str(i): [["TEST", "test"]] for i in range(count)},
    }
    effects = click(processor, choices(), {"select_building": 6})
    assert len(effects) == 1
    seen = []
    for page in range(max(1, (count + 6) // 7)):
        effect = effects[0]
        actions = [json.loads(b["action"]["payload"]) for b in buttons(effect)]
        assert sum(a.get("return_to_choices") is True for a in actions) == 1
        seen.extend(a["entrance"] for a in actions if "entrance" in a)
        if page + 1 < max(1, (count + 6) // 7):
            action = next(a for a in actions if a.get("entrance_page") == page + 1)
            effects = click(processor, effect["context"], action)
    assert seen == [str(i) for i in range(count)]
    assert processor._get.call_count == 1


def test_stale_back_cannot_reopen_a_dismissed_or_direct_response(processor):
    for context in [{"status": "dismissed"}, {"status": "found", "data": {"1": []}}, choices()]:
        assert click(processor, context, {"return_to_choices": True}) == []
    processor._get.assert_not_called()


def test_expired_back_clears_buttons_and_asks_for_complete_address(processor):
    effect = click(processor, None, {"return_to_choices": True})[0]
    assert "Срок хранения" in effect["params"]["message"]
    assert buttons(effect) == []


def test_missing_building_still_allows_return(processor):
    processor._get.return_value.status_code = 404
    chosen = click(processor, choices(), {"select_building": 6})[0]
    back = payload_for(chosen, "return_to_choices")
    assert click(processor, chosen["context"], back)[0]["params"]["message"].startswith("Уточните адрес:")

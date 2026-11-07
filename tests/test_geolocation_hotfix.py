import asyncio
import importlib
import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.dadata_client import DadataClient, DadataUnavailable


@pytest.fixture(autouse=True)
def reset_geocoding_breaker() -> Iterator[None]:
    DadataClient._unavailable_until = 0
    yield
    DadataClient._unavailable_until = 0


@pytest.fixture
def builder(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("src.services.dadata_client.httpx.AsyncClient", MagicMock(return_value=client))
    return client.post


def test_geocoding_timeout_opens_breaker_for_new_instances(builder: MagicMock) -> None:
    builder.side_effect = TimeoutError()

    async def run() -> None:
        for _ in range(2):
            with pytest.raises(DadataUnavailable):
                await DadataClient("fake-token").geolocate(1, 2)

    asyncio.run(run())
    builder.assert_awaited_once()


def test_total_deadline_cancels_stalled_geocoding(builder: MagicMock) -> None:
    async def stalled() -> None:
        await asyncio.sleep(10)

    builder.side_effect = stalled

    async def run() -> None:
        with pytest.raises(DadataUnavailable):
            await asyncio.wait_for(DadataClient("fake", timeout_seconds=0.02).geolocate(1, 2), timeout=1)

    asyncio.run(run())


def test_unsuccessful_geocoding_is_not_reported_as_no_results(builder: MagicMock) -> None:
    builder.return_value = SimpleNamespace(status_code=503)
    with pytest.raises(DadataUnavailable):
        asyncio.run(DadataClient("fake").get_address_by_geo(1, 2))


def test_legitimate_empty_geocoding_result_remains_empty(builder: MagicMock) -> None:
    builder.return_value = SimpleNamespace(status_code=200, json=MagicMock(return_value={"suggestions": []}))
    assert asyncio.run(DadataClient("fake").get_address_by_geo(1, 2)) is None


def test_missing_block_type_does_not_crash_geocoding(builder: MagicMock) -> None:
    data = {"city": "Город", "street": "Улица", "house": "1", "block": "2", "block_type": None}
    builder.return_value = SimpleNamespace(
        status_code=200, json=MagicMock(return_value={"suggestions": [{"data": data}]})
    )
    assert asyncio.run(DadataClient("fake").get_address_by_geo(1, 2)) is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"city": None, "settlement": "Нахабино", "street": "Тестовая", "house": "7"}, "Нахабино Тестовая 7"),
        ({"city": "Москва", "street": "Тестовая", "house": None}, None),
        ({"city": None, "street": "Тестовая", "house": "7"}, None),
        ({"city": "Москва", "street": "Тестовая", "house": "4", "block_type": "к", "block": "1 стр 1"},
         "Москва Тестовая 4 к1с1"),
    ],
)
def test_geocoding_requires_a_house_and_locality_and_preserves_blocks(builder, data, expected):
    builder.return_value = SimpleNamespace(
        status_code=200, json=MagicMock(return_value={"suggestions": [{"data": data}]})
    )
    assert asyncio.run(DadataClient("fake").get_address_by_geo(1, 2)) == expected


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        vk_group_id="1",
        domofomka_api_host="api",
        domofomka_api_port=8000,
        dadata_token="fake",
        dadata_timeout_seconds=8,
    )
    for name in ("src.vkbot.config", "src.api.config"):
        module = ModuleType(name)
        module.settings = settings
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(
        handlers=importlib.import_module("src.vkbot.handlers"),
        routes=importlib.import_module("src.api.routes"),
        main=importlib.import_module("src.vkbot.main"),
    )


def test_api_returns_service_unavailable_for_geocoding_failure(modules: SimpleNamespace) -> None:
    from litestar.exceptions import ServiceUnavailableException

    client = SimpleNamespace(get_address_by_geo=AsyncMock(side_effect=DadataUnavailable()))
    with pytest.raises(ServiceUnavailableException) as error:
        asyncio.run(modules.routes.get_codes_by_geo.fn(lat=1, lon=2, dadata=client))
    assert error.value.status_code == 503


def test_geo_failure_explains_text_fallback_and_next_text_works(modules: SimpleNamespace) -> None:
    vk = MagicMock()
    handler = modules.handlers.MessageHandler(vk, MagicMock())
    handler.check_subscription = MagicMock(return_value=True)
    handler.get_codes_by_geo = AsyncMock(side_effect=TimeoutError())
    handler.get_codes_by_message = AsyncMock(return_value={})

    async def run() -> None:
        await handler.handle_message(
            {"message": {"from_id": 10, "text": "", "geo": {"coordinates": {"latitude": 1, "longitude": 2}}}}
        )
        await handler.handle_message({"message": {"from_id": 10, "text": "адрес"}})

    asyncio.run(run())
    assert "адрес текстом" in vk.messages.send.call_args_list[0].kwargs["message"]
    assert vk.messages.send.call_args_list[1].kwargs["message"] == "Нет результатов"
    handler.get_codes_by_message.assert_awaited_once_with("адрес")


def test_legacy_main_keeps_remaining_events_after_one_failure(modules: SimpleNamespace) -> None:
    if not hasattr(modules.main, "VKBot"):
        pytest.skip("Development runtime replaces the legacy loop; tested in durable queue tests")
    from vk_api.bot_longpoll import VkBotEventType

    events = [SimpleNamespace(type=VkBotEventType.MESSAGE_NEW, obj={"id": i}) for i in (1, 2)]

    def listen() -> Iterator[SimpleNamespace]:
        yield from events
        raise asyncio.CancelledError()

    bot = object.__new__(modules.main.VKBot)
    bot.long_poll = SimpleNamespace(listen=listen)
    bot.handler = SimpleNamespace(handle_message=AsyncMock(side_effect=[RuntimeError(), None]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run())
    assert bot.handler.handle_message.await_count == 2

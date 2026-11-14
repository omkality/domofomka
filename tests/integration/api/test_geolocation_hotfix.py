import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.code_service.services.geocoding import DadataClient, DadataUnavailable


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
    monkeypatch.setattr("src.code_service.services.geocoding.httpx.AsyncClient", MagicMock(return_value=client))
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
        (
            {"city": "Москва", "street": "Тестовая", "house": "4", "block_type": "к", "block": "1 стр 1"},
            "Москва Тестовая 4 к1с1",
        ),
    ],
)
def test_geocoding_requires_a_house_and_locality_and_preserves_blocks(builder, data, expected):
    builder.return_value = SimpleNamespace(
        status_code=200, json=MagicMock(return_value={"suggestions": [{"data": data}]})
    )
    assert asyncio.run(DadataClient("fake").get_address_by_geo(1, 2)) == expected

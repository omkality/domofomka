from unittest.mock import AsyncMock

import pytest
from litestar.testing import TestClient

from src.code_service.config import settings
from src.code_service.main import app
from src.code_service.services.v2 import search
from src.code_service.services.v2.postgres import PostgresCatalog


@pytest.mark.parametrize("path", ["/codes/geo", "/api/v1/codes/geo", "/api/v2/codes/geo"])
@pytest.mark.parametrize("observe", [False, True])
def test_geocoding_access_denial_is_typed_service_unavailable(monkeypatch, path, observe):
    from unittest.mock import AsyncMock

    from src.code_service.services.geocoding import DadataClient, DadataUnavailable
    from src.infrastructure.telemetry import settings as telemetry_settings

    monkeypatch.setattr(telemetry_settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)
    monkeypatch.setattr(DadataClient, "get_address_by_geo", AsyncMock(side_effect=DadataUnavailable(retryable=False)))
    with TestClient(app) as client:
        response = client.get(path, params={"lat": 1, "lon": 2, "observe": observe})
    assert response.status_code == 503
    assert response.json()["extra"]["geocoding_retryable"] is False


@pytest.mark.parametrize("status", ["found", "no_codes", "not_found"])
def test_geolocation_uses_the_same_v2_and_guarded_fallback_as_text(monkeypatch, status):
    from unittest.mock import AsyncMock

    from src.code_service.services.geocoding import DadataClient

    address = "Москва Тестовая 4к1с1"
    result = {"status": status, "engine": "v2", "address": "Москва, улица Тестовая, дом 4к1с1", "data": {}}
    if status == "found":
        result["data"] = {"1": [["EXPECTED", "test"]]}

    class Reader:
        async def resolve(self, message, candidates=None):
            assert message == address
            return result

    legacy = AsyncMock(return_value={"address": result["address"], "data": {"1": [["LEGACY", "test"]]}})
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(search, "get_catalog", AsyncMock(return_value=Reader()))
    monkeypatch.setattr(search, "get_data_from_db", legacy)
    monkeypatch.setattr(DadataClient, "get_address_by_geo", AsyncMock(return_value=address))
    with TestClient(app) as client:
        geo = client.get("/api/v2/codes/geo", params={"lat": 1, "lon": 2})
        text = client.get("/api/v2/codes/msg", params={"message": address})
    assert geo.status_code == text.status_code == 200
    assert geo.json() == text.json()
    assert legacy.await_count == (2 if status == "not_found" else 0)


def test_legacy_paths_remain_equivalent(monkeypatch):
    # Real routing and real v1 implementation; empty input avoids the production DB.
    with TestClient(app) as client:
        old = client.get("/codes/msg", params={"message": ""})
        versioned = client.get("/api/v1/codes/msg", params={"message": ""})
        assert old.status_code == versioned.status_code == 200
        assert old.json() == versioned.json() == {}


def test_v1_collection_endpoint_preserves_the_existing_response():
    with TestClient(app) as client:
        canonical = client.get("/api/v1/codes", params={"address": ""})
        compatible = client.get("/api/v1/codes/msg", params={"message": ""})
    assert canonical.status_code == compatible.status_code == 200
    assert canonical.json() == compatible.json() == {}


def test_v2_collection_endpoint_uses_the_same_application_scenario(monkeypatch):
    from unittest.mock import AsyncMock

    result = {"status": "no_codes", "engine": "v2", "address": "Known house", "data": {}}
    lookup = AsyncMock(return_value=result)
    monkeypatch.setattr(search, "lookup", lookup)
    with TestClient(app) as client:
        canonical = client.get("/api/v2/codes", params={"address": "Тестовая 1"})
        compatible = client.get("/api/v2/codes/msg", params={"message": "Тестовая 1"})
    assert canonical.status_code == compatible.status_code == 200
    assert canonical.json() == compatible.json() == result
    assert [call.args for call in lookup.await_args_list] == [("Тестовая 1",), ("Тестовая 1",)]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_nearby_resource_and_compatible_geo_route_share_error_contract(monkeypatch, version):
    from unittest.mock import AsyncMock

    from src.code_service.services.geocoding import DadataClient, DadataUnavailable

    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 0)
    monkeypatch.setattr(DadataClient, "get_address_by_geo", AsyncMock(side_effect=DadataUnavailable(retryable=False)))
    with TestClient(app) as client:
        canonical = client.get(f"/api/{version}/codes/nearby", params={"lat": 1, "lon": 2})
        compatible = client.get(f"/api/{version}/codes/geo", params={"lat": 1, "lon": 2})
    assert canonical.status_code == compatible.status_code == 503
    assert canonical.json() == compatible.json()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_code_resources_do_not_accept_writes(version):
    with TestClient(app) as client:
        assert client.post(f"/api/{version}/codes", json={"address": ""}).status_code == 405


def test_v2_unavailable_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(search, "get_catalog", AsyncMock(side_effect=ConnectionError("Catalog unavailable")))
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": ""})
        assert result.status_code == 200
        assert result.json()["engine"] == "v1"


def test_identity_without_codes_is_not_overridden(monkeypatch):
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(
        PostgresCatalog,
        "resolve",
        AsyncMock(return_value={"status": "no_codes", "address": "Known house", "data": {}, "engine": "v2"}),
    )

    async def should_not_run(message):
        raise AssertionError("Do not replace a known house with legacy guesses")

    monkeypatch.setattr(search, "get_data_from_db", should_not_run)
    with TestClient(app) as client:
        response = client.get("/api/v2/codes/msg", params={"message": "something"})
        assert response.json()["status"] == "no_codes"


def test_long_queries_are_rejected_without_truncating_to_another_house(monkeypatch):
    monkeypatch.setattr(settings, "search_max_query_length", 20)
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": "House 1 " + "x" * 50 + " House 99"})
        assert result.status_code == 200
        assert result.json()["status"] == "invalid_query"


def test_v2_does_not_return_legacy_house_number_reinterpretation(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "get_catalog", AsyncMock(side_effect=ConnectionError("Catalog unavailable")))
    monkeypatch.setattr(settings, "opensearch_url", None)

    async def legacy(message, *, accept_source):
        return {"address": "Москва, улица 1-я Парковая, дом 2", "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(search, "get_data_from_db", legacy)
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": "Москва Парковая 12"}).json()
        assert result["status"] == "not_found"
        assert result["fallback_rejected"] == "house_conflict"


@pytest.mark.parametrize(
    ("query", "address"),
    [
        ("Волжский бульвар квартал 114ак7", "Москва, квартал Волжский Бульвар 114А, дом к7"),
        ("Парнас заречная 33", "Санкт-Петербург, улица Заречная, дом ПАРНАС33"),
        ("Ельнинская 1 1", "Москва, улица Ельнинская, дом 11"),
    ],
)
def test_v2_keeps_opaque_legacy_numbering_compatibility(monkeypatch, tmp_path, query, address):
    monkeypatch.setattr(search, "get_catalog", AsyncMock(side_effect=ConnectionError("Catalog unavailable")))
    monkeypatch.setattr(settings, "opensearch_url", None)

    async def legacy(message, *, accept_source):
        return {"address": address, "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(search, "get_data_from_db", legacy)
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": query}).json()
        assert result["status"] == "found"
        assert result["address"] == address


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Набережная улица 19", "Набережная 19"])
async def test_empty_lexical_result_with_house_still_uses_compatible_v1(monkeypatch, query):
    class EmptyCatalog:
        async def resolve(self, message: str, candidates: list[dict] | None = None):
            return {"status": "incomplete_query", "data": {}, "engine": "v2"}

    async def legacy(message, *, accept_source):
        return {"address": "Долгопрудный, улица Набережная, дом 19", "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(search, "get_catalog", AsyncMock(return_value=EmptyCatalog()))
    monkeypatch.setattr(search, "get_data_from_db", legacy)
    result = await search.uncached_lookup(query)
    assert result["status"] == "found"
    assert result["engine"] == "v1"


@pytest.mark.asyncio
async def test_incomplete_address_does_not_reinterpret_an_area_number_as_a_house(monkeypatch):
    class EmptyCatalog:
        async def resolve(self, message: str, candidates: list[dict] | None = None):
            return {"status": "incomplete_query", "data": {}, "engine": "v2"}

    async def forbidden(message):
        raise AssertionError("A house number was not supplied")

    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(search, "get_catalog", AsyncMock(return_value=EmptyCatalog()))
    monkeypatch.setattr(search, "get_data_from_db", forbidden)
    assert (await search.uncached_lookup("квартал 114А"))["status"] == "incomplete_query"

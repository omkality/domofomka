import pytest
from litestar.testing import TestClient

from src.api import v2
from src.api.app import app
from src.api.config import settings


def test_legacy_paths_remain_equivalent(monkeypatch):
    # Real routing and real v1 implementation; empty input avoids the production DB.
    with TestClient(app) as client:
        old = client.get("/codes/msg", params={"message": ""})
        versioned = client.get("/api/v1/codes/msg", params={"message": ""})
        assert old.status_code == versioned.status_code == 200
        assert old.json() == versioned.json() == {}


def test_v2_unavailable_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(settings, "search_catalog_path", str(tmp_path / "missing.db"))
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": ""})
        assert result.status_code == 200
        assert result.json()["engine"] == "v1"


def test_identity_without_codes_is_not_overridden(monkeypatch):
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(
        v2.Catalog,
        "resolve",
        lambda self, message, candidates: {"status": "no_codes", "address": "Known house", "data": {}, "engine": "v2"},
    )

    async def should_not_run(message):
        raise AssertionError("Do not replace a known house with legacy guesses")

    monkeypatch.setattr(v2, "get_data_from_db", should_not_run)
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
    monkeypatch.setattr(settings, "search_catalog_path", str(tmp_path / "missing.db"))
    monkeypatch.setattr(settings, "opensearch_url", None)

    async def legacy(message, *, accept_source):
        return {"address": "Москва, улица 1-я Парковая, дом 2", "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(v2, "get_data_from_db", legacy)
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
    monkeypatch.setattr(settings, "search_catalog_path", str(tmp_path / "missing.db"))
    monkeypatch.setattr(settings, "opensearch_url", None)

    async def legacy(message, *, accept_source):
        return {"address": address, "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(v2, "get_data_from_db", legacy)
    with TestClient(app) as client:
        result = client.get("/api/v2/codes/msg", params={"message": query}).json()
        assert result["status"] == "found"
        assert result["address"] == address


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Набережная улица 19", "Набережная 19"])
async def test_empty_lexical_result_with_house_still_uses_compatible_v1(monkeypatch, query):
    class EmptyCatalog:
        def resolve(self, message: str, candidates: list[dict] | None = None):
            return {"status": "incomplete_query", "data": {}, "engine": "v2"}

    async def legacy(message, *, accept_source):
        return {"address": "Долгопрудный, улица Набережная, дом 19", "data": {"1": [["CODE", "test"]]}}

    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(v2, "get_catalog", EmptyCatalog)
    monkeypatch.setattr(v2, "get_data_from_db", legacy)
    result = await v2.uncached_lookup(query)
    assert result["status"] == "found"
    assert result["engine"] == "v1"


@pytest.mark.asyncio
async def test_incomplete_address_does_not_reinterpret_an_area_number_as_a_house(monkeypatch):
    class EmptyCatalog:
        def resolve(self, message: str, candidates: list[dict] | None = None):
            return {"status": "incomplete_query", "data": {}, "engine": "v2"}

    async def forbidden(message):
        raise AssertionError("A house number was not supplied")

    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(v2, "get_catalog", EmptyCatalog)
    monkeypatch.setattr(v2, "get_data_from_db", forbidden)
    assert (await v2.uncached_lookup("квартал 114А"))["status"] == "incomplete_query"

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

from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar.testing import TestClient

from src.code_service.app import app
from src.code_service.config import settings
from src.code_service.v2 import search
from src.code_service.v2.cache import LookupCache
from src.code_service.v2.engine import Catalog


def test_cache_expiry_eviction_copy_and_memory_budget():
    clock = [0.0]
    cache = LookupCache(lambda: clock[0])
    cache.configure(5, 2, 100)
    original = {"data": ["CODE"]}
    cache.put("first", original)
    original["data"].append("changed")
    cached = cache.get("first")
    assert cached == {"data": ["CODE"]}
    cached["data"].append("also changed")
    assert cache.get("first") == {"data": ["CODE"]}
    cache.put("second", {})
    cache.put("third", {})
    assert cache.get("first") is None
    assert len(cache.entries) <= 2
    assert cache.used_bytes <= 100
    cache.put("large", {"data": "x" * 1000})
    assert cache.get("large") is None
    assert cache.get("third") == {}
    clock[0] = 6
    assert cache.get("third") is None
    cache.configure(0, 2, 100)
    cache.put("disabled", {})
    assert cache.get("disabled") is None
    assert cache.used_bytes == 0


@pytest.fixture
def cached_lookup(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.db"
    catalog.write_bytes(b"catalog revision 1")
    source = tmp_path / "codes.db"
    source.touch()
    monkeypatch.setattr(settings, "search_catalog_path", str(catalog))
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "opensearch_url", None)
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 120)
    resolve = MagicMock(
        return_value={"status": "found", "engine": "v2", "address": "Дом", "data": {"1": [["CODE", "test"]]}}
    )
    monkeypatch.setattr(Catalog, "resolve", resolve)
    return catalog, resolve


def test_repeated_http_search_is_cached_and_catalog_replacement_invalidates_it(cached_lookup, monkeypatch):
    path, resolve = cached_lookup
    with TestClient(app) as client:
        first = client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"})
        second = client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"})
        assert first.json() == second.json()
        assert resolve.call_count == 1
        replacement = path.with_suffix(".new")
        replacement.write_bytes(b"catalog revision 2")
        replacement.replace(path)
        client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"})
        assert resolve.call_count == 2
        monkeypatch.setattr(settings, "opensearch_prefix_length", 0)
        client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"})
        assert resolve.call_count == 3


def test_unavailable_search_fallback_is_not_cached(cached_lookup, monkeypatch):
    _, resolve = cached_lookup
    success = resolve.return_value
    resolve.side_effect = [RuntimeError("offline"), success]
    monkeypatch.setattr(search, "get_data_from_db", AsyncMock(return_value={"data": {"1": []}}))
    with TestClient(app) as client:
        first = client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"}).json()
        assert first["fallback_reason"] == "unavailable"
        second = client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1"}).json()
        assert second["engine"] == "v2"
        assert resolve.call_count == 2


def test_geolocation_cache_uses_exact_coordinates(cached_lookup, monkeypatch):
    geocoder = MagicMock()
    geocoder.get_address_by_geo = AsyncMock(return_value="Москва Перерва 62к1")
    monkeypatch.setattr("src.code_service.geocoding.DadataClient.get_address_by_geo", geocoder.get_address_by_geo)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get("/api/v2/codes/geo", params={"lat": 55.1, "lon": 37.1}).status_code == 200
        assert geocoder.get_address_by_geo.await_count == 1
        client.get("/api/v2/codes/geo", params={"lat": 55.100001, "lon": 37.1})
        assert geocoder.get_address_by_geo.await_count == 2

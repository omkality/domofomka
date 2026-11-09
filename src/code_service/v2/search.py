"""Сценарии поиска: выбор каталога, кеш и проверенный резервный поиск.

HTTP-маршруты и коды ответа здесь не используются.
"""

import asyncio
import hashlib
import logging
from functools import partial
from pathlib import Path

from src.code_service.config import settings
from src.code_service.errors import BuildingNotFound, CatalogUnavailable
from src.code_service.geocoding import DadataClient
from src.code_service.v1.codes import get_data_from_db
from src.code_service.v2.cache import LookupCache
from src.code_service.v2.catalog import get_catalog
from src.code_service.v2.constraints import legacy_result_matches, legacy_source_matches
from src.code_service.v2.normalize import parse_query
from src.code_service.v2.opensearch import candidate_ids
from src.infrastructure.telemetry import note, search_result, span

logger = logging.getLogger(__name__)
result_cache = LookupCache()


def cache_key(request: tuple) -> tuple | None:
    result_cache.configure(
        settings.search_cache_ttl_seconds, settings.search_cache_max_entries, settings.search_cache_max_bytes
    )
    if settings.search_cache_ttl_seconds == 0:
        return None
    try:
        files = []
        filenames = [settings.db_name]
        if settings.search_backend == "sqlite":
            filenames.insert(0, settings.search_catalog_path)
        for filename in filenames:
            path = Path(filename).resolve()
            stat = path.stat()
            files.append((str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return None
    return (
        *files,
        settings.search_backend,
        settings.catalog_revision,
        hashlib.sha256(
            (settings.catalog_database_url.get_secret_value() if settings.catalog_database_url else "").encode()
        ).hexdigest(),
        settings.opensearch_url,
        settings.opensearch_index,
        settings.opensearch_fuzziness,
        settings.opensearch_prefix_length,
        settings.search_min_similarity,
        settings.search_ambiguity_margin,
        settings.search_candidate_limit,
        request,
    )


def remember(key: tuple | None, result: dict) -> None:
    if (
        key is not None
        and result.get("fallback_reason") != "unavailable"
        and result.get("status") in {"found", "no_codes", "ambiguous", "no_codes_ambiguous", "not_found"}
    ):
        result_cache.put(key, result)


async def lookup(message: str) -> dict:
    if len(message) > settings.search_max_query_length:
        return {"status": "invalid_query", "data": {}, "engine": "v2"}
    note(catalog_backend=settings.search_backend)
    key = cache_key(("text", message))
    cached = result_cache.get(key) if key is not None else None
    if cached is not None:
        note(cache_hit=True, cached_result_engine=cached.get("engine"), result=search_result(cached))
        return cached
    note(cache_hit=False)
    result = await uncached_lookup(message)
    remember(key, result)
    return result


async def uncached_lookup(message: str) -> dict:
    reason = None
    whole_location = False
    try:
        catalog = get_catalog()
        candidates = None
        if settings.opensearch_url:
            try:
                async with asyncio.timeout(settings.opensearch_timeout_seconds):
                    ids = await candidate_ids(message, settings)
                    candidates = (
                        await asyncio.to_thread(catalog.load_candidates, ids, parse_query(message).house)
                        if ids
                        else None
                    )

                candidates = candidates or None
            except Exception as exc:
                logger.warning("OpenSearch unavailable error=%s; using catalog index", type(exc).__name__)
        with span(
            "search.v2",
            kind="search",
            engine="v2",
            storage_backend=settings.search_backend,
            query=message[: settings.search_max_query_length],
        ) as detail:
            result = await asyncio.to_thread(catalog.resolve, message, candidates)
            detail["result"] = search_result(result)
        note(result=search_result(result))
        recoverable_incomplete = result["status"] == "incomplete_query" and bool(parse_query(message).house)
        if result["status"] != "not_found" and not recoverable_incomplete:
            return result
        whole_location = result.get("fallback_rejected") == "known_street_without_requested_house"
        reason = result["status"]
    except Exception as exc:
        logger.warning("V2 unavailable error=%s; using legacy search", type(exc).__name__)
        reason = "unavailable"
    result = await get_data_from_db(
        message, accept_source=partial(legacy_source_matches, message, whole_location=whole_location)
    )
    note(search_fallback_reason=reason)
    if result.get("data"):
        if not legacy_result_matches(message, result.get("address", ""), whole_location=whole_location):
            rejected = {
                "status": "not_found",
                "engine": "v2",
                "data": {},
                "fallback_reason": reason,
                "fallback_rejected": "house_conflict",
            }
            note(result=search_result(rejected))
            return rejected
    answer = dict(
        result,
        status="found" if result.get("data") else "not_found",
        engine="v1",
        fallback_reason=reason,
        data=result.get("data", {}),
    )
    note(result=search_result(answer))
    return answer


async def geo_lookup(lat: float, lon: float, dadata: DadataClient) -> dict:
    note(catalog_backend=settings.search_backend)
    key = cache_key(("geo", lat, lon))
    cached = result_cache.get(key) if key is not None else None
    if cached is not None:
        note(geo_cache_hit=True, cached_result_engine=cached.get("engine"), result=search_result(cached))
        return cached
    note(geo_cache_hit=False)
    with span("geo.resolve"):
        address = await dadata.get_address_by_geo(lat, lon)
    result = await lookup(address or "")
    remember(key, result)
    return result


async def read_building(building_id: str) -> dict:
    note(catalog_backend=settings.search_backend)

    def read() -> dict | None:
        catalog = get_catalog()
        row = catalog.building(building_id)
        return catalog.answer(row) if row else None

    try:
        with span("search.v2.building", kind="search", engine="v2", storage_backend=settings.search_backend) as detail:
            result = await asyncio.to_thread(read)
            detail["result"] = search_result(result) if result is not None else {"status": "not_found"}
    except Exception as exc:
        raise CatalogUnavailable("Catalog unavailable") from exc
    if result is None:
        raise BuildingNotFound(building_id)
    return result

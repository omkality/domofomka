import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from litestar import Router, get
from pyreqwest.client import ClientBuilder

from src.api.cache import LookupCache
from src.api.config import settings
from src.api.routes import get_dadata_client
from src.search.engine import Catalog, search_body
from src.services.codes import get_data_from_db

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
        for filename in (settings.search_catalog_path, settings.db_name):
            path = Path(filename).resolve()
            stat = path.stat()
            files.append((str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return None
    return (
        *files,
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
    key = cache_key(("text", message))
    cached = result_cache.get(key) if key is not None else None
    if cached is not None:
        return cached
    result = await uncached_lookup(message)
    remember(key, result)
    return result


async def uncached_lookup(message: str) -> dict:
    reason = None
    try:
        catalog = Catalog(
            settings.search_catalog_path, settings.search_min_similarity, settings.search_ambiguity_margin
        )
        candidates = None
        if settings.opensearch_url:
            try:
                async with (
                    ClientBuilder()
                    .no_proxy()
                    .timeout(timedelta(seconds=settings.opensearch_timeout_seconds))
                    .build() as client
                ):
                    response = (
                        await client.post(settings.opensearch_url + "/" + settings.opensearch_index + "/_search")
                        .body_json(
                            search_body(
                                message,
                                settings.search_candidate_limit,
                                fuzziness=settings.opensearch_fuzziness,
                                prefix_length=settings.opensearch_prefix_length,
                            )
                        )
                        .build()
                        .send()
                    )
                    if response.status != 200:
                        raise RuntimeError("Search index unavailable")
                    result = await response.json()
                    ids = [r["_id"] for r in result["hits"]["hits"]]

                    from src.search.normalize import parse_query

                    candidates = (
                        await asyncio.to_thread(catalog.load_candidates, ids, parse_query(message).house)
                        if ids
                        else None
                    )

                candidates = candidates or None
            except Exception as exc:
                logger.warning("OpenSearch unavailable error=%s; using catalog index", type(exc).__name__)
        result = await asyncio.to_thread(catalog.resolve, message, candidates)
        if result["status"] != "not_found":
            return result
        reason = "not_found"
    except Exception as exc:
        logger.warning("V2 unavailable error=%s; using legacy search", type(exc).__name__)
        reason = "unavailable"
    result = await get_data_from_db(message)
    if result.get("data"):
        from src.search.normalize import legacy_house_matches

        if not legacy_house_matches(message, result.get("address", "")):
            return {
                "status": "not_found",
                "engine": "v2",
                "data": {},
                "fallback_reason": reason,
                "fallback_rejected": "house_conflict",
            }
    return dict(
        result,
        status="found" if result.get("data") else "not_found",
        engine="v1",
        fallback_reason=reason,
        data=result.get("data", {}),
    )


@get("/msg")
async def message_codes(message: str) -> dict:
    if len(message) > settings.search_max_query_length:
        return {"status": "invalid_query", "data": {}, "engine": "v2"}
    return await lookup(message)


@get("/geo")
async def geo_codes(lat: float, lon: float) -> dict:
    from litestar.exceptions import ServiceUnavailableException

    from src.services.dadata_client import DadataUnavailable

    key = cache_key(("geo", lat, lon))
    cached = result_cache.get(key) if key is not None else None
    if cached is not None:
        return cached
    try:
        address = await (await get_dadata_client()).get_address_by_geo(lat, lon)
    except DadataUnavailable as exc:
        raise ServiceUnavailableException(detail="Geocoding unavailable; send text") from exc
    result = await lookup(address or "")
    remember(key, result)
    return result


v2_router = Router(path="/api/v2/codes", route_handlers=[message_codes, geo_codes])


@get("/api/v2/buildings/{building_id:str}")
async def building_codes(building_id: str) -> dict:
    from litestar.exceptions import NotFoundException

    catalog = Catalog(settings.search_catalog_path)

    def read() -> dict | None:
        row = catalog.building(building_id)
        return catalog.answer(row) if row else None

    result = await asyncio.to_thread(read)
    if result is None:
        raise NotFoundException()
    return result

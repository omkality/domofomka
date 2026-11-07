"""Диагностика передаётся боту; API не записывает её в БД очереди."""

from collections.abc import Awaitable, Callable

from litestar.exceptions import HTTPException
from litestar.response import Response

from src.telemetry import Capture, note, settings, span


async def observed(operation: Callable[[], Awaitable[dict]], requested: bool) -> dict | Response:
    if not requested or not settings.telemetry_enabled:
        return await operation()
    with Capture() as capture:
        note(api_revision=settings.app_revision, catalog_revision=settings.catalog_revision)
        from src.api.config import settings as api_settings

        note(
            search_config={
                name: getattr(api_settings, name)
                for name in (
                    "search_min_similarity",
                    "search_backend",
                    "search_ambiguity_margin",
                    "search_candidate_limit",
                    "search_cache_ttl_seconds",
                    "opensearch_fuzziness",
                    "opensearch_prefix_length",
                )
            }
        )
        try:
            with span("api.handle"):
                result = await operation()
        except HTTPException as exc:
            return Response(
                {"detail": exc.detail, "_diagnostics": capture.packet(), **({"extra": exc.extra} if exc.extra else {})},
                status_code=exc.status_code,
            )
        return dict(result, _diagnostics=capture.packet())

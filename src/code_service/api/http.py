"""Преобразование ошибок сценариев в HTTP-ответы на границе API."""

from collections.abc import Awaitable, Callable

from litestar.exceptions import NotFoundException, ServiceUnavailableException
from litestar.response import Response

from src.code_service.api.diagnostics import observed
from src.code_service.errors import BuildingNotFound, CatalogUnavailable
from src.code_service.services.geocoding import DadataUnavailable


async def respond(operation: Callable[[], Awaitable[dict]], observe: bool) -> dict | Response:
    """Преобразовать типизированные ошибки сценария в HTTP-ответ."""

    async def invoke() -> dict:
        """Вызвать сценарий и перевести его ошибку на границе HTTP."""
        try:
            return await operation()
        except DadataUnavailable as exc:
            raise ServiceUnavailableException(
                detail="Geocoding unavailable; send text", extra={"geocoding_retryable": exc.retryable}
            ) from exc
        except CatalogUnavailable as exc:
            raise ServiceUnavailableException(detail="Catalog unavailable") from exc
        except BuildingNotFound as exc:
            raise NotFoundException() from exc

    return await observed(invoke, observe)

"""Служебные HTTP-маршруты версии и документации API."""

from litestar import get
from litestar.response import Redirect

from src.infrastructure.version import get_app_info


@get("/", include_in_schema=False)
async def redirect_to_docs() -> Redirect:
    """Открыть документацию HTTP-контрактов с корня API."""
    return Redirect(path="/docs")


@get("/version")
async def version() -> dict[str, str]:
    """Вернуть метаданные приложения для диагностики и healthcheck."""
    return get_app_info()

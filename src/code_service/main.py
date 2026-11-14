"""ASGI-точка входа: сборка HTTP-приложения, его зависимостей и роутеров."""

from litestar import Litestar
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin

from src.code_service.api.dependencies import get_dadata_client
from src.code_service.api.routes import route_handlers
from src.code_service.config import settings
from src.code_service.services.v2.catalog import close_catalog
from src.infrastructure.version import get_app_info


def create_app() -> Litestar:
    """Собрать API без запуска сервера, SQL-миграций или фоновых задач."""
    return Litestar(
        dependencies={"dadata": Provide(get_dadata_client)},
        on_shutdown=[close_catalog],
        route_handlers=route_handlers(),
        openapi_config=OpenAPIConfig(
            title="Domofomka",
            version=get_app_info()["version"],
            render_plugins=[ScalarRenderPlugin()],
            path="/docs",
        ),
        debug=settings.api_debug,
    )


app = create_app()

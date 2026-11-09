import logging
from copy import deepcopy

from litestar import Litestar, Router, get
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.response import Redirect

from src.code_service.config import settings
from src.code_service.geocoding import DadataClient
from src.code_service.v1.routes import codes_router
from src.code_service.v2.catalog import close_catalog
from src.code_service.v2.routes import router as v2_router
from src.infrastructure.version import get_app_info

logger = logging.getLogger(__name__)


@get("/", include_in_schema=False)
async def redirect_to_docs() -> Redirect:
    return Redirect(path="/docs")


@get("/version")
async def version() -> dict[str, str]:
    return get_app_info()


async def get_dadata_client() -> DadataClient:
    return DadataClient(token=settings.dadata_token, proxy_url=settings.dadata_proxy_url)


app = Litestar(
    dependencies={"dadata": Provide(get_dadata_client)},
    on_shutdown=[close_catalog],
    route_handlers=[
        redirect_to_docs,
        version,
        codes_router,
        Router(path="/api/v1", route_handlers=[deepcopy(codes_router)]),
        v2_router,
    ],
    openapi_config=OpenAPIConfig(
        title="Domofomka",
        version=get_app_info()["version"],
        render_plugins=[ScalarRenderPlugin()],
        path="/docs",
    ),
    debug=settings.api_debug,
)

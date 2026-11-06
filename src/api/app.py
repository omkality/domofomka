import logging
from copy import deepcopy

from litestar import Litestar, Router, get
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.response import Redirect

from src.api.config import settings
from src.api.routes import codes_router
from src.api.v2 import building_codes, close_catalog, v2_router
from src.version import get_app_info

logger = logging.getLogger(__name__)


@get("/", include_in_schema=False)
async def redirect_to_docs() -> Redirect:
    return Redirect(path="/docs")


@get("/version")
async def version() -> dict[str, str]:
    return get_app_info()


app = Litestar(
    on_shutdown=[close_catalog],
    route_handlers=[
        redirect_to_docs,
        version,
        codes_router,
        Router(path="/api/v1", route_handlers=[deepcopy(codes_router)]),
        v2_router,
        building_codes,
    ],
    openapi_config=OpenAPIConfig(
        title="Domofomka",
        version=get_app_info()["version"],
        render_plugins=[ScalarRenderPlugin()],
        path="/docs",
    ),
    debug=settings.api_debug,
)

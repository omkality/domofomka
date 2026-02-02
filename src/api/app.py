import logging

from litestar import Litestar, get
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.response import Redirect

from src.api.routes import codes_router
from src.version import get_app_info

logger = logging.getLogger(__name__)


@get("/", include_in_schema=False)
async def redirect_to_docs() -> Redirect:
    return Redirect(path="/docs")


@get("/version")
async def version() -> dict[str, str]:
    return get_app_info()


app = Litestar(
    route_handlers=[redirect_to_docs, version, codes_router],
    openapi_config=OpenAPIConfig(
        title="Domofomka",
        version=get_app_info()["version"],
        render_plugins=[ScalarRenderPlugin()],
        path="/docs",
    ),
    debug=True,
)

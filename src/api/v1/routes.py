"""HTTP-маршруты API v1; сценарии находятся в application."""

from litestar import Router, get
from litestar.di import Provide

from src.api.responses import respond
from src.application import source_search
from src.services.dadata_client import DadataClient
from src.services.geocoding import get_dadata_client


@get("/")
async def search_codes(address: str, observe: bool = False) -> dict:
    return await respond(lambda: source_search.lookup(address), observe)


@get("/msg")
async def message_codes(message: str, observe: bool = False) -> dict:
    return await respond(lambda: source_search.lookup(message), observe)


@get(["/nearby", "/geo"])
async def nearby_codes(lat: float, lon: float, dadata: DadataClient, observe: bool = False) -> dict:
    return await respond(lambda: source_search.geo_lookup(lat, lon, dadata), observe)


codes_router = Router(
    path="/codes",
    route_handlers=[search_codes, message_codes, nearby_codes],
    dependencies={"dadata": Provide(get_dadata_client)},
)

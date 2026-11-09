"""HTTP-маршруты API v2; сценарии находятся в application."""

from litestar import Router, get
from litestar.di import Provide
from litestar.params import FromPath, FromQuery

from src.api.responses import respond
from src.application import search
from src.services.dadata_client import DadataClient
from src.services.geocoding import get_dadata_client


@get("/")
async def search_codes(address: FromQuery[str], observe: FromQuery[bool] = False) -> dict:
    return await respond(lambda: search.lookup(address), observe)


@get("/msg")
async def message_codes(message: FromQuery[str], observe: FromQuery[bool] = False) -> dict:
    return await respond(lambda: search.lookup(message), observe)


@get(["/nearby", "/geo"])
async def nearby_codes(
    lat: FromQuery[float], lon: FromQuery[float], dadata: DadataClient, observe: FromQuery[bool] = False
) -> dict:
    return await respond(lambda: search.geo_lookup(lat, lon, dadata), observe)


codes_router = Router(
    path="/codes",
    route_handlers=[search_codes, message_codes, nearby_codes],
    dependencies={"dadata": Provide(get_dadata_client)},
)


@get("/buildings/{building_id:str}")
async def building_codes(building_id: FromPath[str], observe: FromQuery[bool] = False) -> dict:
    return await respond(lambda: search.read_building(building_id), observe)


router = Router(path="/api/v2", route_handlers=[codes_router, building_codes])

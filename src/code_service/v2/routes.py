"""HTTP-маршруты API v2; сценарии находятся в search.py этой версии."""

from litestar import Router, get
from litestar.params import FromPath, FromQuery

from src.code_service.geocoding import DadataClient
from src.code_service.http import respond
from src.code_service.v2 import search


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
)


@get("/buildings/{building_id:str}")
async def building_codes(building_id: FromPath[str], observe: FromQuery[bool] = False) -> dict:
    return await respond(lambda: search.read_building(building_id), observe)


router = Router(path="/api/v2", route_handlers=[codes_router, building_codes])

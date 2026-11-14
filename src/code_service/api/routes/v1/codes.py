"""HTTP-маршруты API v1; сценарии находятся в search.py этой версии."""

from litestar import Router, get
from litestar.params import FromQuery

from src.code_service.api.http import respond
from src.code_service.services.geocoding import DadataClient
from src.code_service.services.v1 import search as source_search


@get("/")
async def search_codes(address: FromQuery[str], observe: FromQuery[bool] = False) -> dict:
    """Найти коды по адресу через сценарий соответствующей версии API."""
    return await respond(lambda: source_search.lookup(address), observe)


@get("/msg")
async def message_codes(message: FromQuery[str], observe: FromQuery[bool] = False) -> dict:
    """Обслужить совместимый HTTP-путь поиска по тексту сообщения."""
    return await respond(lambda: source_search.lookup(message), observe)


@get(["/nearby", "/geo"])
async def nearby_codes(
    lat: FromQuery[float], lon: FromQuery[float], dadata: DadataClient, observe: FromQuery[bool] = False
) -> dict:
    """Определить адрес координат и выполнить поиск кодов данной версии."""
    return await respond(lambda: source_search.geo_lookup(lat, lon, dadata), observe)


codes_router = Router(
    path="/codes",
    route_handlers=[search_codes, message_codes, nearby_codes],
)

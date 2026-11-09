"""Сценарии совместимого поиска v1 в исходной базе кодов."""

from src.code_service.geocoding import DadataClient
from src.code_service.v1.codes import get_data_from_db
from src.infrastructure.telemetry import span


async def lookup(message: str) -> dict:
    return await get_data_from_db(message)


async def geo_lookup(lat: float, lon: float, dadata: DadataClient) -> dict:
    with span("geo.resolve"):
        address = await dadata.get_address_by_geo(lat=lat, lon=lon)
    return await lookup(address or "")

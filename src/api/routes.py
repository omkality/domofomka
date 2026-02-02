import logging

from litestar import Router, get
from litestar.di import Provide

from src.api.config import settings
from src.services.codes import get_data_from_db
from src.services.dadata_client import DadataClient

logger = logging.getLogger(__name__)


async def get_dadata_client() -> DadataClient:
    return DadataClient(token=settings.dadata_token)


@get("/msg")
async def get_codes_by_message(message: str) -> dict[str, dict]:
    return await get_data_from_db(message)


@get("/geo")
async def get_codes_by_geo(
    lat: float,
    lon: float,
    dadata: DadataClient,
) -> dict[str, dict]:
    address = await dadata.get_address_by_geo(lat=lat, lon=lon)
    return await get_data_from_db(address)


codes_router = Router(
    path="/codes",
    route_handlers=[get_codes_by_message, get_codes_by_geo],
    dependencies={"dadata": Provide(get_dadata_client)},
)

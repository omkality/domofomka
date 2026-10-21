import logging

from litestar import Router, get
from litestar.di import Provide
from litestar.exceptions import ServiceUnavailableException

from src.api.config import settings
from src.services.codes import get_data_from_db
from src.services.dadata_client import DadataClient, DadataUnavailable

logger = logging.getLogger(__name__)


async def get_dadata_client() -> DadataClient:
    return DadataClient(
        token=settings.dadata_token,
        proxy_url=settings.dadata_proxy_url,
    )


@get("/msg")
async def get_codes_by_message(message: str) -> dict[str, dict]:
    return await get_data_from_db(message)


@get("/geo")
async def get_codes_by_geo(
    lat: float,
    lon: float,
    dadata: DadataClient,
) -> dict[str, dict]:
    try:
        address = await dadata.get_address_by_geo(lat=lat, lon=lon)
    except DadataUnavailable as exc:
        raise ServiceUnavailableException(
            detail="Geolocation is temporarily unavailable; send an address as text"
        ) from exc
    return await get_data_from_db(address)


codes_router = Router(
    path="/codes",
    route_handlers=[get_codes_by_message, get_codes_by_geo],
    dependencies={"dadata": Provide(get_dadata_client)},
)

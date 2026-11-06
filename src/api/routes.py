import logging

from litestar import Router, get
from litestar.di import Provide
from litestar.exceptions import ServiceUnavailableException

from src.api.config import settings
from src.api.telemetry import observed
from src.services.codes import get_data_from_db
from src.services.dadata_client import DadataClient, DadataUnavailable
from src.telemetry import span

logger = logging.getLogger(__name__)


async def get_dadata_client() -> DadataClient:
    return DadataClient(
        token=settings.dadata_token,
        proxy_url=settings.dadata_proxy_url,
    )


@get("/msg")
async def get_codes_by_message(message: str, observe: bool = False) -> dict[str, dict]:
    return await observed(lambda: get_data_from_db(message), observe)


@get("/geo")
async def get_codes_by_geo(
    lat: float,
    lon: float,
    dadata: DadataClient,
    observe: bool = False,
) -> dict[str, dict]:
    return await observed(lambda: legacy_geo(lat, lon, dadata), observe)


async def legacy_geo(lat: float, lon: float, dadata: DadataClient) -> dict:
    try:
        with span("geo.resolve"):
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

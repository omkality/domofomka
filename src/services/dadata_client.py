import asyncio
import logging
import time
import typing as tp
from datetime import timedelta

from pyreqwest.simple.request import pyreqwest_post

logger = logging.getLogger(__name__)


class DadataUnavailable(RuntimeError):
    """Temporary geocoding failure; callers can distinguish it from no results."""


class DadataClient:
    _unavailable_until = 0.0

    def __init__(self, token: str, timeout_seconds: float = 8):
        self.timeout_seconds = timeout_seconds
        self.token = token
        self.base_url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
        self.headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

    async def geolocate(self, lat: float, lon: float) -> dict[str, tp.Any] | None:
        url = f"{self.base_url}/geolocate/address"
        payload = {"lat": lat, "lon": lon}

        if time.monotonic() < type(self)._unavailable_until:
            raise DadataUnavailable("Geocoding is temporarily unavailable")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await (
                    pyreqwest_post(url)
                    .headers(self.headers)
                    .body_json(payload)
                    .timeout(timedelta(seconds=self.timeout_seconds))
                    .send()
                )
                if response.status != 200:
                    raise DadataUnavailable("Geocoding returned an unsuccessful response")
                return await response.json()
        except Exception as exc:
            # Separate client instances are created for requests. Keep the short
            # circuit breaker at process level to avoid repeated long waits.
            type(self)._unavailable_until = time.monotonic() + 30
            logger.warning("Geocoding unavailable error=%s", type(exc).__name__)
            raise DadataUnavailable("Geocoding is temporarily unavailable") from None

    async def get_address_by_geo(self, lat: float, lon: float) -> str | None:
        houses = await self.geolocate(lat=lat, lon=lon)

        if houses and "suggestions" in houses and houses["suggestions"]:
            house_data: dict[str, str] = houses["suggestions"][0]["data"]

            if house_data.get("street"):
                address = f"{house_data['city']} {house_data['street']} {house_data['house']}"

                if house_data.get("block"):
                    block_type = (house_data.get("block_type") or "").replace("стр", "с")
                    block = house_data["block"].replace(" стр ", "с")
                    address += f" {block_type}{block}"

                return address

        return None

    async def close(self) -> None:
        pass

import logging
import traceback
import typing as tp

from pyreqwest.simple.request import pyreqwest_post

logger = logging.getLogger(__name__)


class DadataClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
        self.headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

    async def geolocate(self, lat: float, lon: float) -> dict[str, tp.Any] | None:
        url = f"{self.base_url}/geolocate/address"
        payload = {"lat": lat, "lon": lon}

        try:
            response = (
                await pyreqwest_post(url)
                .headers(self.headers)
                .body_json(payload)
                .send()
            )

            if response.status == 200:
                return await response.json()
            else:
                logger.error(f"Dadata API error: {response.status}")
                return None

        except Exception:
            logger.error(f"Dadata request failed: {traceback.format_exc()}")
            raise

    async def get_address_by_geo(self, lat: float, lon: float) -> str | None:
        houses = await self.geolocate(lat=lat, lon=lon)

        if houses and "suggestions" in houses and houses["suggestions"]:
            house_data: dict[str, str] = houses["suggestions"][0]["data"]

            if house_data.get("street"):
                address = (
                    f"{house_data['city']} {house_data['street']} {house_data['house']}"
                )

                if house_data.get("block"):
                    block_type = house_data["block_type"].replace("стр", "с")
                    block = house_data["block"].replace(" стр ", "с")
                    address += f" {block_type}{block}"

                return address

        return None

    async def close(self) -> None:
        pass

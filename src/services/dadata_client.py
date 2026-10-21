import asyncio
import logging
import time
import typing as tp
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import urlsplit

from pyreqwest.client import ClientBuilder
from pyreqwest.exceptions import ConnectError, ConnectTimeoutError
from pyreqwest.proxy import ProxyBuilder
from pyreqwest.simple.request import pyreqwest_post

from src.network import network_settings
from src.services.outbound import proxy_health

logger = logging.getLogger(__name__)


class DadataUnavailable(RuntimeError):
    """Temporary geocoding failure; callers can distinguish it from no results."""


class DadataClient:
    _unavailable_until = 0.0

    def __init__(self, token: str, timeout_seconds: float | None = None, proxy_url: str | None = None):
        if proxy_url and (urlsplit(proxy_url).scheme not in {"http", "https"} or not urlsplit(proxy_url).hostname):
            raise ValueError("DADATA_PROXY_URL must be an HTTP or HTTPS proxy URL")
        self.proxy_url = proxy_url
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else network_settings.dadata_timeout_seconds
        )
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
            # One deadline covers proxy, direct fallback and response decoding.
            async with asyncio.timeout(self.timeout_seconds):
                use_proxy = bool(self.proxy_url and proxy_health.choose("dadata", self.proxy_url))
                if use_proxy:
                    try:
                        result = await self._request(url, payload, True)
                    except (ConnectError, ConnectTimeoutError):
                        proxy_health.failed("dadata", self.proxy_url)
                    else:
                        proxy_health.succeeded("dadata", self.proxy_url)
                        return result
                return await self._request(url, payload, False)
        except Exception as exc:
            # Separate client instances are created for requests. Keep the short
            # circuit breaker at process level to avoid repeated long waits.
            type(self)._unavailable_until = time.monotonic() + network_settings.dadata_failure_cooldown_seconds
            logger.warning("Geocoding unavailable error=%s", type(exc).__name__)
            raise DadataUnavailable("Geocoding is temporarily unavailable") from None

    async def _request(self, url: str, payload: dict, use_proxy: bool) -> dict:
        async with AsyncExitStack() as stack:
            request = pyreqwest_post
            if self.proxy_url:
                builder = ClientBuilder()
                builder = builder.proxy(ProxyBuilder.https(self.proxy_url)) if use_proxy else builder.no_proxy()
                # Bound failed proxy connection setup, preserving the total deadline.
                client = await stack.enter_async_context(
                    builder.connect_timeout(timedelta(seconds=network_settings.dadata_connect_timeout_seconds)).build()
                )
                request = client.post
            pending = (
                request(url).headers(self.headers).body_json(payload).timeout(timedelta(seconds=self.timeout_seconds))
            )
            if self.proxy_url:
                pending = pending.build()
            response = await pending.send()
            if response.status != 200:
                raise DadataUnavailable("Geocoding returned an unsuccessful response")
            return await response.json()

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

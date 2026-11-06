import asyncio
import logging
import time
import typing as tp
from urllib.parse import urlsplit

import httpx

from src.network import network_settings
from src.services.outbound import proxy_health
from src.telemetry import amend, note, span

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
            note(geocoding_skipped="service_cooldown")
            raise DadataUnavailable("Geocoding is temporarily unavailable")
        try:
            # One deadline covers proxy, direct fallback and response decoding.
            async with asyncio.timeout(self.timeout_seconds):
                use_proxy, reason = (
                    proxy_health.decision("dadata", self.proxy_url) if self.proxy_url else (False, "not_configured")
                )
                if use_proxy:
                    try:
                        with span(
                            "network.dadata",
                            kind="network",
                            target="dadata",
                            operation="geolocate",
                            route="ru_proxy",
                            route_reason=reason,
                            attempt=1,
                            timeout_ms=self.timeout_seconds * 1000,
                        ):
                            result = await self._request(url, payload, True)
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
                        proxy_health.failed("dadata", self.proxy_url)
                    else:
                        proxy_health.succeeded("dadata", self.proxy_url)
                        return result
                with span(
                    "network.dadata",
                    kind="network",
                    target="dadata",
                    operation="geolocate",
                    route="direct",
                    route_reason="proxy_connect_failed" if use_proxy else reason,
                    attempt=2 if use_proxy else 1,
                    timeout_ms=self.timeout_seconds * 1000,
                ):
                    return await self._request(url, payload, False)
        except Exception as exc:
            # Separate client instances are created for requests. Keep the short
            # circuit breaker at process level to avoid repeated long waits.
            type(self)._unavailable_until = time.monotonic() + network_settings.dadata_failure_cooldown_seconds
            logger.warning("Geocoding unavailable error=%s", type(exc).__name__)
            raise DadataUnavailable("Geocoding is temporarily unavailable") from None

    async def _request(self, url: str, payload: dict, use_proxy: bool) -> dict:
        async with httpx.AsyncClient(
            proxy=self.proxy_url if use_proxy else None,
            trust_env=False,
            timeout=httpx.Timeout(
                self.timeout_seconds,
                connect=min(network_settings.dadata_connect_timeout_seconds, self.timeout_seconds),
            ),
            follow_redirects=True,
        ) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            amend(http_status=response.status_code)
            if response.status_code != 200:
                raise DadataUnavailable("Geocoding returned an unsuccessful response")
            return response.json()

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

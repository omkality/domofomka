"""Определение адреса по координатам с ограничением времени и доступа DaData."""

import asyncio
import logging
import time
import typing as tp
from urllib.parse import urlsplit

import httpx

from src.infrastructure.network import network_settings, tls_context
from src.infrastructure.proxy import proxy_health
from src.infrastructure.telemetry import amend, note, span

logger = logging.getLogger(__name__)


class DadataUnavailable(RuntimeError):
    """Temporary geocoding failure; callers can distinguish it from no results."""

    def __init__(self, message: str = "Geocoding is temporarily unavailable", *, retryable: bool = True):
        """Сохранить причину отказа геокодера и допустимость повтора."""
        super().__init__(message)
        self.retryable = retryable


class DadataClient:
    """Геокодер координат с общим дедлайном и защитой от повторов при отказе доступа."""

    _unavailable_until = 0.0
    _unavailable_retryable = True

    def __init__(self, token: str, timeout_seconds: float | None = None, proxy_url: str | None = None):
        """Проверить URL прокси и настроить токен и общий бюджет DaData."""
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
        """Запросить адрес по координатам через настроенный транспорт."""
        url = f"{self.base_url}/geolocate/address"
        payload = {"lat": lat, "lon": lon}

        if time.monotonic() < type(self)._unavailable_until:
            note(geocoding_skipped="service_cooldown")
            raise DadataUnavailable(retryable=type(self)._unavailable_retryable)
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
            retryable = not isinstance(exc, DadataUnavailable) or exc.retryable
            cooldown = (
                network_settings.dadata_failure_cooldown_seconds
                if retryable
                else network_settings.dadata_access_cooldown_seconds
            )
            type(self)._unavailable_until = time.monotonic() + cooldown
            type(self)._unavailable_retryable = retryable
            logger.warning("Geocoding unavailable error=%s", type(exc).__name__)
            raise DadataUnavailable(retryable=retryable) from None

    async def _request(self, url: str, payload: dict, use_proxy: bool) -> dict:
        """Выполнить ограниченный запрос к геокодеру по выбранному маршруту."""
        async with httpx.AsyncClient(
            verify=tls_context(),
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
            if response.status_code in {401, 403}:
                # 403 also covers invalid credentials/unconfirmed mail: do not
                # claim the daily quota is certainly exhausted or change routes.
                raise DadataUnavailable("Geocoding access unavailable", retryable=False)
            if response.status_code != 200:
                raise DadataUnavailable("Geocoding returned an unsuccessful response")
            return response.json()

    async def get_address_by_geo(self, lat: float, lon: float) -> str | None:
        """Определить адрес либо вернуть типизированный временный отказ геокодера."""
        houses = await self.geolocate(lat=lat, lon=lon)

        if houses and "suggestions" in houses and houses["suggestions"]:
            house_data: dict[str, str] = houses["suggestions"][0]["data"]

            locality = house_data.get("settlement") or house_data.get("city")
            if locality and house_data.get("street") and house_data.get("house"):
                address = f"{locality} {house_data['street']} {house_data['house']}"

                if house_data.get("block"):
                    if not house_data.get("block_type"):
                        return None
                    block_type = house_data["block_type"].replace("стр", "с")
                    block = house_data["block"].replace(" стр ", "с")
                    address += f" {block_type}{block}"

                return address

        return None

    async def close(self) -> None:
        """Закрыть открытые соединения и ресурсы компонента."""
        pass

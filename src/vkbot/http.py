"""Ограниченные HTTP-попытки VK, общий RateGate и выбор сетевого маршрута."""

import asyncio
import os
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from requests import Response, Session
from requests.exceptions import ConnectionError, ConnectTimeout, ProxyError, Timeout

from src.infrastructure.network import network_settings
from src.infrastructure.proxy import proxy_health
from src.infrastructure.telemetry import active, measured, span


async def call_vk[T](operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Выполнить sync SDK и дождаться завершения запроса даже при отмене worker."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class TimeoutSession(Session):
    """Bound VK API requests while preserving explicit Long Poll timeouts."""

    def __init__(self, vk_proxy_url: str | None = None, *, longpoll: bool = False) -> None:
        """Настроить ограниченные HTTP-попытки и отдельный режим Long Poll."""
        super().__init__()
        self.longpoll = longpoll
        self.vk_proxy_url = vk_proxy_url if vk_proxy_url is not None else os.getenv("VK_PROXY_URL") or None
        if self.vk_proxy_url:
            proxy = urlsplit(self.vk_proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise ValueError("VK_PROXY_URL must be an HTTP or HTTPS proxy URL")

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Выполнить ограниченный HTTP-запрос с выбранным сетевым бюджетом."""
        if self.longpoll:
            kwargs["timeout"] = (
                network_settings.vk_connect_timeout_seconds,
                network_settings.vk_longpoll_wait_seconds + network_settings.vk_longpoll_timeout_margin_seconds,
            )
        elif kwargs.get("timeout") is None:
            kwargs["timeout"] = (network_settings.vk_connect_timeout_seconds, network_settings.vk_read_timeout_seconds)

        endpoint = urlsplit(url)
        host = endpoint.hostname or ""
        managed_proxy = (
            self.vk_proxy_url
            if ("proxies" not in kwargs and (host in {"vk.com", "vk.ru"} or host.endswith((".vk.com", ".vk.ru"))))
            else None
        )
        api_method = endpoint.path.removeprefix("/method/")
        safe_methods = {
            "groups.isMember",
            "groups.getLongPollServer",
            "messages.getById",
            "messages.getByConversationMessageId",
            "messages.edit",
        }
        data = kwargs.get("data")
        deduplicated_send = api_method == "messages.send" and isinstance(data, dict) and bool(data.get("random_id"))
        retryable = endpoint.hostname == "api.vk.com" and (api_method in safe_methods or deduplicated_send)
        # Unknown mutating methods are never automatically replayed.
        safe_replay = retryable or method.upper() in {"GET", "HEAD"}
        attempts = (
            network_settings.vk_retry_attempts
            if retryable
            else (network_settings.vk_fallback_attempts if managed_proxy and safe_replay else 1)
        )
        proxy_failed = False
        for attempt in range(attempts):
            routed = dict(kwargs)
            use_proxy, reason = proxy_health.decision("vk", managed_proxy) if managed_proxy else (False, "not_managed")
            if proxy_failed and not use_proxy:
                reason = "proxy_connect_failed"
            if managed_proxy:
                # Explicit empty proxy values also disable environment proxies.
                routed["proxies"] = {
                    "http": managed_proxy if use_proxy else "",
                    "https": managed_proxy if use_proxy else "",
                }
            try:
                with span(
                    "network.vk",
                    kind="network",
                    target="vk",
                    operation="longpoll" if self.longpoll else api_method,
                    route="ru_proxy" if use_proxy else ("external" if "proxies" in kwargs else "direct"),
                    route_reason=reason,
                    attempt=attempt + 1,
                    timeout_seconds=kwargs["timeout"],
                ) as detail:
                    response = super().request(method, url, **routed)
                    detail["http_status"] = response.status_code
                    if active() and endpoint.hostname == "api.vk.com":
                        try:
                            error = response.json().get("error", {})
                            if error.get("error_code") is not None:
                                detail["provider_error_code"] = str(error["error_code"])
                        except (ValueError, AttributeError, TypeError):
                            pass
                if use_proxy:
                    proxy_health.succeeded("vk", managed_proxy)
                return response
            except (ConnectionError, Timeout) as exc:
                # Only a failed proxy connection changes routing. A response
                # timeout may follow a successful mutation; keep existing safe retries.
                if use_proxy and isinstance(exc, (ProxyError, ConnectTimeout)):
                    proxy_failed = True
                    proxy_health.failed("vk", managed_proxy)
                elif use_proxy:
                    proxy_health.succeeded("vk", managed_proxy)
                if attempt == attempts - 1:
                    raise
                with span("network.retry_wait", target="vk"):
                    time.sleep((attempt + 1) * network_settings.vk_retry_delay_seconds)
        raise RuntimeError("Request attempts exhausted")


class RateGate:
    """Общий ограничитель частоты фактических HTTP-попыток VK."""

    def __init__(self, interval: float) -> None:
        """Создать общий потокобезопасный бюджет HTTP-запросов."""
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    @measured("vk.rate_limit_wait")
    def wait(self) -> None:
        """Дождаться разрешённого момента следующего HTTP-запроса."""
        with self.lock:
            remaining = self.interval - (time.monotonic() - self.last)
            if remaining > 0:
                time.sleep(remaining)
            self.last = time.monotonic()


class RateSession(TimeoutSession):
    """Сессия VK с общим ограничителем частоты запросов."""

    def __init__(self, gate: RateGate, *, longpoll: bool = False) -> None:
        """Подключить общий бюджет к ограниченной сессии VK."""
        super().__init__(longpoll=longpoll)
        self.gate = gate

    def send(self, request: Any, **kwargs: Any) -> Any:
        # Gate actual HTTP attempts, including retries inside TimeoutSession.
        """Выполнить HTTP-попытку после проверки общего ограничителя."""
        self.gate.wait()
        return super().send(request, **kwargs)

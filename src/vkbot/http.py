import os
import time
from typing import Any
from urllib.parse import urlsplit

from requests import Response, Session
from requests.exceptions import ConnectionError, ConnectTimeout, ProxyError, Timeout

from src.network import network_settings
from src.services.outbound import proxy_health


class TimeoutSession(Session):
    """Bound VK API requests while preserving explicit Long Poll timeouts."""

    def __init__(self, vk_proxy_url: str | None = None, *, longpoll: bool = False):
        super().__init__()
        self.longpoll = longpoll
        self.vk_proxy_url = vk_proxy_url if vk_proxy_url is not None else os.getenv("VK_PROXY_URL") or None
        if self.vk_proxy_url:
            proxy = urlsplit(self.vk_proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise ValueError("VK_PROXY_URL must be an HTTP or HTTPS proxy URL")

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
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
        for attempt in range(attempts):
            routed = dict(kwargs)
            use_proxy = bool(managed_proxy and proxy_health.choose("vk", managed_proxy))
            if managed_proxy:
                # Explicit empty proxy values also disable environment proxies.
                routed["proxies"] = {
                    "http": managed_proxy if use_proxy else "",
                    "https": managed_proxy if use_proxy else "",
                }
            try:
                response = super().request(method, url, **routed)
                if use_proxy:
                    proxy_health.succeeded("vk", managed_proxy)
                return response
            except (ConnectionError, Timeout) as exc:
                # Only a failed proxy connection changes routing. A response
                # timeout may follow a successful mutation; keep existing safe retries.
                if use_proxy and isinstance(exc, (ProxyError, ConnectTimeout)):
                    proxy_health.failed("vk", managed_proxy)
                elif use_proxy:
                    proxy_health.succeeded("vk", managed_proxy)
                if attempt == attempts - 1:
                    raise
                time.sleep((attempt + 1) * network_settings.vk_retry_delay_seconds)
        raise RuntimeError("Request attempts exhausted")

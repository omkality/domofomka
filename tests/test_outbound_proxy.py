import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyreqwest.exceptions import ConnectError
from pyreqwest.request import RequestBuilder
from requests import Response, Session
from requests.exceptions import ProxyError, ReadTimeout

from src.services.dadata_client import DadataClient, DadataUnavailable
from src.services.outbound import ProxyHealth, proxy_health
from src.vkbot.http import TimeoutSession

PROXY = "http://private-proxy:18888"


@pytest.fixture(autouse=True)
def reset_routes() -> Iterator[None]:
    proxy_health.routes.clear()
    DadataClient._unavailable_until = 0
    yield
    proxy_health.routes.clear()
    DadataClient._unavailable_until = 0


@pytest.mark.parametrize(
    "url", ["https://api.vk.com/method/test", "https://lp.vk.com/longpoll", "https://api.vk.ru/method/test"]
)
def test_vk_proxy_covers_api_and_longpoll(url: str) -> None:
    with patch.object(Session, "request") as request:
        TimeoutSession(PROXY).get(url, timeout=35)
    assert request.call_args.kwargs["proxies"] == {"http": PROXY, "https": PROXY}
    assert request.call_args.kwargs["timeout"] == 35
    assert "verify" not in request.call_args.kwargs


@pytest.mark.parametrize(
    "url", ["http://api:8000/codes/msg", "https://suggestions.dadata.ru/path", "https://vk.com.attacker.example/path"]
)
def test_vk_proxy_never_captures_internal_or_other_hosts(url: str) -> None:
    with patch.object(Session, "request") as request:
        TimeoutSession(PROXY).get(url)
    assert "proxies" not in request.call_args.kwargs


def test_vk_proxy_is_opt_in_and_can_be_set_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VK_PROXY_URL", raising=False)
    with patch.object(Session, "request") as request:
        TimeoutSession().get("https://api.vk.com/method/test")
    assert "proxies" not in request.call_args.kwargs
    monkeypatch.setenv("VK_PROXY_URL", PROXY)
    with patch.object(Session, "request") as request:
        TimeoutSession().get("https://api.vk.com/method/test")
    assert request.call_args.kwargs["proxies"]["https"] == PROXY


def test_explicit_request_proxy_has_priority() -> None:
    explicit = {"https": "http://another-private-proxy:8888"}
    with patch.object(Session, "request") as request:
        TimeoutSession(PROXY).get("https://api.vk.com/method/test", proxies=explicit)
    assert request.call_args.kwargs["proxies"] == explicit


@pytest.mark.parametrize("proxy", ["socks5://unsupported:1080", "invalid", "http://"])
def test_invalid_proxy_fails_without_exposing_credentials(proxy: str) -> None:
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        TimeoutSession(proxy)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        DadataClient("fake-token", proxy_url=proxy)


@pytest.mark.parametrize("fail", [False, True])
def test_dadata_proxy_uses_dedicated_client_and_closes_it(fail: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    request = MagicMock(spec=RequestBuilder)
    request.headers.return_value = request
    request.body_json.return_value = request
    request.timeout.return_value = request
    consumed = MagicMock()
    consumed.send = AsyncMock(
        return_value=SimpleNamespace(status=200, json=AsyncMock(return_value={"suggestions": []}))
    )
    request.build.return_value = consumed
    if fail:
        consumed.send.side_effect = TimeoutError()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post.return_value = request
    builder = MagicMock()
    builder.proxy.return_value = builder
    builder.connect_timeout.return_value = builder
    builder.build.return_value = client
    monkeypatch.setattr("src.services.dadata_client.ClientBuilder", MagicMock(return_value=builder))
    proxy_builder = MagicMock()
    monkeypatch.setattr("src.services.dadata_client.ProxyBuilder", proxy_builder)
    direct = MagicMock()
    monkeypatch.setattr("src.services.dadata_client.pyreqwest_post", direct)
    DadataClient._unavailable_until = 0
    try:
        if fail:
            with pytest.raises(DadataUnavailable):
                asyncio.run(DadataClient("fake-token", proxy_url=PROXY).geolocate(1, 2))
        else:
            assert asyncio.run(DadataClient("fake-token", proxy_url=PROXY).geolocate(1, 2)) == {"suggestions": []}
        proxy_builder.https.assert_called_once_with(PROXY)
        client.__aexit__.assert_awaited_once()
        direct.assert_not_called()
        assert request.timeout.call_args.args[0].total_seconds() == 8
    finally:
        DadataClient._unavailable_until = 0


def test_proxy_backoff_and_single_recovery_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("src.services.outbound.time.monotonic", lambda: clock[0])
    health = ProxyHealth()
    assert health.choose("vk", PROXY)
    health.failed("vk", PROXY)
    assert not health.choose("vk", PROXY)
    assert health.choose("dadata", PROXY)
    clock[0] = 30
    assert health.choose("vk", PROXY)
    assert not health.choose("vk", PROXY)
    health.failed("vk", PROXY)
    clock[0] = 89
    assert not health.choose("vk", PROXY)
    clock[0] = 90
    assert health.choose("vk", PROXY)
    health.succeeded("vk", PROXY)
    assert health.choose("vk", PROXY)


def test_vk_proxy_failure_replays_same_random_id_direct_and_stays_direct() -> None:
    response = Response()
    data = {"peer_id": 1, "message": "test", "random_id": 42}
    with (
        patch.object(Session, "request", side_effect=[ProxyError(), response, response]) as request,
        patch("src.vkbot.http.time.sleep"),
    ):
        session = TimeoutSession(PROXY)
        session.post("https://api.vk.com/method/messages.send", data=data)
        session.get("https://lp.vk.com/test")
    calls = request.call_args_list
    assert calls[0].kwargs["proxies"]["https"] == PROXY
    assert calls[1].kwargs["proxies"] == {"http": "", "https": ""}
    assert calls[2].kwargs["proxies"] == {"http": "", "https": ""}
    assert calls[0].kwargs["data"] == calls[1].kwargs["data"] == data


def test_vk_unsafe_mutation_does_not_replay_after_proxy_failure() -> None:
    with patch.object(Session, "request", side_effect=ProxyError()) as request, pytest.raises(ProxyError):
        TimeoutSession(PROXY).post("https://api.vk.com/method/messages.send", data={"random_id": 0})
    assert request.call_count == 1
    assert not proxy_health.choose("vk", PROXY)


def test_vk_response_timeout_does_not_switch_route() -> None:
    with (
        patch.object(Session, "request", side_effect=[ReadTimeout(), Response()]) as request,
        patch("src.vkbot.http.time.sleep"),
    ):
        TimeoutSession(PROXY).post("https://api.vk.com/method/messages.edit", data={"message": "same result"})
    assert all(c.kwargs["proxies"]["https"] == PROXY for c in request.call_args_list)


def test_vk_http_error_does_not_switch_route() -> None:
    response = Response()
    response.status_code = 429
    with patch.object(Session, "request", return_value=response) as request:
        assert TimeoutSession(PROXY).get("https://api.vk.com/method/test").status_code == 429
    assert request.call_count == 1
    assert proxy_health.choose("vk", PROXY)


def test_dadata_connection_failure_falls_back_and_new_instance_skips_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(side_effect=[ConnectError("connect failed", {}), {"suggestions": []}, {"suggestions": []}])
    monkeypatch.setattr(DadataClient, "_request", request)

    async def run() -> None:
        assert await DadataClient("fake", proxy_url=PROXY).geolocate(1, 2) == {"suggestions": []}
        assert await DadataClient("fake", proxy_url=PROXY).geolocate(1, 2) == {"suggestions": []}

    asyncio.run(run())
    assert [c.args[-1] for c in request.call_args_list] == [True, False, False]


def test_dadata_does_not_retry_http_errors_or_ambiguous_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(side_effect=DadataUnavailable("HTTP error"))
    monkeypatch.setattr(DadataClient, "_request", request)
    with pytest.raises(DadataUnavailable):
        asyncio.run(DadataClient("fake", proxy_url=PROXY).geolocate(1, 2))
    request.assert_awaited_once()


def test_dadata_fallback_shares_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def request(*args: Any) -> None:
        await asyncio.sleep(0.04)
        if args[-1]:
            raise ConnectError("connect failed", {})
        await asyncio.sleep(1)

    monkeypatch.setattr(DadataClient, "_request", request)

    async def run() -> None:
        with pytest.raises(DadataUnavailable):
            await asyncio.wait_for(DadataClient("fake", timeout_seconds=0.06, proxy_url=PROXY).geolocate(1, 2), 0.3)

    asyncio.run(run())

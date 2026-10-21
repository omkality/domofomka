from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from requests import Response, Session
from requests.exceptions import ReadTimeout

from src.network import NetworkSettings
from src.services.outbound import ProxyHealth
from src.vkbot.http import TimeoutSession


def test_network_values_are_loaded_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "VK_CONNECT_TIMEOUT_SECONDS",
        "VK_RETRY_ATTEMPTS",
        "DADATA_TIMEOUT_SECONDS",
        "PROXY_RETRY_INITIAL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "VK_CONNECT_TIMEOUT_SECONDS=1.5\nVK_RETRY_ATTEMPTS=2\nDADATA_TIMEOUT_SECONDS=9\nPROXY_RETRY_INITIAL_SECONDS=7\n",
        encoding="utf8",
    )
    config = NetworkSettings(_env_file=env)
    assert config.vk_connect_timeout_seconds == 1.5
    assert config.vk_retry_attempts == 2
    assert config.dadata_timeout_seconds == 9
    assert config.proxy_retry_initial_seconds == 7


@pytest.mark.parametrize(
    "values",
    [
        {"vk_connect_timeout_seconds": 0},
        {"vk_retry_attempts": 0},
        {"vk_retry_attempts": 1000},
        {"vk_longpoll_wait_seconds": 91},
        {"proxy_retry_initial_seconds": 50, "proxy_retry_max_seconds": 10},
        {"dadata_connect_timeout_seconds": 10, "dadata_timeout_seconds": 8},
    ],
)
def test_invalid_tuning_is_rejected(values: dict) -> None:
    with pytest.raises(ValidationError):
        NetworkSettings(_env_file=None, **values)


def test_vk_applies_configured_timeout_retries_and_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    config = NetworkSettings(
        _env_file=None,
        vk_connect_timeout_seconds=1,
        vk_read_timeout_seconds=2,
        vk_retry_attempts=2,
        vk_retry_delay_seconds=0.1,
    )
    monkeypatch.setattr("src.vkbot.http.network_settings", config)
    with (
        patch.object(Session, "request", side_effect=[ReadTimeout(), Response()]) as request,
        patch("src.vkbot.http.time.sleep") as sleep,
    ):
        TimeoutSession().post("https://api.vk.com/method/messages.send", data={"random_id": 123})
    assert request.call_count == 2
    assert all(c.kwargs["timeout"] == (1, 2) for c in request.call_args_list)
    sleep.assert_called_once_with(0.1)


def test_longpoll_uses_configured_wait_and_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    config = NetworkSettings(
        _env_file=None, vk_connect_timeout_seconds=2, vk_longpoll_wait_seconds=10, vk_longpoll_timeout_margin_seconds=4
    )
    monkeypatch.setattr("src.vkbot.http.network_settings", config)
    with patch.object(Session, "request") as request:
        TimeoutSession(longpoll=True).get("https://lp.vk.com/test", timeout=35)
    assert request.call_args.kwargs["timeout"] == (2, 14)


def test_proxy_health_obeys_configured_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    config = NetworkSettings(_env_file=None, proxy_retry_initial_seconds=2, proxy_retry_max_seconds=3)
    monkeypatch.setattr("src.services.outbound.network_settings", config)
    clock = [0.0]
    monkeypatch.setattr("src.services.outbound.time.monotonic", lambda: clock[0])
    health = ProxyHealth()
    health.failed("vk", "proxy")
    assert not health.choose("vk", "proxy")
    clock[0] = 2
    assert health.choose("vk", "proxy")
    health.failed("vk", "proxy")
    assert health.routes["vk", "proxy"]["delay"] == 3

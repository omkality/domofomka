"""Validated runtime tuning. Override defaults through .env or process environment."""

import logging
from functools import lru_cache
from ssl import SSLContext

import httpx
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NetworkSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", allow_inf_nan=False, hide_input_in_errors=True
    )

    dadata_timeout_seconds: float = Field(default=8, gt=0)
    dadata_connect_timeout_seconds: float = Field(default=2, gt=0)
    dadata_failure_cooldown_seconds: float = Field(default=30, gt=0)
    dadata_access_cooldown_seconds: float = Field(default=3600, gt=0)
    proxy_retry_initial_seconds: float = Field(default=30, gt=0)
    proxy_retry_max_seconds: float = Field(default=300, gt=0)
    proxy_retry_multiplier: float = Field(default=2, ge=1)
    proxy_probe_lease_seconds: float = Field(default=60, gt=0)
    vk_connect_timeout_seconds: float = Field(default=5, gt=0)
    vk_read_timeout_seconds: float = Field(default=20, gt=0)
    vk_retry_attempts: int = Field(default=3, ge=1, le=10)
    vk_fallback_attempts: int = Field(default=2, ge=1, le=10)
    vk_retry_delay_seconds: float = Field(default=1, gt=0)
    vk_longpoll_wait_seconds: int = Field(default=25, gt=0, le=90)
    vk_longpoll_timeout_margin_seconds: float = Field(default=10, gt=0)
    bot_restart_delay_seconds: float = Field(default=3, gt=0)
    internal_api_connect_timeout_seconds: float = Field(default=5, gt=0)
    internal_api_timeout_seconds: float = Field(default=30, gt=0)
    internal_api_geo_timeout_seconds: float = Field(default=30, gt=0)

    queue_db_connect_timeout_seconds: int = Field(default=5, gt=0)
    queue_db_statement_timeout_ms: int = Field(default=15000, gt=0)
    queue_db_lock_timeout_ms: int = Field(default=5000, gt=0)

    @model_validator(mode="after")
    def consistent_limits(self) -> "NetworkSettings":
        if self.proxy_retry_max_seconds < self.proxy_retry_initial_seconds:
            raise ValueError("Proxy retry maximum must cover the initial delay")
        if self.dadata_connect_timeout_seconds > self.dadata_timeout_seconds:
            raise ValueError("DaData connect timeout must fit within the total timeout")
        return self


for _library in ("httpx", "httpcore"):
    logging.getLogger(_library).setLevel(logging.WARNING)


@lru_cache(maxsize=1)
def tls_context() -> SSLContext:
    """Переиспользовать проверяющий TLS-контекст без повторной загрузки CA на каждый запрос."""
    return httpx.create_ssl_context(trust_env=False)


network_settings = NetworkSettings()

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VKBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    vk_group_token: str
    vk_group_id: str
    vk_proxy_url: str | None = None

    cache_expire_time: int
    anti_spam_time: int

    domofomka_api_host: str
    domofomka_api_port: int
    search_api_version: str = 'v2'

    queue_database_url: str
    queue_workers: int = 2
    queue_poll_seconds: float = 1
    queue_lease_seconds: int = 120
    queue_retry_base_seconds: float = 5
    queue_retry_max_seconds: float = 300
    callback_notice_interval_seconds: float = 10
    callback_ack_interval_seconds: float = 1
    vk_min_interval_seconds: float = 0.4
    recovery_interval_seconds: float = 300
    recovery_overlap_seconds: int = 900
    recovery_bootstrap_seconds: int = 86400
    app_environment: str = "production"
    expected_vk_group_id: str
    dev_faults_enabled: bool = False

    queue_guard_interval_seconds: float = Field(default=2, gt=0)
    queue_heartbeat_min_seconds: float = Field(default=1, gt=0)
    vk_error_pause_seconds: float = Field(default=30, gt=0)


settings = VKBotSettings()

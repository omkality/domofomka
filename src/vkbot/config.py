from pydantic_settings import BaseSettings, SettingsConfigDict


class VKBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    vk_group_token: str
    vk_group_id: str

    redis_host: str
    redis_port: int
    redis_password: str | None = None
    cache_expire_time: int
    anti_spam_time: int

    domofomka_api_host: str
    domofomka_api_port: int


settings = VKBotSettings()

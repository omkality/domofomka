from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    api_host: str
    api_port: int
    db_name: str
    dadata_token: str
    dadata_timeout_seconds: float = 8


settings = Settings()

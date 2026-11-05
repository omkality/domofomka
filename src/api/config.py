from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_debug: bool = False
    api_host: str
    api_port: int
    db_name: str
    dadata_token: str
    dadata_proxy_url: str | None = None
    search_catalog_path: str = "artifacts/search/catalog.db"
    opensearch_url: str | None = None
    opensearch_index: str = "domofomka-buildings"
    opensearch_timeout_seconds: float = Field(default=2, gt=0)
    search_candidate_limit: int = Field(default=100, ge=5, le=500)
    search_max_query_length: int = Field(default=500, ge=20, le=2000)
    search_min_similarity: float = Field(default=0.86, gt=0, le=1)
    search_ambiguity_margin: float = Field(default=0.06, ge=0, le=1)


settings = Settings()

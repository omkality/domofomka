from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_debug: bool = False
    api_host: str
    api_port: int
    db_name: str
    legacy_index_path: str = "/backend/catalog/legacy-index.db"
    dadata_token: str
    dadata_proxy_url: str | None = None
    search_catalog_path: str = "/backend/catalog/serving.db"
    search_backend: Literal["sqlite", "postgres"] = "sqlite"
    catalog_database_url: SecretStr | None = None
    catalog_revision: str = "unknown"
    catalog_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    catalog_statement_timeout_ms: int = Field(default=2000, ge=50, le=30000)
    catalog_lock_timeout_seconds: float = Field(default=2, gt=0, le=30)
    catalog_operation_timeout_seconds: float = Field(default=5, gt=0, le=30)
    opensearch_url: str | None = None
    opensearch_index: str = "domofomka-buildings"
    opensearch_timeout_seconds: float = Field(default=2, gt=0)
    opensearch_fuzziness: Literal["AUTO", "0", "1", "2"] = "AUTO"
    opensearch_prefix_length: int = Field(default=1, ge=0, le=3)
    search_candidate_limit: int = Field(default=100, ge=5, le=500)
    search_max_query_length: int = Field(default=500, ge=20, le=2000)
    search_min_similarity: float = Field(default=0.86, gt=0, le=1)
    search_ambiguity_margin: float = Field(default=0.06, ge=0, le=1)
    search_cache_ttl_seconds: float = Field(default=120, ge=0)
    search_cache_max_entries: int = Field(default=256, ge=1, le=4096)
    search_cache_max_bytes: int = Field(default=2097152, ge=1024)


settings = Settings()

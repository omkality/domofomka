"""Настройка клиента геокодирования в точке сборки приложения."""

from src.config import settings
from src.services.dadata_client import DadataClient


async def get_dadata_client() -> DadataClient:
    return DadataClient(token=settings.dadata_token, proxy_url=settings.dadata_proxy_url)

"""Зависимости HTTP-обработчиков сервиса кодов."""

from src.code_service.config import settings
from src.code_service.services.geocoding import DadataClient


async def get_dadata_client() -> DadataClient:
    """Создать клиент геокодирования с настройками текущего процесса."""
    return DadataClient(token=settings.dadata_token, proxy_url=settings.dadata_proxy_url)

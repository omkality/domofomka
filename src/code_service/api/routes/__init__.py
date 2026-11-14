"""Регистрация HTTP-маршрутов сервиса кодов."""

from copy import deepcopy

from litestar import Router

from src.code_service.api.routes.health import redirect_to_docs, version
from src.code_service.api.routes.v1.codes import codes_router
from src.code_service.api.routes.v2.codes import router as v2_router


def route_handlers() -> list:
    """Собрать v1, v2 и совместимые пути, сохранив прежние URL."""
    return [
        redirect_to_docs,
        version,
        deepcopy(codes_router),
        Router(path="/api/v1", route_handlers=[deepcopy(codes_router)]),
        deepcopy(v2_router),
    ]

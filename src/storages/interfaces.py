"""Minimal compatibility type for the legacy handler; v2 uses PostgreSQL context."""
from typing import Any, Protocol


class KeyValueClientProtocol(Protocol):
    client: Any

    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: Any, **kwargs: Any) -> Any: ...

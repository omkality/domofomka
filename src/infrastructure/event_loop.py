"""Запуск async CLI с совместимым с Psycopg циклом событий на Windows."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Закрыть цикл после команды; для Windows выбрать поддерживаемый Selector."""
    return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)

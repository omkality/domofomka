"""Явная точка входа соответствующего приложения."""

import asyncio
import logging

from src.vkbot.config import settings
from src.vkbot.runtime import Runtime


def main() -> None:
    """Запустить Long Poll, обработчики и обслуживание постоянной очереди."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(Runtime(settings).run())


if __name__ == "__main__":
    main()

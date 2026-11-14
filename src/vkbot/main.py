"""Явная точка входа соответствующего приложения."""

import logging

from src.infrastructure.event_loop import run_async
from src.vkbot.config import settings
from src.vkbot.runtime import Runtime


def main() -> None:
    """Запустить Long Poll, обработчики и обслуживание постоянной очереди."""
    logging.basicConfig(level=logging.INFO)
    run_async(Runtime(settings).run())


if __name__ == "__main__":
    main()

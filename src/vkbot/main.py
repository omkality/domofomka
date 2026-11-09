import asyncio
import logging

from src.vkbot.config import settings
from src.vkbot.runtime import Runtime


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(Runtime(settings).run())


if __name__ == "__main__":
    main()

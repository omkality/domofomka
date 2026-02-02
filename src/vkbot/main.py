import asyncio
import logging
import traceback

from redis.asyncio import Redis
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.vk_api import VkApiMethod

from src.storages.redis import RedisStorage
from src.vkbot.config import settings
from src.vkbot.handlers import MessageHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class VKBot:
    def __init__(
        self,
        token: str,
        group_id: str,
        redis_host: str,
        redis_port: int,
        redis_password: str | None = None,
    ):
        self.vk_session: VkApi = VkApi(token=token)
        self.long_poll: VkBotLongPoll = VkBotLongPoll(
            vk=self.vk_session, group_id=group_id
        )
        self.vk_api: VkApiMethod = self.vk_session.get_api()

        redis_client = Redis(host=redis_host, port=redis_port, password=redis_password)
        self.redis_storage = RedisStorage(redis_client)
        self.handler = MessageHandler(vk_api=self.vk_api, redis=self.redis_storage)

    async def run(self) -> None:
        logger.info("VK Bot started")

        while True:
            try:
                for event in self.long_poll.listen():
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        await self.handler.handle_message(event.obj)
                    elif event.type == VkBotEventType.MESSAGE_EVENT:
                        await self.handler.handle_event(event.obj)

            except Exception:
                logger.error(f"Error in main loop: {traceback.format_exc()}")
                await asyncio.sleep(3)

    async def cleanup(self) -> None:
        await self.redis_storage.client.aclose()


async def main() -> None:
    bot = VKBot(
        token=settings.vk_group_token,
        group_id=settings.vk_group_id,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password,
    )
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await bot.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

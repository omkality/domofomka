import json
import logging
import traceback

from pyreqwest.simple.request import pyreqwest_get
from vk_api.utils import get_random_id
from vk_api.vk_api import VkApiMethod

from src.storages.interfaces import KeyValueClientProtocol
from src.vkbot.config import settings
from src.vkbot.keyboard import build_many_buttons, get_location_keyboard

logger = logging.getLogger(__name__)


class MessageHandler:
    def __init__(self, vk_api: VkApiMethod, redis: KeyValueClientProtocol):
        self.vk = vk_api
        self.redis = redis

    def handle_start(self, user_id: int) -> None:
        try:
            with open("/backend/start_message.txt", encoding="utf8") as f:
                message = f.read()
        except Exception:
            logger.error(f"Failed to read start message: {traceback.format_exc()}")
            raise

        self.vk.messages.send(
            user_id=user_id,
            random_id=get_random_id(),
            message=message,
            keyboard=get_location_keyboard(),
        )

    def check_subscription(self, user_id: int) -> bool:
        try:
            result = self.vk.groups.isMember(
                group_id=settings.vk_group_id,
                user_id=user_id,
            )
            return result == 1
        except Exception:
            logger.error(f"Failed to check subscription: {traceback.format_exc()}")
            raise

    async def get_codes_by_geo(self, lat: float, lon: float) -> dict:
        url = f"http://{settings.domofomka_api_host}:{settings.domofomka_api_port}/codes/geo"

        try:
            response = await pyreqwest_get(url).query({"lat": lat, "lon": lon}).send()

            if response.status == 200:
                return await response.json()
            else:
                logger.error(f"API error: {response.status}")
                raise RuntimeError(f"API returned status {response.status}")

        except Exception:
            logger.error(f"Failed to get codes by geo: {traceback.format_exc()}")
            raise

    async def get_codes_by_message(self, message: str) -> dict:
        url = f"http://{settings.domofomka_api_host}:{settings.domofomka_api_port}/codes/msg"

        try:
            response = await pyreqwest_get(url).query({"message": message}).send()

            if response.status == 200:
                return await response.json()
            else:
                logger.error(f"API error: {response.status}")
                raise RuntimeError(f"API returned status {response.status}")

        except Exception:
            logger.error(f"Failed to get codes by message: {traceback.format_exc()}")
            raise

    async def handle_message(self, event: dict) -> None:
        user_id = event["message"]["from_id"]
        text = event["message"]["text"].lower()

        if text == "начать":
            self.handle_start(user_id)
            return

        if not self.check_subscription(user_id):
            self.vk.messages.send(
                user_id=user_id,
                random_id=get_random_id(),
                message="Пожалуйста, подпишитесь на группу",
                keyboard=get_location_keyboard(),
            )
            return

        result = None
        if "geo" in event["message"]:
            coords = event["message"]["geo"]["coordinates"]
            lat, lon = coords["latitude"], coords["longitude"]
            result = await self.get_codes_by_geo(lat, lon)
        else:
            result = await self.get_codes_by_message(event["message"]["text"])

        if not result:
            self.vk.messages.send(
                user_id=user_id,
                random_id=get_random_id(),
                message="Нет результатов",
            )
            return

        entrances = list(result["data"].keys())
        number_of_messages = (len(entrances) - 1) // 10 + 1

        for i in range(number_of_messages):
            ent_slice = entrances[i * 10 : (i + 1) * 10]
            buttons = build_many_buttons(ent_slice)

            if not await self.redis.get(f"vk:user:{user_id}:action"):
                msg_id = self.vk.messages.send(
                    user_id=user_id,
                    random_id=get_random_id(),
                    message=result["address"],
                    keyboard=json.dumps(
                        {
                            "inline": True,
                            "buttons": buttons,
                        }
                    ),
                )

                msg_data = self.vk.messages.getById(message_ids=msg_id)
                conversation_message_id = msg_data["items"][0][
                    "conversation_message_id"
                ]

                await self.redis.set(
                    f"vk:user:{user_id}:message:{conversation_message_id}",
                    json.dumps(result),
                    ttl=settings.cache_expire_time,
                )

    async def handle_event(self, event: dict) -> None:
        user_id = event["user_id"]
        peer_id = event["peer_id"]
        payload = event["payload"]
        conversation_message_id = event["conversation_message_id"]

        if await self.redis.get(f"vk:user:{user_id}:action"):
            ttl = await self.redis.client.ttl(f"vk:user:{user_id}:action")
            if ttl > 0:
                sec_str = "секунд" + (
                    "у"
                    if ttl % 10 == 1 and ttl % 100 != 11
                    else (
                        "ы"
                        if ttl % 10 in [2, 3, 4] and ttl not in range(10, 15)
                        else ""
                    )
                )

                self.vk.messages.sendMessageEventAnswer(
                    event_id=event["event_id"],
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data=json.dumps(
                        {
                            "type": "show_snackbar",
                            "text": f"Подождите {ttl} {sec_str}!",
                        }
                    ),
                )
                return

        cached = await self.redis.get(
            f"vk:user:{user_id}:message:{conversation_message_id}"
        )

        if cached:
            result = json.loads(cached)
        else:
            msg_data = self.vk.messages.getByConversationMessageId(
                peer_id=peer_id,
                conversation_message_ids=conversation_message_id,
            )

            address = msg_data["items"][0]["text"].split("\n")[0]
            message_text = address.replace(",", "").replace(" дом ", " ")
            result = await self.get_codes_by_message(message_text)

        possible_types = ["yaeda", "delivery", "oldcodes"]
        codes = result["data"][payload["entrance"]]
        codes.sort(key=lambda x: possible_types.index(x[1]))

        answer = ""
        current_type = ""

        for code, code_type in codes:
            if code_type.upper() != current_type:
                current_type = code_type.upper()
                answer += f"\n{current_type}:\n"
            answer += code + " "

        self.vk.messages.edit(
            peer_id=peer_id,
            conversation_message_id=conversation_message_id,
            message=f"{result['address']}\nПодъезд {payload['entrance']}\n{answer}",
            keyboard=json.dumps(
                {
                    "inline": True,
                    "buttons": build_many_buttons(payload["ent_slice"]),
                }
            ),
        )

        await self.redis.set(
            f"vk:user:{user_id}:action",
            "button_tap",
            ttl=settings.anti_spam_time,
        )
        await self.redis.set(
            f"vk:user:{user_id}:message:{conversation_message_id}",
            json.dumps(result),
            ttl=settings.cache_expire_time,
        )

"""Plan durable responses separately from sending them to VK."""

import json
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from pyreqwest.client import SyncClientBuilder
from pyreqwest.exceptions import PyreqwestError
from vk_api import VkApi

from src.network import network_settings
from src.vkbot.http import TimeoutSession
from src.vkbot.keyboard import build_many_buttons, get_location_keyboard
from src.vkbot.queue import QueueStore


class RateGate:
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self) -> None:
        with self.lock:
            remaining = self.interval - (time.monotonic() - self.last)
            if remaining > 0:
                time.sleep(remaining)
            self.last = time.monotonic()


class RateSession(TimeoutSession):
    def __init__(self, gate: RateGate, *, longpoll: bool = False):
        super().__init__(longpoll=longpoll)
        self.gate = gate

    def send(self, request: Any, **kwargs: Any) -> Any:
        # Gate actual HTTP attempts, including retries inside TimeoutSession.
        self.gate.wait()
        return super().send(request, **kwargs)


def inline_keyboard(entrances: list[str]) -> str:
    return json.dumps({"inline": True, "buttons": build_many_buttons(entrances)}, ensure_ascii=False)


def code_answer(result: dict, entrance: str) -> str:
    order = {"yaeda": 0, "delivery": 1, "oldcodes": 2}
    codes = sorted(result["data"][entrance], key=lambda item: order.get(item[1], 99))
    lines = []
    previous = None
    for code, kind in codes:
        if kind != previous:
            lines.extend(["", kind.upper() + ":"])
            previous = kind
        lines.append(code)
    return f"{result['address']}\nПодъезд {entrance}\n" + "\n".join(lines)


class LookupUnavailable(RuntimeError):
    """The internal lookup API could not complete this attempt."""


class Processor:
    def __init__(self, settings: Any, store: QueueStore, gate: RateGate):
        self.settings = settings
        self.store = store
        self.vk = VkApi(token=settings.vk_group_token, api_version="5.199", session=RateSession(gate)).get_api()
        self.http = (
            SyncClientBuilder()
            .no_proxy()
            .connect_timeout(timedelta(seconds=network_settings.internal_api_connect_timeout_seconds))
            .timeout(timedelta(seconds=network_settings.internal_api_timeout_seconds))
            .build()
        )

    def close(self) -> None:
        self.http.close()
        self.vk._vk.http.close()

    def fault(self, name: str) -> None:
        if self.settings.dev_faults_enabled and self.store.consume_fault(name):
            raise TimeoutError("Injected dev failure")

    def lookup(self, message: str | None = None, geo: dict | None = None) -> dict:
        url = f"http://{self.settings.domofomka_api_host}:{self.settings.domofomka_api_port}/codes"
        try:
            self.fault("api")
            if geo:
                pending = self.http.get(url + "/geo").query({"lat": geo["latitude"], "lon": geo["longitude"]})
            else:
                pending = self.http.get(url + "/msg").query({"message": message or ""})
            response = pending.build().send()
            if response.status != 200:
                raise LookupUnavailable("Lookup API returned an unsuccessful response")
            return response.json()
        except (PyreqwestError, TimeoutError):
            raise LookupUnavailable("Lookup API is temporarily unavailable") from None

    def plan(self, job: dict) -> None:
        if job["kind"] == "message_new":
            self._message(job)
        elif job["kind"] == "message_event":
            self._callback(job)
        else:
            raise ValueError("Unsupported inbox event")

    def _message(self, job: dict) -> None:
        message = job["payload"]["message"]
        peer = job["peer_id"]
        base = {"peer_id": peer}
        if message.get("id"):
            base["reply_to"] = message["id"]
        effects = []
        if message.get("text", "").strip().lower() == "начать":
            effects.append(
                {
                    "method": "send",
                    "params": dict(
                        base,
                        message=Path("start_message.txt").read_text(encoding="utf8"),
                        keyboard=get_location_keyboard(),
                    ),
                }
            )
        elif not self.vk.groups.isMember(group_id=self.settings.vk_group_id, user_id=peer):
            effects.append(
                {
                    "method": "send",
                    "params": dict(base, message="Пожалуйста, подпишитесь на группу", keyboard=get_location_keyboard()),
                }
            )
        else:
            geo = message.get("geo", {}).get("coordinates")
            try:
                result = self.lookup(message.get("text"), geo)
            except LookupUnavailable:
                if not geo:
                    raise
                # Respond to an unavailable geocoder, rather than blocking this
                # peer indefinitely behind retries of the same geolocation.
                self.store.plan(
                    job,
                    [
                        {
                            "method": "send",
                            "params": dict(
                                base,
                                message=(
                                    "Не удалось определить адрес по геолокации. Пожалуйста, отправьте адрес текстом."
                                ),
                            ),
                        }
                    ],
                )
                return
            if not result or not result.get("data"):
                effects.append({"method": "send", "params": dict(base, message="Нет результатов")})
            else:
                entrances = list(result["data"])
                for offset in range(0, len(entrances), 10):
                    subset = entrances[offset : offset + 10]
                    effects.append(
                        {
                            "method": "send",
                            "params": dict(base, message=result["address"], keyboard=inline_keyboard(subset)),
                            "context": result,
                        }
                    )
        self.store.plan(job, effects)

    def _callback(self, job: dict) -> None:
        obj = job["payload"]
        payload = obj["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        peer = job["peer_id"]
        # Distinct clicks remain ordered, including rapid entrance changes.
        # Durable ingest coalesces consecutive identical clicks before planning.
        cmid = int(obj["conversation_message_id"])
        result = self.store.message_context(peer, cmid)
        if result is None:
            messages = self.vk.messages.getByConversationMessageId(peer_id=peer, conversation_message_ids=cmid)["items"]
            if not messages:
                raise ValueError("Callback refers to a missing message")
            address = messages[0]["text"].split("\n")[0]
            result = self.lookup(address.replace(",", "").replace(" дом ", " "))
        entrance = str(payload["entrance"])
        if not result or entrance not in result.get("data", {}):
            raise ValueError("Selected entrance is no longer available")
        subset = [str(value) for value in payload["ent_slice"]]
        effect = {
            "method": "edit",
            "params": {
                "peer_id": peer,
                "conversation_message_id": cmid,
                "message": code_answer(result, entrance),
                "keyboard": inline_keyboard(subset),
            },
            "context": result,
        }
        self.store.plan(job, [effect], callback_at=job["occurred_at"])

    def deliver(self, job: dict) -> None:
        params = dict(job["params"])
        if not self.store.renew("bot_outbox", job):
            raise RuntimeError("Queue lease lost before dispatch")
        if job["method"] == "send":
            # The database sequence is persistent and unique for this queue.
            if job["id"] > 2147483647:
                raise ValueError("Outbox random_id sequence exhausted")
            params["random_id"] = job["id"]
            result = job.get("result")
            if result is None:
                self.fault("vk-send")
                result = self.vk.messages.send(**params)
                self.fault("vk-send-after")
                self.store.save_result(job, result)
            if isinstance(result, dict):
                result = result["message_id"]
            context = None
            if job.get("context"):
                messages = self.vk.messages.getById(message_ids=result)["items"]
                if not messages:
                    raise RuntimeError("Sent message not yet available")
                context = (int(messages[0]["conversation_message_id"]), job["context"])
            self.store.finish(job, context)
        elif job["method"] == "edit":
            self.fault("vk-edit")
            self.vk.messages.edit(**params)
            self.fault("vk-edit-after")
            context = (int(params["conversation_message_id"]), job["context"]) if job.get("context") else None
            self.store.finish(job, context)
        else:
            raise ValueError("Unsupported outbox method")

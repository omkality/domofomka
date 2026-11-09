"""Plan durable responses separately from sending them to VK."""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from vk_api import VkApi

from src.infrastructure.network import network_settings, tls_context
from src.infrastructure.telemetry import active, measured, merge, note, search_result, span
from src.vkbot.http import TimeoutSession
from src.vkbot.keyboard import (
    INLINE_BUTTON_LIMIT,
    build_many_buttons,
    callback_button,
    get_location_keyboard,
    serialize_inline_keyboard,
)
from src.vkbot.queue import QueueStore

ADDRESS_PAGE_SIZE = 5
# Reserve previous/next page and return-to-addresses buttons within the same budget.
SELECTED_ENTRANCES_PER_PAGE = INLINE_BUTTON_LIMIT - 3


class RateGate:
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    @measured("vk.rate_limit_wait")
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
    return serialize_inline_keyboard(build_many_buttons(entrances))


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


def address_choices(result: dict, page: int = 0) -> tuple[str, str]:
    choices = result["candidates"]
    size = ADDRESS_PAGE_SIZE
    start = page * size
    if page < 0 or start >= len(choices):
        raise ValueError("Invalid address page")
    subset = choices[start : start + size]
    labels = [c["address"] for c in subset]
    message = "Уточните адрес:\n" + "\n".join(f"{start + i + 1}. {label}" for i, label in enumerate(labels))
    message += "\nЕсли нужного варианта нет, пришлите адрес целиком одним сообщением: город, улицу и номер дома."

    rows = [[callback_button(str(start + i + 1), {"select_building": start + i}) for i in range(len(subset))]]
    navigation = []
    if page > 0:
        navigation.append(callback_button("Назад", {"choice_page": page - 1}))
    if start + size < len(choices):
        navigation.append(callback_button("Далее", {"choice_page": page + 1}))
    if navigation:
        rows.append(navigation)
    rows.append([callback_button("Ни один не подходит", {"dismiss_choices": True})])
    return message, serialize_inline_keyboard(rows)


def selected_keyboard(result: dict) -> str:
    """Подъезды выбранного адреса и возврат к сохранённому списку в одном сообщении."""
    entrances = list(result.get("data", {}))
    page = result["_address_choice"].get("entrance_page", 0)
    start = page * SELECTED_ENTRANCES_PER_PAGE
    if page < 0 or (start >= len(entrances) and (page or entrances)):
        raise ValueError("Invalid entrance page")
    subset = entrances[start : start + SELECTED_ENTRANCES_PER_PAGE]
    rows = build_many_buttons(subset) if subset else []
    navigation = []
    if page:
        navigation.append(callback_button("Предыдущие подъезды", {"entrance_page": page - 1}))
    if start + SELECTED_ENTRANCES_PER_PAGE < len(entrances):
        navigation.append(callback_button("Следующие подъезды", {"entrance_page": page + 1}))
    if navigation:
        rows.append(navigation)
    rows.append([callback_button("Назад к адресам", {"return_to_choices": True})])
    return serialize_inline_keyboard(rows)


class LookupUnavailable(RuntimeError):
    """The internal lookup API could not complete this attempt."""


class Processor:
    def __init__(self, settings: Any, store: QueueStore, gate: RateGate):
        self.settings = settings
        self.store = store
        self.vk = VkApi(token=settings.vk_group_token, api_version="5.199", session=RateSession(gate)).get_api()

    def close(self) -> None:
        self.vk._vk.http.close()

    def _get(self, url: str, params: dict | None = None, *, timeout: float | None = None) -> httpx.Response:
        total = timeout if timeout is not None else network_settings.internal_api_timeout_seconds

        async def request() -> httpx.Response:
            async with (
                asyncio.timeout(total),
                httpx.AsyncClient(
                    verify=tls_context(),
                    trust_env=False,
                    timeout=httpx.Timeout(
                        total, connect=min(network_settings.internal_api_connect_timeout_seconds, total)
                    ),
                ) as client,
            ):
                return await client.get(url, params=params)

        # Планирование выполняется в рабочем потоке. Один бюджет включает получение тела,
        # а клиент закрывается до завершения его цикла событий.
        return asyncio.run(request())

    def fault(self, name: str) -> None:
        if self.settings.dev_faults_enabled and self.store.consume_fault(name):
            raise TimeoutError("Injected dev failure")

    @measured("api.lookup")
    def lookup(self, message: str | None = None, geo: dict | None = None) -> dict:
        if message and len(message) > getattr(self.settings, "search_max_query_length", 500):
            return {"status": "invalid_query", "data": {}, "engine": "v2"}
        version = getattr(self.settings, "search_api_version", "v1")
        url = f"http://{self.settings.domofomka_api_host}:{self.settings.domofomka_api_port}/api/{version}/codes"
        try:
            self.fault("api")
            params = {"lat": geo["latitude"], "lon": geo["longitude"]} if geo else {"message": message or ""}
            if active():
                params["observe"] = "true"
            response = self._get(
                url + ("/geo" if geo else "/msg"),
                params,
                timeout=network_settings.internal_api_geo_timeout_seconds if geo else None,
            )
            try:
                result = response.json()
            except (ValueError, TypeError):
                raise LookupUnavailable("Lookup API returned invalid JSON") from None
            if active():
                packet = result.pop("_diagnostics", None)
                if packet:
                    merge(packet)
                else:
                    note(api_measurements="missing")
            if response.status_code != 200:
                extra = result.get("extra")
                if (
                    geo
                    and response.status_code == 503
                    and isinstance(extra, dict)
                    and (extra.get("geocoding_retryable") is False)
                ):
                    return {"status": "geocoding_unavailable", "data": {}, "engine": version}
                raise LookupUnavailable("Lookup API returned an unsuccessful response")
            return result
        except (httpx.HTTPError, TimeoutError):
            raise LookupUnavailable("Lookup API is temporarily unavailable") from None

    def plan(self, job: dict) -> None:
        if job["kind"] in {"message_new", "geo_retry"}:
            self._message(job)
        elif job["kind"] == "message_event":
            self._callback(job)
        else:
            raise ValueError("Unsupported inbox event")

    def _message(self, job: dict) -> None:
        if job["kind"] == "geo_retry" and self.store.geo_retry_current(job) is False:
            note(decision="geo_obsolete_or_expired")
            self.store.defer_geo_retry(job, self.settings.geo_retry_delay_seconds)
            return
        message = job["payload"]["message"]
        peer = job["peer_id"]
        base = {"peer_id": peer}
        if message.get("id"):
            base["reply_to"] = message["id"]
        effects = []
        if message.get("text", "").strip().lower() == "начать":
            note(decision="start_message")
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
            note(decision="subscription_required")
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
                note(result=search_result(result))
                if active():
                    from src.vkbot.telemetry import identity

                    result = dict(result, _request_id=identity(self.store.group_id, job["id"]))
            except LookupUnavailable:
                if not geo:
                    raise
                if job["kind"] == "geo_retry":
                    note(decision="geo_deferred_again")
                    self.store.defer_geo_retry(job, self.settings.geo_retry_delay_seconds)
                    return
                note(decision="geo_deferred_with_notice")
                self.store.plan(
                    job,
                    [
                        {
                            "method": "send",
                            "params": dict(
                                base,
                                message=(
                                    "Не удалось определить адрес по геолокации. "
                                    "Запрос сохранён, попробую повторить позже. "
                                    "Можно отправить адрес текстом — он заменит отложенный запрос."
                                ),
                            ),
                            "defer_geo": {
                                "delay": self.settings.geo_retry_delay_seconds,
                                "max_age": self.settings.geo_retry_max_age_seconds,
                            },
                        }
                    ],
                )
                return
            if result.get("status") == "geocoding_unavailable":
                note(decision="geo_access_unavailable")
                effects.append(
                    {
                        "method": "send",
                        "params": dict(
                            base,
                            message="Определение адреса по геолокации временно недоступно. Отправьте адрес текстом.",
                        ),
                    }
                )
            elif result.get("status") == "invalid_query":
                effects.append(
                    {
                        "method": "send",
                        "params": dict(base, message="Пришлите улицу и номер дома одним коротким сообщением."),
                    }
                )
            elif result.get("status") == "incomplete_query":
                effects.append(
                    {
                        "method": "send",
                        "params": dict(
                            base, message="Пришлите адрес целиком одним сообщением: город, улицу и номер дома."
                        ),
                    }
                )
            elif result.get("status") == "no_codes_ambiguous":
                effects.append(
                    {
                        "method": "send",
                        "params": dict(
                            base,
                            message=(
                                "По этому запросу кодов в базе не найдено. Чтобы проверить конкретный дом, "
                                "пришлите адрес целиком одним сообщением: город, улицу и номер дома."
                            ),
                        ),
                    }
                )
            elif result.get("status") == "no_codes":
                effects.append(
                    {
                        "method": "send",
                        "params": dict(
                            base, message=f"{result['address']}\nПо этому адресу кодов в нашей базе пока нет."
                        ),
                    }
                )
            elif result.get("status") == "ambiguous":
                message, keyboard = address_choices(result)
                effects.append(
                    {"method": "send", "params": dict(base, message=message, keyboard=keyboard), "context": result}
                )
            elif not result or not result.get("data"):
                text = (
                    "Не удалось найти адрес. Укажите город, улицу и номер дома."
                    if result.get("status") == "not_found"
                    else "Нет результатов"
                )
                effects.append({"method": "send", "params": dict(base, message=text)})
            else:
                entrances = list(result["data"])
                for offset in range(0, len(entrances), INLINE_BUTTON_LIMIT):
                    subset = entrances[offset : offset + INLINE_BUTTON_LIMIT]
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
        # Ingest enforces a shared per-user cooldown before any planning.
        cmid = int(obj["conversation_message_id"])
        result = self.store.message_context(peer, cmid)
        if result:
            note(context_hit=True, result=search_result(result))
        if result and result.get("_request_id"):
            note(parent_request_id=result["_request_id"])
        actions = {
            "dismiss_choices": "dismiss",
            "select_building": "select_building",
            "choice_page": "page",
            "return_to_choices": "return_to_choices",
            "entrance_page": "entrance_page",
        }
        action = next((name for key, name in actions.items() if key in payload), "entrance")
        note(
            callback_action=action,
            selected_entrance=str(payload.get("entrance", ""))[:40],
        )
        choice_action = action != "entrance"
        if choice_action and result is None:
            note(decision="choices_expired")
            self.store.plan(
                job,
                [
                    {
                        "method": "edit",
                        "params": {
                            "peer_id": peer,
                            "conversation_message_id": cmid,
                            "message": (
                                "Срок хранения вариантов истёк. Пришлите адрес целиком одним сообщением: "
                                "город, улицу и номер дома."
                            ),
                            "keyboard": serialize_inline_keyboard([]),
                        },
                        "context": {"status": "dismissed", "data": {}},
                        "close_choices": True,
                    }
                ],
            )
            return
        if payload.get("return_to_choices") is True or "entrance_page" in payload:
            navigation = result.get("_address_choice") if result else None
            if not navigation or result.get("status") == "dismissed":
                self.store.plan(job, [])
                return
            if payload.get("return_to_choices") is True:
                note(decision="return_to_address_choices")
                message, keyboard = address_choices(navigation["result"], navigation["page"])
                # Reopen only when VK confirms this edit; the flag travels with the durable outbox.
                context = dict(navigation["result"], _reopen_choices=True)
            else:
                note(decision="entrances_page")
                context = dict(result, _address_choice=dict(navigation, entrance_page=int(payload["entrance_page"])))
                message, keyboard = context["address"], selected_keyboard(context)
            self.store.plan(
                job,
                [
                    {
                        "method": "edit",
                        "params": {
                            "peer_id": peer,
                            "conversation_message_id": cmid,
                            "message": message,
                            "keyboard": keyboard,
                        },
                        "context": context,
                    }
                ],
            )
            return
        if (
            (choice_action and self.store.choices_closed(peer, cmid) is True)
            or (choice_action and result and result.get("status") != "ambiguous" and not result.get("candidates"))
            or (result and result.get("status") == "dismissed")
        ):
            note(decision="choices_already_closed")
            self.store.plan(job, [])
            return
        if payload.get("dismiss_choices") is True:
            note(decision="none_of_choices")
            self.store.plan(
                job,
                [
                    {
                        "method": "edit",
                        "params": {
                            "peer_id": peer,
                            "conversation_message_id": cmid,
                            "message": (
                                "Ни один вариант не подошёл. Пришлите адрес целиком одним сообщением: "
                                "город, улицу и номер дома."
                            ),
                            "keyboard": serialize_inline_keyboard([]),
                        },
                        "context": {"status": "dismissed", "data": {}},
                        "close_choices": True,
                    }
                ],
            )
            return
        if "choice_page" in payload:
            note(decision="choices_page")
            if not result or result.get("status") != "ambiguous":
                raise ValueError("No saved address choices")
            message, keyboard = address_choices(result, int(payload["choice_page"]))
            self.store.plan(
                job,
                [
                    {
                        "method": "edit",
                        "params": {
                            "peer_id": peer,
                            "conversation_message_id": cmid,
                            "message": message,
                            "keyboard": keyboard,
                        },
                        "context": result,
                    }
                ],
            )
            return
        if "select_building" in payload:
            from urllib.parse import quote

            index = int(payload["select_building"])
            if not result or not 0 <= index < len(result.get("candidates", [])):
                raise ValueError("Selection is not part of the saved search result")
            building = result["candidates"][index]["building_id"]
            navigation = {
                "result": dict(result, status="ambiguous"),
                "page": index // ADDRESS_PAGE_SIZE,
                "entrance_page": 0,
            }
            note(selected_building_id=building, selected_candidate_index=index)
            base_url = f"http://{self.settings.domofomka_api_host}:{self.settings.domofomka_api_port}"
            url = base_url + "/api/v2/buildings/" + quote(building, safe="")
            with span("api.building_lookup"):
                response = self._get(url, {"observe": "true"} if active() else None)
            if response.status_code == 404:
                context = {"status": "not_found", "data": {}, "_address_choice": navigation}
                self.store.plan(
                    job,
                    [
                        {
                            "method": "edit",
                            "params": {
                                "peer_id": peer,
                                "conversation_message_id": cmid,
                                "message": (
                                    "Этот адрес больше недоступен. Вернитесь к списку и выберите другой вариант."
                                ),
                                "keyboard": selected_keyboard(context),
                            },
                            "context": context,
                            "close_choices": True,
                        }
                    ],
                )
                return
            if response.status_code != 200:
                raise LookupUnavailable("Building lookup unavailable")
            answer = response.json()
            answer = dict(answer, _address_choice=navigation)
            if active():
                packet = answer.pop("_diagnostics", None)
                if packet:
                    merge(packet)
                else:
                    note(api_measurements="missing")
            note(result=search_result(answer))
            if active():
                from src.vkbot.telemetry import identity

                answer = dict(answer, _request_id=identity(self.store.group_id, job["id"]))
            keyboard = selected_keyboard(answer)
            message = (
                answer["address"]
                if answer.get("data")
                else answer["address"] + "\nПо этому адресу кодов в нашей базе пока нет."
            )
            effects = [
                {
                    "method": "edit",
                    "params": {
                        "peer_id": peer,
                        "conversation_message_id": cmid,
                        "message": message,
                        "keyboard": keyboard,
                    },
                    "context": answer,
                    "close_choices": True,
                }
            ]
            self.store.plan(job, effects)
            return
        if result is None:
            note(context_source="vk_message_recovery")
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
                "keyboard": selected_keyboard(result) if result.get("_address_choice") else inline_keyboard(subset),
            },
            "context": result,
        }
        self.store.plan(job, [effect], callback_at=job["occurred_at"])

    def deliver(self, job: dict) -> None:
        params = dict(job["params"])
        if not self.store.renew("bot_outbox", job):
            raise RuntimeError("Queue lease lost before dispatch")
        if job.get("result") is None and self.store.outbox_geo_current(job) is False:
            note(decision="delivery_obsolete")
            self.store.finish(job)
            return
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

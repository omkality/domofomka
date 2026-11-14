"""Plan durable responses separately from sending them to VK."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from vk_api import VkApi

from src.infrastructure.network import network_settings, tls_context
from src.infrastructure.telemetry import active, measured, merge, note, search_result, span
from src.vkbot.http import RateGate, RateSession, call_vk
from src.vkbot.keyboard import (
    INLINE_BUTTON_LIMIT,
    get_location_keyboard,
    serialize_inline_keyboard,
)
from src.vkbot.presentation import (
    ADDRESS_PAGE_SIZE,
    address_choices,
    building_answer,
    code_answer,
    inline_keyboard,
    selected_keyboard,
)
from src.vkbot.queue import QueueStore


class LookupUnavailable(RuntimeError):
    """The internal lookup API could not complete this attempt."""


class Processor:
    """Планировщик ответов и исполнитель сохранённых действий VK."""

    def __init__(self, settings: Any, store: QueueStore, gate: RateGate) -> None:
        """Подключить очередь, VK-клиент и параметры планирования ответов."""
        self.settings = settings
        self.store = store
        self.vk = VkApi(token=settings.vk_group_token, api_version="5.199", session=RateSession(gate)).get_api()
        self.client = httpx.AsyncClient(
            verify=tls_context(), trust_env=False, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)
        )

    async def close(self) -> None:
        """Закрыть открытые соединения и ресурсы компонента."""
        await self.client.aclose()
        self.vk._vk.http.close()

    async def _get(self, url: str, params: dict | None = None, *, timeout: float | None = None) -> httpx.Response:
        """Вызвать внутренний API с общим дедлайном, включая чтение тела."""
        total = timeout if timeout is not None else network_settings.internal_api_timeout_seconds

        async with asyncio.timeout(total):
            return await self.client.get(
                url,
                params=params,
                timeout=httpx.Timeout(total, connect=min(network_settings.internal_api_connect_timeout_seconds, total)),
            )

    async def fault(self, name: str) -> None:
        """Применить разрешённую dev-подстановку ошибки к текущему сценарию."""
        if self.settings.dev_faults_enabled and (await self.store.consume_fault(name)):
            raise TimeoutError("Injected dev failure")

    @measured("api.lookup")
    async def lookup(self, message: str | None = None, geo: dict | None = None) -> dict:
        """Получить результат внутреннего поиска с ограниченным сетевым бюджетом."""
        if message and len(message) > getattr(self.settings, "search_max_query_length", 500):
            return {"status": "invalid_query", "data": {}, "engine": "v2"}
        version = getattr(self.settings, "search_api_version", "v1")
        url = f"http://{self.settings.domofomka_api_host}:{self.settings.domofomka_api_port}/api/{version}/codes"
        try:
            await self.fault("api")
            params = {"lat": geo["latitude"], "lon": geo["longitude"]} if geo else {"message": message or ""}
            if active():
                params["observe"] = "true"
            response = await self._get(
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

    async def plan(self, job: dict) -> None:
        """Подготовить постоянные эффекты входящего события без немедленной отправки."""
        if job["kind"] in {"message_new", "geo_retry"}:
            await self._message(job)
        elif job["kind"] == "message_event":
            await self._callback(job)
        else:
            raise ValueError("Unsupported inbox event")

    async def _message(self, job: dict) -> None:
        """Подготовить ответ на входящее сообщение, не отправляя его в VK."""
        if job["kind"] == "geo_retry" and (await self.store.geo_retry_current(job)) is False:
            note(decision="geo_obsolete_or_expired")
            await self.store.defer_geo_retry(job, self.settings.geo_retry_delay_seconds)
            return
        message = job["payload"]["message"]
        peer = job["peer_id"]
        base = {"peer_id": peer}
        if message.get("id"):
            base["reply_to"] = message["id"]
        effects: list[dict[str, Any]] = []
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
        elif not (await call_vk(self.vk.groups.isMember, group_id=self.settings.vk_group_id, user_id=peer)):
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
                result = await self.lookup(message.get("text"), geo)
                note(result=search_result(result))
                if active():
                    from src.vkbot.telemetry import identity

                    result = dict(result, _request_id=identity(self.store.group_id, job["id"]))
            except LookupUnavailable:
                if not geo:
                    raise
                if job["kind"] == "geo_retry":
                    note(decision="geo_deferred_again")
                    await self.store.defer_geo_retry(job, self.settings.geo_retry_delay_seconds)
                    return
                note(decision="geo_deferred_with_notice")
                await self.store.plan(
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
                        "hint": True,
                    }
                )
            elif result.get("status") == "incomplete_query":
                effects.append(
                    {
                        "method": "send",
                        "params": dict(
                            base, message="Пришлите адрес целиком одним сообщением: город, улицу и номер дома."
                        ),
                        "hint": True,
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
                effects.append({"method": "send", "params": dict(base, message=text), "hint": True})
            else:
                entrances = list(result["data"])
                for offset in range(0, len(entrances), INLINE_BUTTON_LIMIT):
                    subset = entrances[offset : offset + INLINE_BUTTON_LIMIT]
                    effects.append(
                        {
                            "method": "send",
                            "params": dict(
                                base,
                                message=building_answer(result),
                                keyboard=inline_keyboard(subset if len(entrances) > 1 else []),
                            ),
                            "context": result if len(entrances) > 1 else None,
                            "automatic_code": len(entrances) == 1,
                        }
                    )
        await self.store.plan(job, effects)

    async def _callback(self, job: dict) -> None:
        """Подготовить действие кнопки по сохранённому контексту сообщения."""
        obj = job["payload"]
        payload = obj["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        peer = job["peer_id"]
        # Ingest enforces a shared per-user cooldown before any planning.
        cmid = int(obj["conversation_message_id"])
        result = await self.store.message_context(peer, cmid)
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
            await self.store.plan(
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
                await self.store.plan(job, [])
                return
            if payload.get("return_to_choices") is True:
                note(decision="return_to_address_choices")
                message, keyboard = address_choices(navigation["result"], navigation["page"])
                # Reopen only when VK confirms this edit; the flag travels with the durable outbox.
                context = dict(navigation["result"], _reopen_choices=True)
            else:
                note(decision="entrances_page")
                context = dict(result, _address_choice=dict(navigation, entrance_page=int(payload["entrance_page"])))
                message, keyboard = building_answer(context), selected_keyboard(context)
            await self.store.plan(
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
            (choice_action and (await self.store.choices_closed(peer, cmid)) is True)
            or (choice_action and result and result.get("status") != "ambiguous" and not result.get("candidates"))
            or (result and result.get("status") == "dismissed")
        ):
            note(decision="choices_already_closed")
            await self.store.plan(job, [])
            return
        if payload.get("dismiss_choices") is True:
            note(decision="none_of_choices")
            await self.store.plan(
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
            await self.store.plan(
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
                response = await self._get(url, {"observe": "true"} if active() else None)
            if response.status_code == 404:
                context = {"status": "not_found", "data": {}, "_address_choice": navigation}
                await self.store.plan(
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
            message = building_answer(answer)
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
                    "automatic_code": len(answer.get("data", {})) == 1,
                }
            ]
            await self.store.plan(job, effects)
            return
        if result is None:
            note(context_source="vk_message_recovery")
            messages = (
                await call_vk(self.vk.messages.getByConversationMessageId, peer_id=peer, conversation_message_ids=cmid)
            )["items"]
            if not messages:
                raise ValueError("Callback refers to a missing message")
            address = messages[0]["text"].split("\n")[0]
            result = await self.lookup(address.replace(",", "").replace(" дом ", " "))
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
                "keyboard": (
                    selected_keyboard(result)
                    if result.get("_address_choice")
                    else inline_keyboard(subset if len(result["data"]) > 1 else [])
                ),
            },
            "context": result,
        }
        await self.store.plan(job, [effect], callback_at=job["occurred_at"])

    async def deliver(self, job: dict) -> None:
        """Исполнить сохранённое действие и подтвердить его только после ответа VK."""
        params = dict(job["params"])
        if not (await self.store.renew("bot_outbox", job)):
            raise RuntimeError("Queue lease lost before dispatch")
        if job.get("result") is None and (await self.store.outbox_geo_current(job)) is False:
            note(decision="delivery_obsolete")
            await self.store.finish(job)
            return
        if job["method"] == "send":
            # The database sequence is persistent and unique for this queue.
            if job["id"] > 2147483647:
                raise ValueError("Outbox random_id sequence exhausted")
            params["random_id"] = job["id"]
            result = job.get("result")
            if result is None:
                await self.fault("vk-send")
                result = await call_vk(self.vk.messages.send, **params)
                await self.fault("vk-send-after")
                await self.store.save_result(job, result)
            if isinstance(result, dict):
                result = result["message_id"]
            context = None
            if job.get("context"):
                messages = (await call_vk(self.vk.messages.getById, message_ids=result))["items"]
                if not messages:
                    raise RuntimeError("Sent message not yet available")
                context = (int(messages[0]["conversation_message_id"]), job["context"])
            await self.store.finish(job, context)
        elif job["method"] == "edit":
            await self.fault("vk-edit")
            await call_vk(self.vk.messages.edit, **params)
            await self.fault("vk-edit-after")
            context = (int(params["conversation_message_id"]), job["context"]) if job.get("context") else None
            await self.store.finish(job, context)
        else:
            raise ValueError("Unsupported outbox method")

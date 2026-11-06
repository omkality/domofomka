"""Ограниченные измерения текущей попытки; никаких фоновых потоков или сетевых отправок."""

import functools
import inspect
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telemetry_enabled: bool = False
    telemetry_max_spans: int = Field(default=128, ge=8, le=512)
    telemetry_max_packet_bytes: int = Field(default=65536, ge=4096, le=262144)
    telemetry_query_max_chars: int = Field(default=2000, ge=100, le=4000)
    telemetry_retention_days: int = Field(default=30, ge=1, le=365)
    telemetry_max_storage_bytes: int = Field(default=33554432, ge=1048576)
    telemetry_db_timeout_ms: int = Field(default=500, ge=50, le=2000)
    app_revision: str = "unknown"
    catalog_revision: str = "unknown"


settings = TelemetrySettings()
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"
_capture: ContextVar[Any] = ContextVar("observation_capture", default=None)
_parent: ContextVar[str | None] = ContextVar("observation_parent", default=None)
_attributes: ContextVar[Any] = ContextVar("observation_attributes", default=None)


class Capture:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.rows: list[dict] = []
        self.notes: dict = {}
        self.dropped = 0
        self.size = 0
        self.lock = threading.Lock()

    def __enter__(self) -> "Capture":
        """Начать изолированную попытку измерения."""
        self.token = _capture.set(self if self.enabled else None)
        self.parent_token = _parent.set(None)
        self.attr_token = _attributes.set(None)
        return self

    def __exit__(self, *args: Any) -> None:
        """Восстановить контекст вызывающего обработчика."""
        _parent.reset(self.parent_token)
        _attributes.reset(self.attr_token)
        _capture.reset(self.token)

    def add(self, row: dict) -> None:
        try:
            size = len(json.dumps(row, ensure_ascii=False).encode())
        except (TypeError, ValueError):
            self.dropped += 1
            return
        with self.lock:
            if len(self.rows) >= settings.telemetry_max_spans or self.size + size > settings.telemetry_max_packet_bytes:
                self.dropped += 1
                return
            self.rows.append(row)
            self.size += size

    def packet(self) -> dict:
        # Notes are explicit, bounded summaries, never an arbitrary response body.
        packet = {"schema_version": 1, "spans": list(self.rows), "notes": dict(self.notes), "dropped": self.dropped}
        while len(json.dumps(packet, ensure_ascii=False).encode()) > settings.telemetry_max_packet_bytes:
            if packet["spans"]:
                packet["spans"].pop(0)
            elif packet["notes"]:
                packet["notes"].pop(next(iter(packet["notes"])))
            else:
                break
            packet["dropped"] += 1
        return packet


def active() -> bool:
    return _capture.get() is not None


def note(**values: Any) -> None:
    capture = _capture.get()
    if capture is not None:
        for key, value in values.items():
            try:
                size = len(json.dumps(value, ensure_ascii=False).encode())
            except (TypeError, ValueError):
                capture.dropped += 1
                continue
            if size <= 4096 and (key in capture.notes or len(capture.notes) < 32):
                capture.notes[key] = value
            else:
                capture.dropped += 1


def amend(**values: Any) -> None:
    attributes = _attributes.get()
    if attributes is not None:
        attributes.update(values)


@contextmanager
def span(stage: str, *, kind: str = "internal", **attributes: Any) -> Iterator[dict]:
    capture = _capture.get()
    if capture is None:
        yield {}
        return
    row = {
        "id": str(uuid.uuid4()),
        "parent_id": _parent.get(),
        "stage": stage,
        "kind": kind,
        "instance_id": INSTANCE_ID,
        "started_at": time.time(),
        "attributes": attributes,
        "status": "success",
    }
    token = _parent.set(row["id"])
    attr_token = _attributes.set(attributes)
    started = time.perf_counter()
    try:
        yield attributes
    except BaseException as exc:
        row["status"] = "error" if isinstance(exc, Exception) else "interrupted"
        row["error_type"] = type(exc).__name__
        code = getattr(exc, "code", None)
        if isinstance(code, (int, str)):
            row["error_code"] = str(code)[:40]
        raise
    finally:
        if row["status"] == "success" and attributes.get("provider_error_code"):
            row.update(status="error", error_type="ProviderError", error_code=attributes["provider_error_code"])
        elif row["status"] == "success" and attributes.get("http_status", 0) >= 400:
            row.update(status="error", error_type="HTTPStatus", error_code=str(attributes["http_status"]))
        row["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if (
            row["status"] == "error"
            and attributes.get("operation") in {"messages.send", "messages.edit"}
            and row.get("error_type") in {"ReadTimeout", "Timeout", "ConnectionError"}
        ):
            attributes["delivery_confirmation"] = "unknown"
        _parent.reset(token)
        _attributes.reset(attr_token)
        capture.add(row)


def measured(stage: str) -> Callable:
    def decorate(function: Callable) -> Callable:
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def asynchronous(*args: Any, **kwargs: Any) -> Any:
                with span(stage):
                    return await function(*args, **kwargs)

            return asynchronous

        @functools.wraps(function)
        def synchronous(*args: Any, **kwargs: Any) -> Any:
            with span(stage):
                return function(*args, **kwargs)

        return synchronous

    return decorate


def search_result(result: dict) -> dict:
    """Снимок выдачи без кодов, координат, токенов и произвольных полей ответа."""
    value = {
        key: result[key]
        for key in (
            "status",
            "engine",
            "address",
            "building_id",
            "identity",
            "fallback_reason",
            "fallback_rejected",
            "candidate_count",
            "requested_entrance",
        )
        if key in result
    }
    value["candidates"] = [
        {key: row[key] for key in ("building_id", "address") if key in row}
        for row in result.get("candidates", [])[:100]
    ]
    return value


def merge(packet: dict) -> None:
    capture = _capture.get()
    if capture is None:
        return
    if (
        not isinstance(packet, dict)
        or not isinstance(packet.get("spans"), list)
        or not isinstance(packet.get("notes"), dict)
    ):
        note(api_measurements="invalid_packet")
        return
    encoded = json.dumps(packet, ensure_ascii=False)
    if len(encoded.encode()) > settings.telemetry_max_packet_bytes + 8192 or packet.get("schema_version") != 1:
        note(api_measurements="omitted_or_unknown_schema")
        return
    for incoming in packet.get("spans", [])[: settings.telemetry_max_spans]:
        row = dict(incoming, service="api")
        if row.get("parent_id") is None:
            row["parent_id"] = _parent.get()
        capture.add(row)
    note(**packet.get("notes", {}))
    capture.dropped += packet.get("dropped", 0)


def current_packet() -> dict | None:
    capture = _capture.get()
    return capture.packet() if capture else None

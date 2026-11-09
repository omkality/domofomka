"""Ограниченный кэш результатов поиска; сохранность запросов обеспечивает очередь."""

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable


class LookupCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.entries: OrderedDict[Hashable, tuple[float, bytes, int]] = OrderedDict()
        self.limits = (0.0, 1, 1024)
        self.used_bytes = 0
        self.hits = 0

    def configure(self, ttl: float, maximum_entries: int, maximum_bytes: int) -> None:
        with self.lock:
            limits = (ttl, maximum_entries, maximum_bytes)
            if limits != self.limits:
                self.entries.clear()
                self.used_bytes = 0
                self.limits = limits

    def get(self, key: Hashable) -> dict | None:
        with self.lock:
            entry = self.entries.pop(key, None)
            if entry is None:
                return None
            expires, data, size = entry
            if expires <= self.clock():
                self.used_bytes -= size
                return None
            self.entries[key] = entry
            self.hits += 1
        return json.loads(data)

    def put(self, key: Hashable, result: dict) -> None:
        data = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        size = len(data) + len(repr(key).encode())
        with self.lock:
            ttl, maximum_entries, maximum_bytes = self.limits
            if ttl <= 0 or size > maximum_bytes:
                return
            old = self.entries.pop(key, None)
            if old:
                self.used_bytes -= old[2]
            while self.entries and (len(self.entries) >= maximum_entries or self.used_bytes + size > maximum_bytes):
                self.used_bytes -= self.entries.popitem(last=False)[1][2]
            self.entries[key] = (self.clock() + ttl, data, size)
            self.used_bytes += size

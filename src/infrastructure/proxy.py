"""Process-local route health; one probe at a time, no background traffic."""

import threading
import time

from src.infrastructure.network import network_settings


class ProxyHealth:
    """Состояние выбора прокси и его cooldown в текущем процессе."""

    def __init__(self):
        """Создать потокобезопасное состояние маршрутов без фоновых проверок."""
        self.lock = threading.Lock()
        self.routes: dict[tuple[str, str], dict[str, float]] = {}

    def choose(self, service: str, proxy: str) -> bool:
        """Выбрать доступный маршрут с учётом cooldown прокси."""
        return self.decision(service, proxy)[0]

    def decision(self, service: str, proxy: str) -> tuple[bool, str]:
        """Вернуть выбранный маршрут и причину решения."""
        with self.lock:
            state = self.routes.get((service, proxy))
            if state is None:
                return True, "preferred"
            now = time.monotonic()
            if now < state["until"] or now < state["probe_until"]:
                return False, "proxy_cooldown_or_probe_busy"
            state["probe_until"] = now + network_settings.proxy_probe_lease_seconds
            return True, "proxy_probe"

    def failed(self, service: str, proxy: str) -> None:
        """Отметить отказ подключения и назначить следующую пробу прокси."""
        with self.lock:
            state = self.routes.get((service, proxy), {})
            delay = min(
                (
                    state["delay"] * network_settings.proxy_retry_multiplier
                    if "delay" in state
                    else network_settings.proxy_retry_initial_seconds
                ),
                network_settings.proxy_retry_max_seconds,
            )
            self.routes[service, proxy] = {"delay": delay, "until": time.monotonic() + delay, "probe_until": 0}

    def succeeded(self, service: str, proxy: str) -> None:
        """Вернуть исправный прокси в обычное обслуживание запросов."""
        with self.lock:
            self.routes.pop((service, proxy), None)


proxy_health = ProxyHealth()

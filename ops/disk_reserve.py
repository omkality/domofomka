"""Аварийный резерв корневой ФС; только стандартная очистка и остановка бота.

Не удаляет БД, образы Docker, VPN-конфигурации или произвольные временные файлы.
Оповещения наружу не отправляет. Не заменяет квоты и лимиты производителей данных.
"""

import json
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

HOME = Path("/var/lib/domofomka-storage")
RESERVE = int(os.environ.get("DISK_RESERVE_BYTES", "268435456"))
LOW = int(os.environ.get("DISK_LOW_BYTES", "536870912"))
CRITICAL = int(os.environ.get("DISK_CRITICAL_BYTES", "134217728"))
REARM = int(os.environ.get("DISK_REARM_BYTES", "1342177280"))


def safe_cleanup() -> None:
    """Выполнить только штатную очистку журналов и кеша пакетов с таймаутами."""
    for command in (
        ["apt-get", "clean"],
        ["journalctl", "--rotate"],
        ["journalctl", "--vacuum-size=32M"],
        ["systemctl", "start", "logrotate.service"],
    ):
        with suppress(subprocess.TimeoutExpired):
            subprocess.run(command, timeout=30, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reserve_action(free: int, exists: bool) -> str:
    """Выбрать освобождение или восстановление дискового резерва с гистерезисом."""
    if free < LOW:
        return "release_and_clean"
    if not exists and free >= REARM:
        return "rearm"
    return "keep"


def stop_application() -> list[str]:
    """Аварийно остановить только известные контейнеры domofomka, сохранив VPN."""
    if not shutil.which("docker"):
        return []
    names = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True).splitlines()
    allowed = {
        f"{project}-{service}-1"
        for project in ("domofomka", "domofomka-dev")
        for service in ("api", "vkbot", "queue", "redis")
    }
    stopped = [
        f"{project}-{service}-1"
        for project in ("domofomka", "domofomka-dev")
        for service in ("vkbot", "api", "queue", "redis")
        if f"{project}-{service}-1" in allowed and f"{project}-{service}-1" in names
    ]
    for name in stopped:
        # Аварийно останавливаем только принадлежащие проекту записи на диск.
        subprocess.run(
            ["docker", "stop", "--time", "20", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    return stopped


def main() -> None:
    """Проверить место, освободить резерв и при критическом дефиците остановить только domofomka."""
    import fcntl

    if not 67108864 <= CRITICAL < LOW < REARM or not 0 < RESERVE <= LOW:
        raise ValueError("Invalid disk safety thresholds")
    os.umask(0o077)
    HOME.mkdir(exist_ok=True)
    with (HOME / "guard.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        reserve = HOME / "emergency.reserve"
        free = shutil.disk_usage("/").free
        action = reserve_action(free, reserve.exists())
        if action == "release_and_clean":
            reserve.unlink(missing_ok=True)
            safe_cleanup()
        elif action == "rearm":
            with reserve.open("xb") as file:
                try:
                    os.posix_fallocate(file.fileno(), 0, RESERVE)
                except OSError:
                    reserve.unlink(missing_ok=True)
                    raise
        free = shutil.disk_usage("/").free
        stopped = stop_application() if free < CRITICAL else []
        if action != "keep" or stopped:
            # Локальный журнал; контейнеры сами не запускаем повторно при сохраняющейся причине.
            print(json.dumps({"disk_action": action, "free_bytes": free, "stopped_application": stopped}))  # noqa: T201


if __name__ == "__main__":
    main()

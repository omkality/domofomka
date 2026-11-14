# ruff: noqa: UP017
"""Штатные ограничения хранения Ubuntu. Не перезапускает Docker, SSH или VPN.

Применять только из опубликованного коммита после проверки. Предыдущие конфиги
сохраняются в закрытой /var/backups/domofomka-storage. По умолчанию только аудит.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def configuration_files(config: Path) -> list[dict]:
    """Прочитать проверенный перечень штатных конфигураций без генерации их синтаксиса."""
    files = json.loads((config / "manifest.json").read_text(encoding="utf-8"))
    for entry in files:
        source = (config / entry["source"]).resolve()
        source.relative_to(config.parent.resolve())
        target = PurePosixPath(entry["target"])
        if not source.is_file() or not target.is_absolute() or ".." in target.parts:
            raise ValueError("Invalid host configuration manifest")
        if entry.get("when") not in {None, "exists", "missing"}:
            raise ValueError("Invalid host configuration condition")
    return files


def audit() -> dict:
    """Прочитать свободное место и состояние штатной ротации без изменений хоста."""
    disk = shutil.disk_usage("/")
    return {
        "total_bytes": disk.total,
        "free_bytes": disk.free,
        "journal": subprocess.check_output(["journalctl", "--disk-usage"], text=True).strip(),
        "logrotate_enabled": subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "logrotate.timer"], check=False
        ).returncode
        == 0,
    }


def apply() -> None:
    """Установить проверенные конфиги хранения с резервированием прежних файлов."""
    if os.geteuid() != 0:
        raise PermissionError("Root required")
    os.umask(0o077)
    backup = Path("/var/backups/domofomka-storage") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True)
    changes = {}

    def write(name: str, content: str) -> None:
        """Сохранить прежний конфиг и заменить его только при изменившемся содержимом."""
        path = Path(name)
        if path.exists() and path.read_text() == content:
            return
        if path.is_symlink():
            raise ValueError("Refusing configuration symlink")
        changes[name] = path.exists()
        if path.exists():
            saved = backup / name.lstrip("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    config = Path(__file__).with_name("config")
    for entry in configuration_files(config):
        target = Path(entry["target"])
        if entry.get("when") == "exists" and not target.exists():
            continue
        if entry.get("when") == "missing" and target.exists():
            continue
        write(str(target), (config / entry["source"]).read_text(encoding="utf-8"))
    try:
        subprocess.run(
            ["logrotate", "--debug", "/etc/logrotate.conf"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        calendar = next(
            line.split("=", 1)[1]
            for line in (config / "logrotate-timer.conf").read_text().splitlines()
            if line.startswith("OnCalendar=") and line.split("=", 1)[1]
        )
        subprocess.run(["systemd-analyze", "calendar", calendar], check=True, stdout=subprocess.DEVNULL)
    except BaseException:
        for name, existed in changes.items():
            if existed:
                shutil.copy2(backup / name.lstrip("/"), name)
            else:
                Path(name).unlink()
        raise
    (backup / "manifest.json").write_text(json.dumps(changes))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "systemd-journald"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "logrotate.timer", "systemd-tmpfiles-clean.timer"], check=True)
    subprocess.run(["systemctl", "restart", "logrotate.timer"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "domofomka-disk-reserve.timer"], check=True)
    subprocess.run(["journalctl", "--rotate"], check=True)
    subprocess.run(
        ["journalctl", "--vacuum-size=64M", "--vacuum-time=7d"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["sysctl", "-p", "/etc/sysctl.d/99-storage-no-core.conf"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["apt-get", "clean"], check=True)
    # Запуск через systemd сериализуется с таймером и не конкурирует за state lock.
    subprocess.run(["systemctl", "start", "logrotate.service"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["systemctl", "start", "domofomka-disk-reserve.service"], check=True)
    # Три небольших набора прежних конфигураций; никакие данные приложений здесь не удаляются.
    copies = sorted(backup.parent.iterdir())
    for path in copies[:-3]:
        if path.is_dir() and path.parent == backup.parent and (path / "manifest.json").is_file():
            shutil.rmtree(path)
    sys.stdout.write(json.dumps({"applied": True, "backup": str(backup), **audit()}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    else:
        sys.stdout.write(json.dumps(audit()) + "\n")

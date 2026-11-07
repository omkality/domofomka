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
from pathlib import Path

JOURNAL = """[Journal]
SystemMaxUse=64M
SystemMaxFileSize=8M
SystemKeepFree=512M
RuntimeMaxUse=16M
RuntimeMaxFileSize=4M
RuntimeKeepFree=32M
MaxRetentionSec=7day
Compress=yes
"""
TIMER = """[Timer]
OnCalendar=
OnCalendar=*-*-* *:0/5:00
RandomizedDelaySec=30s
AccuracySec=30s
Persistent=true
"""


def log_rule(paths: list[str], size: str, copies: int, create: str = "") -> str:
    return (
        "\n".join(paths)
        + " {\n    daily\n    maxsize "
        + size
        + "\n    rotate "
        + str(copies)
        + (
            "\n    maxage 7\n    missingok\n    notifempty\n    compress\n    "
            + ("create " + create if create else "copytruncate")
            + "\n}\n"
        )
    )


def audit() -> dict:
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
    if os.geteuid() != 0:
        raise PermissionError("Root required")
    os.umask(0o077)
    backup = Path("/var/backups/domofomka-storage") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True)
    changes = {}

    def write(name: str, content: str) -> None:
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

    write("/etc/systemd/journald.conf.d/99-storage-budget.conf", JOURNAL)
    write("/etc/systemd/system/logrotate.timer.d/zz-storage-budget.conf", TIMER)
    write("/etc/logrotate.d/btmp", log_rule(["/var/log/btmp"], "2M", 7, "0660 root utmp"))
    write("/etc/logrotate.d/wtmp", log_rule(["/var/log/wtmp"], "1M", 2, "0664 root utmp"))
    if Path("/etc/logrotate.d/rsyslog").exists():
        write(
            "/etc/logrotate.d/rsyslog",
            log_rule(
                ["/var/log/" + x for x in ["syslog", "mail.log", "kern.log", "auth.log", "user.log", "cron.log"]],
                "2M",
                7,
            ),
        )
    if Path("/etc/logrotate.d/tinyproxy").exists():
        write("/etc/logrotate.d/tinyproxy", log_rule(["/var/log/tinyproxy/tinyproxy.log"], "1M", 4))
    write("/etc/apt/apt.conf.d/99-cache-budget", 'APT::Periodic::MaxAge "7";\nAPT::Periodic::MaxSize "64";\n')
    write("/etc/sysctl.d/99-storage-no-core.conf", "kernel.core_pattern = |/bin/false\nfs.suid_dumpable = 0\n")
    guard = Path(__file__).with_name("disk_reserve.py")
    write("/usr/local/libexec/domofomka-disk-reserve.py", guard.read_text())
    if not Path("/etc/domofomka-storage.env").exists():
        write(
            "/etc/domofomka-storage.env",
            "DISK_RESERVE_BYTES=268435456\nDISK_LOW_BYTES=536870912\n"
            "DISK_CRITICAL_BYTES=134217728\nDISK_REARM_BYTES=1342177280\n",
        )
    write(
        "/etc/systemd/system/domofomka-disk-reserve.service",
        """[Unit]
Description=Release emergency disk reserve and protect host availability
After=local-fs.target
[Service]
Type=oneshot
EnvironmentFile=-/etc/domofomka-storage.env
ExecStart=/usr/bin/python3 /usr/local/libexec/domofomka-disk-reserve.py
Nice=19
IOSchedulingClass=idle
CPUQuota=10%
MemoryMax=64M
TimeoutStartSec=180
""",
    )
    write(
        "/etc/systemd/system/domofomka-disk-reserve.timer",
        """[Unit]
Description=Check emergency disk reserve every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
""",
    )
    try:
        subprocess.run(
            ["logrotate", "--debug", "/etc/logrotate.conf"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["systemd-analyze", "calendar", "*-*-* *:0/5:00"], check=True, stdout=subprocess.DEVNULL)
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

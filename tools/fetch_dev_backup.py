"""Забрать свежую готовую копию dev по SSH без токенов VK и сторонних библиотек.

Подходит для запуска планировщиком на машине вне VDS. Каталог назначения приватный.
Копия принимается только после проверки заголовка PostgreSQL, размера и SHA-256.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REMOTE = """
import hashlib,json,pathlib,sys
paths=list(pathlib.Path('/root/domofomka-dev/backups/queue').glob('queue-*.dump'))
path=max(paths,key=lambda p:p.stat().st_mtime)
with path.open('rb') as source:
    digest=hashlib.sha256()
    for block in iter(lambda:source.read(1048576),b''):digest.update(block)
    size=source.tell();source.seek(0)
    sys.stdout.buffer.write((json.dumps({'name':path.name,'bytes':size,'sha256':digest.hexdigest()})+'\\n').encode())
    for block in iter(lambda:source.read(1048576),b''):sys.stdout.buffer.write(block)
"""


def receive(stream, output: Path, maximum: int = 67108864):
    header = json.loads(stream.readline(2048))
    if not re.fullmatch(r"queue-[0-9TZ]+-[0-9]+\.dump", header["name"]):
        raise ValueError("Invalid backup name")
    if not 5 <= header["bytes"] <= maximum or not re.fullmatch(r"[0-9a-f]{64}", header["sha256"]):
        raise ValueError("Invalid backup size or digest")
    output.mkdir(parents=True, exist_ok=True)
    target = output / header["name"]
    temporary = None
    digest = hashlib.sha256()
    count = 0
    try:
        with tempfile.NamedTemporaryFile(dir=output, prefix=target.name + ".", suffix=".part", delete=False) as file:
            temporary = Path(file.name)
            while count < header["bytes"]:
                block = stream.read(min(1048576, header["bytes"] - count))
                if not block:
                    raise ValueError("Truncated backup")
                if count == 0 and not block.startswith(b"PGDMP"):
                    raise ValueError("Not a PostgreSQL custom dump")
                file.write(block)
                digest.update(block)
                count += len(block)
        if digest.hexdigest() != header["sha256"] or stream.read(1):
            raise ValueError("Backup integrity check failed")
        temporary.replace(target)
        return {**header, "path": str(target), "verified": True}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prune(output: Path, daily: int = 7, weekly: int = 4) -> int:
    if not 1 <= daily <= 31 or not 1 <= weekly <= 12:
        raise ValueError("Invalid local retention")
    files = []
    for path in output.glob("queue-*.dump"):
        match = re.fullmatch(r"queue-(\d{8}T\d{6}Z)-\d+\.dump", path.name)
        if match and not path.is_symlink():
            files.append((datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), path))
    dates, weeks, keep = set(), set(), set()
    for stamp, path in sorted(files, reverse=True):
        week = stamp.isocalendar()[:2]
        if (stamp.date() not in dates and len(dates) < daily) or (week not in weeks and len(weeks) < weekly):
            keep.add(path)
        dates.add(stamp.date())
        weeks.add(week)
    removed = 0
    for _, path in files:
        if path not in keep:
            path.unlink()
            removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--known-hosts", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/private/backups/offsite"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.host):
        raise ValueError("Invalid SSH hostname")
    import base64

    command = (
        "python3 -c \"import base64;exec(base64.b64decode('" + base64.b64encode(REMOTE.encode()).decode() + "'))\""
    )
    ssh = [
        "ssh",
        "-i",
        str(args.key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(args.known_hosts),
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "root@" + args.host,
        command,
    ]
    with subprocess.Popen(ssh, stdout=subprocess.PIPE) as process:
        try:
            result = receive(process.stdout, args.output)
            if process.wait(timeout=30):
                raise RuntimeError("SSH backup transfer failed")
        except BaseException:
            process.kill()
            raise
    result["old_copies_removed"] = prune(args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

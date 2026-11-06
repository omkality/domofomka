"""Р—Р°Р±СЂР°С‚СЊ СЃРІРµР¶СѓСЋ РіРѕС‚РѕРІСѓСЋ РєРѕРїРёСЋ dev РїРѕ SSH Р±РµР· С‚РѕРєРµРЅРѕРІ VK Рё СЃС‚РѕСЂРѕРЅРЅРёС… Р±РёР±Р»РёРѕС‚РµРє.

РџРѕРґС…РѕРґРёС‚ РґР»СЏ Р·Р°РїСѓСЃРєР° РїР»Р°РЅРёСЂРѕРІС‰РёРєРѕРј РЅР° РјР°С€РёРЅРµ РІРЅРµ VDS. РљР°С‚Р°Р»РѕРі РЅР°Р·РЅР°С‡РµРЅРёСЏ РїСЂРёРІР°С‚РЅС‹Р№.
РљРѕРїРёСЏ РїСЂРёРЅРёРјР°РµС‚СЃСЏ С‚РѕР»СЊРєРѕ РїРѕСЃР»Рµ РїСЂРѕРІРµСЂРєРё Р·Р°РіРѕР»РѕРІРєР° PostgreSQL, СЂР°Р·РјРµСЂР° Рё SHA-256.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
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
    print(json.dumps(result))


if __name__ == "__main__":
    main()

"""Ограниченная команда отдельного SSH-ключа Actions. Не предоставляет shell/SFTP.

Устанавливается bootstrap_ci.py в закрытый каталог вне получаемых релизов.
stdin: строка JSON с краткоживущим токеном GHCR, затем tar (максимум 2 МиБ).
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

FILES = {"compose.deploy.yml", "ops/deploy.py", "release.json"}


def read_url(url: str) -> bytes:
    """Прочитать проверяемый исходный артефакт по URL с ограничением размера."""
    with urlopen(Request(url, headers={"User-Agent": "domofomka-deploy"}), timeout=20) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError("Source file too large")
    return data


def verify_source(release: Path, target: str) -> None:
    """SSH-ключ сам по себе не разрешает исполнение произвольного загруженного кода."""
    manifest = json.loads((release / "release.json").read_text())
    branch = "dev" if target == "dev" else "master"
    ref = json.loads(read_url("https://api.github.com/repos/omkality/domofomka/git/ref/heads/" + branch))
    revision = manifest["revision"]
    if ref["object"]["sha"] != revision:
        raise ValueError("Superseded branch commit")
    for name in sorted(FILES - {"release.json"}):
        expected = read_url(f"https://raw.githubusercontent.com/omkality/domofomka/{revision}/{name}")
        if expected != (release / name).read_bytes():
            raise ValueError("Deployment code differs from the published commit")


def unpack(payload: bytes, destination: Path) -> None:
    """Принять только разрешённые небольшие файлы архива выпуска."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        members = archive.getmembers()
        if {m.name for m in members} != FILES or len(members) != len(FILES):
            raise ValueError("Unexpected release files")
        for member in members:
            if not member.isfile() or member.size > 1048576:
                raise ValueError("Only small regular release files allowed")
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(member).read())


def main() -> None:
    """Ограниченная команда отдельного SSH-ключа Actions. Не предоставляет shell/SFTP."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--environment", choices=["dev", "prod"], required=True)
    args = parser.parse_args()
    home = args.root / ".ci"
    action = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if action in {"rollback", "rollback-legacy"}:
        if action == "rollback-legacy" and args.environment != "prod":
            raise ValueError("Legacy rollback is only allowed on prod")
        state = json.loads((home / "state.json").read_text())
        script = Path(state["current"]) / "ops/deploy.py"
        subprocess.run(
            [
                "python3",
                str(script),
                "rollback",
                "--root",
                str(args.root),
                "--environment",
                args.environment,
                *(["--legacy-v1"] if action == "rollback-legacy" else []),
            ],
            check=True,
        )
        return
    if action != "deploy":
        raise ValueError("Only deploy and rollback commands permitted")
    header = json.loads(sys.stdin.buffer.readline(8192))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", header.get("actor", "")):
        raise ValueError("Invalid registry user")
    payload = sys.stdin.buffer.read(2097153)
    if len(payload) > 2097152:
        raise ValueError("Release too large")
    with tempfile.TemporaryDirectory(dir=home, prefix="incoming-") as incoming:
        unpack(payload, Path(incoming))
        manifest = json.loads((Path(incoming) / "release.json").read_text())
        revision = manifest.get("revision", "")
        if manifest.get("environment") != args.environment or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid target or commit")
        verify_source(Path(incoming), args.environment)
        # Каждый повтор имеет свой каталог; активные файлы не перезаписываются.
        release = home / "releases" / (revision + "-" + Path(incoming).name)
        Path(incoming).rename(release)
    with tempfile.TemporaryDirectory(dir=home, prefix="registry-") as config:
        env = dict(os.environ, DOCKER_CONFIG=config)
        subprocess.run(
            ["docker", "login", "ghcr.io", "--username", header["actor"], "--password-stdin"],
            input=header["token"].encode(),
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        header.clear()
        subprocess.run(
            [
                "python3",
                str(release / "ops/deploy.py"),
                "deploy",
                "--root",
                str(args.root),
                "--environment",
                args.environment,
                "--release",
                str(release),
            ],
            env=env,
            check=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Ни заголовок, ни окружение, ни секреты реестра в логи не попадают.
        sys.stderr.write(f"Release rejected or failed: {type(error).__name__}\n")
        sys.exit(1)

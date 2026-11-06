"""Развёртывание двух приложений; PostgreSQL, туннели и данные не изменяются.

Python 3.10+ на хосте, без сторонних зависимостей. Вызывается SSH-приёмником.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}")
IMAGES = {
    "api": re.compile(r"ghcr\.io/omkality/domofomka-api@sha256:[0-9a-f]{64}"),
    "bot": re.compile(r"ghcr\.io/omkality/domofomka-vkbot@sha256:[0-9a-f]{64}"),
}
TARGETS = {
    "dev": ("domofomka-dev", ".env.dev", "223949627", "18000"),
    "prod": ("domofomka", ".env", "213361259", "8000"),
}


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, **kwargs)


def validate_manifest(value: dict, target: str) -> dict:
    if value.get("environment") != target or not SHA.fullmatch(value.get("revision", "")):
        raise ValueError("Invalid release environment or commit")
    for service, pattern in IMAGES.items():
        if not pattern.fullmatch(value.get(service, "")):
            raise ValueError("Immutable image digest required")
        size = value.get(service + "_bytes", 0)
        if not isinstance(size, int) or not 0 < size <= 1073741824:
            raise ValueError("Image size must be bounded")
    return value


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def settings(path: Path) -> dict:
    # Compose самостоятельно читает секреты и подстановки .env. Здесь только числовые лимиты.
    return dict(
        line.strip().split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )


class Deployment:
    def __init__(self, root: Path, target: str):
        self.root = root.resolve()
        self.target = target
        self.project, env_name, self.group, port = TARGETS[target]
        self.env_file = self.root / env_name
        self.config = settings(self.env_file)
        self.port = self.config.get("API_PUBLISH_PORT", port)
        self.home = self.root / ".ci"
        self.state_path = self.home / "state.json"
        self.timeout = int(self.config.get("DEPLOY_HEALTH_TIMEOUT_SECONDS", "120"))
        self.reserve = int(self.config.get("DEPLOY_DISK_RESERVE_BYTES", "268435456"))
        if not 10 <= self.timeout <= 600 or self.reserve < 67108864:
            raise ValueError("Invalid deploy limits")

    def compose(self, release: Path, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        manifest = json.loads((Path(release) / "release.json").read_text())
        env = dict(
            os.environ,
            APP_ROOT=str(self.root),
            DEPLOY_ENV_FILE=str(self.env_file),
            API_IMAGE=manifest["api"],
            BOT_IMAGE=manifest["bot"],
            APP_REVISION=manifest["revision"],
            DEPLOY_GROUP_ID=self.group,
            API_PUBLISH_PORT=self.port,
        )
        return run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "--env-file",
                str(self.env_file),
                "-f",
                str(Path(release) / "compose.deploy.yml"),
                *args,
            ],
            env=env,
            **kwargs,
        )

    def inspect(self, service: str) -> dict:
        name = f"{self.project}-{service}-1"
        return json.loads(run(["docker", "inspect", name], capture_output=True).stdout)[0]

    def verify_environment(self) -> None:
        if self.config.get("VK_GROUP_ID") != self.group or self.config.get("EXPECTED_VK_GROUP_ID") != self.group:
            raise ValueError("Wrong VK community in server environment")
        for file in (self.root / "codes.db", self.root / "artifacts/search/legacy-index.db"):
            if not file.is_file():
                raise ValueError("Required read-only search data missing")
        queue = self.inspect("queue")
        if queue["State"].get("Health", {}).get("Status") != "healthy":
            raise ValueError("Existing PostgreSQL must be healthy; database bootstrap is separate")

    def free_space(self, extra: int = 0) -> None:
        if shutil.disk_usage(self.root).free < self.reserve + extra:
            raise RuntimeError("Insufficient free disk; running release unchanged")

    def check(self, release: Path) -> None:
        # Выполняется внутри готового API, без зависимости хоста от Python-пакетов.
        # Прямая проверка каталога выявляет ошибку, которую fallback v1 мог бы скрыть.
        probe = """
import http.client,json,os
from src.api.config import settings
from src.api.v2 import get_catalog
c=http.client.HTTPConnection('127.0.0.1',int(os.environ.get('API_PORT','8000')),timeout=10)
c.request('GET','/version'); r=c.getresponse(); assert r.status==200; assert json.loads(r.read())['name']=='domofomka'
if settings.search_backend=='postgres':
    catalog=get_catalog()
    assert catalog.resolve('мичурина 1а')['status']=='found'
"""
        self.compose(
            release,
            "exec",
            "-T",
            "api",
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            probe,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Проверяем действующее соединение runtime/Long Poll, не создавая сообщения в VK.
        probe_bot = """
from src.vkbot.config import settings
from src.vkbot.runtime import Runtime
r=Runtime(settings)
with r.store.connect() as db:
    assert db.execute('SELECT NOT pg_try_advisory_lock(20261021,%s) AS active',(r.store.group_id,)).fetchone()['active']
"""
        self.compose(
            release,
            "exec",
            "-T",
            "vkbot",
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            probe_bot,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def activate(self, release: Path) -> None:
        self.compose(release, "stop", "-t", "30", "vkbot")
        self.compose(release, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "api", "vkbot")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            api, bot = self.inspect("api"), self.inspect("vkbot")
            if any(item["State"].get("OOMKilled") for item in (api, bot)):
                raise RuntimeError("Container OOM during deployment")
            if (
                api["State"].get("Health", {}).get("Status") == "healthy"
                and bot["State"].get("Running")
                and bot["RestartCount"] == 0
            ):
                try:
                    self.check(release)
                    return
                except subprocess.CalledProcessError:
                    pass
            time.sleep(3)
        raise RuntimeError("Release failed readiness check")

    def deploy(self, release: Path) -> None:
        previous = json.loads(self.state_path.read_text())
        manifest = validate_manifest(json.loads((release / "release.json").read_text()), self.target)
        self.verify_environment()
        self.free_space()
        for service in ("api", "bot"):
            cached = subprocess.run(
                ["docker", "image", "inspect", manifest[service]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            # Запас на скачивание и распаковку одного нового образа сверх резерва БД.
            if cached.returncode != 0:
                self.free_space(2 * manifest[service + "_bytes"])
            run(["docker", "pull", "--quiet", manifest[service]])
            label = run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                    manifest[service],
                ],
                capture_output=True,
            ).stdout.strip()
            if label != manifest["revision"]:
                raise ValueError("Image revision differs from release commit")
            self.free_space()
        # Схема очереди должна оставаться обратно совместимой. Автомиграций/восстановления БД здесь нет.
        backup = self.home / "queue-before-deploy.dump"
        with backup.with_suffix(".part").open("wb") as output:
            subprocess.run(
                ["docker", "exec", f"{self.project}-queue-1", "pg_dump", "-U", "domofomka", "-d", "domofomka", "-Fc"],
                check=True,
                stdout=output,
            )
        backup.with_suffix(".part").replace(backup)
        self.free_space()
        journal = self.home / "transaction.json"
        write_json(journal, {"previous": previous["current"], "candidate": str(release)})
        try:
            self.activate(release)
        except BaseException:
            sys.stdout.write("Deployment failed; restoring previous application images\n")
            sys.stdout.flush()
            self.activate(Path(previous["current"]))
            journal.unlink()
            raise
        if str(release) != previous["current"]:
            write_json(self.state_path, {"current": str(release), "previous": previous["current"]})
        (self.root / ".deployed-revision").write_text(manifest["revision"] + "\n")
        journal.unlink()
        self.cleanup()
        sys.stdout.write(f"Healthy {self.target} release {manifest['revision']}\n")

    def rollback(self) -> None:
        state = json.loads(self.state_path.read_text())
        if not state.get("previous"):
            raise ValueError("No previous release")
        self.verify_environment()
        try:
            self.activate(Path(state["previous"]))
        except BaseException:
            self.activate(Path(state["current"]))
            raise
        write_json(self.state_path, {"current": state["previous"], "previous": state["current"]})
        manifest = json.loads((Path(state["previous"]) / "release.json").read_text())
        (self.root / ".deployed-revision").write_text(manifest["revision"] + "\n")
        sys.stdout.write(f"Rolled back {self.target} to {manifest['revision']}\n")

    def cleanup(self) -> None:
        state = json.loads(self.state_path.read_text())
        keep = {state["current"], state.get("previous")}
        protected = {
            json.loads((Path(path) / "release.json").read_text())[s] for path in keep if path for s in ("api", "bot")
        }
        for path in (self.home / "releases").iterdir():
            if str(path) in keep or not path.is_dir():
                continue
            manifest = json.loads((path / "release.json").read_text())
            for service in ("api", "bot"):
                image = manifest[service]
                if image not in protected and IMAGES[service].fullmatch(image):
                    # Только конкретные образы старых релизов приложения. Никаких prune/-f.
                    subprocess.run(
                        ["docker", "image", "rm", image],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            shutil.rmtree(path)


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "rollback"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--environment", choices=TARGETS, required=True)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    deployment = Deployment(args.root, args.environment)

    def interrupted(signum: int, frame: Any) -> None:
        raise RuntimeError("Deployment interrupted")

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    with (deployment.home / "deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = deployment.home / "transaction.json"
        if journal.exists():
            interrupted_release = json.loads(journal.read_text())
            deployment.activate(Path(interrupted_release["previous"]))
            journal.unlink()
        if args.action == "rollback":
            deployment.rollback()
        else:
            deployment.deploy(args.release.resolve())


if __name__ == "__main__":
    main()

"""Развёртывание приложений и проверенных обратимых обновлений каталога dev.

Python 3.10+ на хосте, без сторонних зависимостей. Вызывается SSH-приёмником.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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

    def catalog_revision(self, database: str) -> str:
        return run(
            [
                "docker",
                "exec",
                f"{self.project}-queue-1",
                "psql",
                "-U",
                "domofomka",
                "-d",
                database,
                "-Atc",
                "SELECT value FROM metadata WHERE key='source_catalog_sha256'",
            ],
            capture_output=True,
        ).stdout.strip()

    def catalog_change(self, image: str) -> dict | None:
        info = json.loads(
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:size=16m",
                    "--ulimit",
                    "core=0",
                    "--entrypoint",
                    "uv",
                    image,
                    "run",
                    "--frozen",
                    "python",
                    "-m",
                    "src.search.catalog_update",
                    "inspect",
                ],
                capture_output=True,
            ).stdout
        )
        if not info.get("enabled"):
            return None
        database = urlsplit(self.config.get("CATALOG_DATABASE_URL", "").strip("\"'")).path.lstrip("/")
        if not re.fullmatch(r"domofomka_catalog_[A-Za-z0-9_]+", database):
            raise ValueError("Unexpected catalog database")
        current = self.catalog_revision(database)
        if current == info["target_revision"]:
            return None
        if self.target != "dev" or current != info["source_revision"]:
            raise ValueError("Catalog changes require the reviewed dev base")
        if not re.fullmatch(r"[0-9a-f]{64}", info["patch_sha256"]) or not 0 < info["raw_bytes"] <= 67108864:
            raise ValueError("Invalid catalog release manifest")
        patch = self.home / "catalog-candidates" / (info["patch_sha256"] + ".json.gz")
        if patch.is_symlink() or patch.stat().st_size != info["patch_bytes"]:
            raise ValueError("Missing or unexpected catalog artifact")
        digest = hashlib.sha256()
        with patch.open("rb") as stream:
            for block in iter(lambda: stream.read(1048576), b""):
                digest.update(block)
        if digest.hexdigest() != info["patch_sha256"]:
            raise ValueError("Catalog artifact digest mismatch")
        self.free_space(info["raw_bytes"] * 2 + 134217728)
        return {**info, "image": image, "database": database, "patch": str(patch)}

    def set_catalog_revision(self, revision: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValueError("Invalid catalog revision")
        lines = self.env_file.read_text().splitlines()
        lines = [line for line in lines if line.split("=", 1)[0].strip() != "CATALOG_REVISION"]
        temp = self.env_file.with_suffix(".catalog-env.tmp")
        temp.write_text("\n".join([*lines, "CATALOG_REVISION=" + revision]) + "\n")
        temp.chmod(0o600)
        temp.replace(self.env_file)

    def run_catalog(self, info: dict, reverse: bool = False) -> None:
        if self.target != "dev":
            raise ValueError("Catalog mutation is dev-only")
        self.free_space()
        run(
            ["docker", "stop", "--time", "30", f"{self.project}-vkbot-1", f"{self.project}-api-1"],
            stdout=subprocess.DEVNULL,
        )
        client = self.project + "-catalog-update"
        existing = subprocess.run(["docker", "inspect", client], text=True, capture_output=True, check=False)
        if existing.returncode == 0:
            if json.loads(existing.stdout)[0]["Config"].get("Labels", {}).get("io.domofomka.catalog_update") != "true":
                raise ValueError("Catalog client name already used by another container")
            run(["docker", "rm", "-f", client], stdout=subprocess.DEVNULL)
        environment = dict(item.split("=", 1) for item in self.inspect("queue")["Config"]["Env"] if "=" in item)
        password = quote(environment["POSTGRES_PASSWORD"], safe="")
        with tempfile.TemporaryDirectory(dir=self.home, prefix="catalog-admin-") as directory:
            env_file = Path(directory) / "environment"
            env_file.write_text(
                "APP_ENVIRONMENT=development\nEXPECTED_VK_GROUP_ID=223949627\n"
                f"CATALOG_ADMIN_DSN=postgresql://domofomka:{password}@queue:5432/{info['database']}\n"
            )
            env_file.chmod(0o600)
            try:
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--name",
                        client,
                        "--label",
                        "io.domofomka.catalog_update=true",
                        "--network",
                        self.project + "_default",
                        "--read-only",
                        "--tmpfs",
                        "/tmp:size=16m",
                        "--ulimit",
                        "core=0",
                        "--memory",
                        "384m",
                        "--memory-swap",
                        "512m",
                        "--pids-limit",
                        "64",
                        "--env-file",
                        str(env_file),
                        "--mount",
                        "type=bind,source=" + info["patch"] + ",target=/patch,readonly",
                        "--entrypoint",
                        "uv",
                        info["image"],
                        "run",
                        "--frozen",
                        "python",
                        "-m",
                        "src.search.catalog_update",
                        "reverse" if reverse else "apply",
                        "--patch",
                        "/patch",
                    ],
                    timeout=300,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", client], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
        target = info["source_revision"] if reverse else info["target_revision"]
        if self.catalog_revision(info["database"]) != target:
            raise ValueError("Catalog revision was not published")
        self.set_catalog_revision(target)

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
        catalog = self.catalog_change(manifest["api"])
        # Очередь не мигрируем; каталог допускает только проверенный обратимый патч.
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
        write_json(
            journal,
            {
                "previous": previous["current"],
                "candidate": str(release),
                "catalog": catalog,
                "catalog_restore_reverse": True,
            },
        )
        try:
            if catalog:
                self.run_catalog(catalog)
            self.activate(release)
        except BaseException:
            sys.stdout.write("Deployment failed; restoring previous application images\n")
            sys.stdout.flush()
            if catalog:
                self.run_catalog(catalog, True)
            self.activate(Path(previous["current"]))
            journal.unlink()
            raise
        if str(release) != previous["current"]:
            write_json(
                self.state_path,
                {
                    "current": str(release),
                    "previous": previous["current"],
                    "catalog_transition": catalog,
                    "catalog_direction": "forward",
                },
            )
        (self.root / ".deployed-revision").write_text(manifest["revision"] + "\n")
        journal.unlink()
        self.cleanup()
        sys.stdout.write(f"Healthy {self.target} release {manifest['revision']}\n")

    def rollback(self) -> None:
        state = json.loads(self.state_path.read_text())
        if not state.get("previous"):
            raise ValueError("No previous release")
        self.verify_environment()
        catalog = state.get("catalog_transition")
        reverse = state.get("catalog_direction", "forward") == "forward"
        journal = self.home / "transaction.json"
        write_json(
            journal,
            {
                "previous": state["current"],
                "candidate": state["previous"],
                "catalog": catalog,
                "catalog_restore_reverse": not reverse,
            },
        )
        try:
            if catalog:
                self.run_catalog(catalog, reverse)
            self.activate(Path(state["previous"]))
        except BaseException:
            if catalog:
                self.run_catalog(catalog, not reverse)
            self.activate(Path(state["current"]))
            journal.unlink()
            raise
        write_json(
            self.state_path,
            {
                "current": state["previous"],
                "previous": state["current"],
                "catalog_transition": catalog,
                "catalog_direction": "reverse" if reverse else "forward",
            },
        )
        manifest = json.loads((Path(state["previous"]) / "release.json").read_text())
        (self.root / ".deployed-revision").write_text(manifest["revision"] + "\n")
        journal.unlink()
        sys.stdout.write(f"Rolled back {self.target} to {manifest['revision']}\n")

    def recover_pending(self) -> None:
        journal = self.home / "transaction.json"
        if journal.exists():
            interrupted_release = json.loads(journal.read_text())
            committed = json.loads(self.state_path.read_text())["current"] == interrupted_release["candidate"]
            if not committed and interrupted_release.get("catalog"):
                self.run_catalog(
                    interrupted_release["catalog"], interrupted_release.get("catalog_restore_reverse", True)
                )
            recovered = Path(interrupted_release["candidate"] if committed else interrupted_release["previous"])
            self.activate(recovered)
            revision = json.loads((recovered / "release.json").read_text())["revision"]
            (self.root / ".deployed-revision").write_text(revision + "\n")
            journal.unlink()

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
        deployment.recover_pending()
        if args.action == "rollback":
            deployment.rollback()
        else:
            deployment.deploy(args.release.resolve())


if __name__ == "__main__":
    main()

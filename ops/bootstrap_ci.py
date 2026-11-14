"""Однократная установка приёмника из проверенного коммита; данные не трогает.

Запускается администратором вручную, после публикации кода. Для prod — только
после отдельного разрешения владельца. Root/password, sshd и VPN не меняет.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def legacy_manifest(root: Path, bundle: Path) -> dict:
    """Проверить работающие исходники v1 по опубликованному снимку до создания отката."""
    facts = json.loads((bundle / "ops/legacy-v1-baseline.json").read_text())
    if (
        not re.fullmatch(r"[0-9a-f]{40}", facts.get("revision", ""))
        or facts.get("version") != "1.5.6"
        or not facts.get("files")
    ):
        raise ValueError("Unsupported legacy baseline")
    for relative, expected in facts["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid legacy snapshot path or checksum")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Legacy source differs from the published v1 baseline")
    if not (root / ".env").is_file():
        raise ValueError("Original legacy environment is required")
    return facts


def freeze_legacy(root: Path, baseline: Path, bundle: Path, facts: dict) -> None:
    """Сохранить проверенный неизменяемый выпуск v1 для безопасного отката."""
    snapshot = baseline / "legacy-snapshot"
    snapshot.mkdir(mode=0o700)
    for relative in facts["files"]:
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != facts["files"][relative]:
            raise ValueError("Legacy source changed while freezing the baseline")
    shutil.copyfile(root / ".env", snapshot / ".env")
    (snapshot / ".env").chmod(0o600)
    shutil.copyfile(bundle / "ops/compose.legacy-v1.yml", baseline / "compose.legacy-v1.yml")


def retire_build_cache() -> None:
    """Освобождаем только неиспользуемый кэш характерных шагов сборки domofomka."""
    raw = subprocess.check_output(["docker", "buildx", "du", "--format", "json"], text=True)
    records = {row["ID"]: row for row in (json.loads(line) for line in raw.splitlines())}

    def depth(key: str) -> int:
        """Определить глубину зависимости кеша сборки для безопасного удаления."""
        return 1 + max((depth(p) for p in (records[key].get("Parents") or []) if p in records), default=0)

    patterns = (
        "uv sync --frozen --group api",
        "uv sync --frozen --group vkbot",
        "COPY codes.db ./codes.db",
        "COPY src/ ./src/",
        "COPY start_message.txt ./",
    )
    candidates = [
        key
        for key, row in records.items()
        if row["Reclaimable"] and not row["Shared"] and any(value in row["Description"] for value in patterns)
    ]
    for key in sorted(candidates, key=depth, reverse=True):
        subprocess.run(["docker", "buildx", "prune", "--filter", "id=" + key, "--force"], check=True)


def main() -> None:
    """Однократная установка приёмника из проверенного коммита; данные не трогает."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=["dev", "prod"])
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--retire-obsolete-images", action="store_true")
    parser.add_argument("--legacy-v1-baseline", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    bundle = Path(__file__).resolve().parent.parent
    facts = None
    if args.legacy_v1_baseline:
        if args.environment != "prod" or (root / ".ci/state.json").exists():
            raise ValueError("Legacy baseline is only for the first prod installation")
        facts = legacy_manifest(root, bundle)
    elif not (root / ".ci/state.json").exists():
        # v1 has no Runtime/queue guard; it needs its own rollback checks and frozen inputs.
        subprocess.run(
            [
                "docker",
                "exec",
                f"{'domofomka-dev' if args.environment == 'dev' else 'domofomka'}-vkbot-1",
                "python",
                "-c",
                "import importlib.util; assert importlib.util.find_spec('src.vkbot.runtime')",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    project = "domofomka-dev" if args.environment == "dev" else "domofomka"
    home = root / ".ci"
    home.mkdir(exist_ok=True)
    (home / "releases").mkdir(exist_ok=True)
    shutil.copyfile(bundle / "ops/receive_release.py", home / "receive_release.py")
    if not (home / "state.json").exists():
        baseline = home / "releases/baseline"
        (baseline / "ops").mkdir(parents=True)
        for name in ("compose.deploy.yml", "ops/deploy.py"):
            shutil.copyfile(bundle / name, baseline / name)
        manifest = {
            "environment": args.environment,
            "revision": facts["revision"] if facts else (root / ".deployed-revision").read_text().strip(),
        }
        if facts:
            freeze_legacy(root, baseline, bundle, facts)
            manifest.update(runtime="legacy-v1", version=facts["version"])
        for service, field in (("api", "api"), ("vkbot", "bot")):
            manifest[field] = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Image}}", f"{project}-{service}-1"], text=True
            ).strip()
        (baseline / "release.json").write_text(json.dumps(manifest))
        (home / "state.json").write_text(json.dumps({"current": str(baseline), "previous": None}))
    public = args.public_key.read_text().strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_-]+)?", public):
        raise ValueError("Invalid public key")
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", str(root)):
        raise ValueError("Invalid installation path")
    authorized = Path.home() / ".ssh/authorized_keys"
    original = authorized.read_text() if authorized.exists() else ""
    if public.split()[1] not in original:
        command = f"/usr/bin/python3 {home}/receive_release.py --root {root} --environment {args.environment}"
        with authorized.open("a") as output:
            output.write(f'\nrestrict,command="{command}" {public}\n')
    authorized.chmod(0o600)
    if args.retire_obsolete_images:
        protected = {
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Image}}", f"{project}-{service}-1"], text=True
            ).strip()
            for service in ("api", "vkbot")
        }
        legacy_path = home / "releases/baseline/release.json"
        if legacy_path.is_file():
            legacy = json.loads(legacy_path.read_text())
            if legacy.get("runtime") == "legacy-v1":
                protected.update(legacy[field] for field in ("api", "bot"))
        for service in ("api", "vkbot"):
            repository = project + "-" + service
            images = subprocess.check_output(
                ["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}", repository], text=True
            )
            for line in images.splitlines():
                image = json.loads(line)
                if image["Repository"] == repository and image["ID"] not in protected and image["Tag"] != "<none>":
                    subprocess.run(["docker", "image", "rm", repository + ":" + image["Tag"]], check=True)
        retire_build_cache()
    sys.stdout.write("Restricted CI key installed; existing access preserved\n")


if __name__ == "__main__":
    main()

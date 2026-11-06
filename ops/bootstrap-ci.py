"""Однократная установка приёмника из проверенного коммита; данные не трогает.

Запускается администратором вручную, после публикации кода. Для prod — только
после отдельного разрешения владельца. Root/password, sshd и VPN не меняет.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def retire_build_cache() -> None:
    """Освобождаем только неиспользуемый кэш характерных шагов сборки domofomka."""
    raw = subprocess.check_output(["docker", "buildx", "du", "--format", "json"], text=True)
    records = {row["ID"]: row for row in (json.loads(line) for line in raw.splitlines())}

    def depth(key: str) -> int:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=["dev", "prod"])
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--retire-obsolete-images", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    project = "domofomka-dev" if args.environment == "dev" else "domofomka"
    home = root / ".ci"
    home.mkdir(exist_ok=True)
    (home / "releases").mkdir(exist_ok=True)
    bundle = Path(__file__).resolve().parent.parent
    shutil.copyfile(bundle / "ops/receive_release.py", home / "receive_release.py")
    if not (home / "state.json").exists():
        baseline = home / "releases/baseline"
        (baseline / "ops").mkdir(parents=True)
        for name in ("compose.deploy.yml", "ops/deploy.py"):
            shutil.copyfile(bundle / name, baseline / name)
        manifest = {"environment": args.environment, "revision": (root / ".deployed-revision").read_text().strip()}
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

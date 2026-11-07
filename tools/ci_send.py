"""Передаёт релиз по SSH; токен реестра живёт только в памяти и временном DOCKER_CONFIG."""

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path


def main():
    action = os.environ.get("DEPLOY_ACTION", "deploy")
    if action not in {"deploy", "rollback", "rollback-legacy"}:
        raise ValueError("Unsupported deployment action")
    args = [
        "ssh",
        "-i",
        os.environ["DEPLOY_KEY_FILE"],
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + os.environ["DEPLOY_KNOWN_HOSTS_FILE"],
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "root@" + os.environ["DEPLOY_HOST"],
        action,
    ]
    if action in {"rollback", "rollback-legacy"}:
        subprocess.run(args, check=True)
        return
    manifest = {
        "environment": os.environ["DEPLOY_ENVIRONMENT"],
        "revision": os.environ["GITHUB_SHA"],
        "api": os.environ["API_IMAGE"],
        "bot": os.environ["BOT_IMAGE"],
        "api_bytes": int(os.environ["API_IMAGE_BYTES"]),
        "bot_bytes": int(os.environ["BOT_IMAGE_BYTES"]),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("compose.deploy.yml", "ops/deploy.py"):
            archive.add(name, arcname=name, recursive=False)
        data = (json.dumps(manifest) + "\n").encode()
        item = tarfile.TarInfo("release.json")
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    Path("release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    header = json.dumps({"actor": os.environ["GITHUB_ACTOR"], "token": os.environ["GHCR_TOKEN"]}).encode() + b"\n"
    subprocess.run(args, input=header + buffer.getvalue(), check=True)


if __name__ == "__main__":
    main()

"""Digest читается из GHCR: Docker может первым возвращать digest локального тега.

Размер файловой системы измеряется внутри образа; Docker с containerd может
показывать в inspect сжатый размер, недостаточный для расчёта места на VDS.
"""

import json
import os
import re
import subprocess
from pathlib import Path


def published_image(repository: str, revision: str) -> tuple[str, int]:
    image = repository + ":sha-" + revision
    raw = subprocess.check_output(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"], text=True
    )
    digest = json.loads(raw)["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry did not return an immutable digest")
    size = subprocess.check_output(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "du", image, "-sx", "--block-size=1", "/"],
        text=True,
    )
    return repository + "@" + digest, int(size.split()[0])


def main() -> None:
    revision = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Full commit SHA required")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, suffix in (("api", "api"), ("bot", "vkbot")):
            image, size = published_image("ghcr.io/omkality/domofomka-" + suffix, revision)
            output.write(f"{name}={image}\n{name}_bytes={size}\n")


if __name__ == "__main__":
    main()

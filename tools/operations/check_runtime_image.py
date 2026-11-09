"""Проверка реального образа с ограничениями Compose; запускается через stdin контейнера."""

import importlib
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryFile


def main() -> None:
    modules = {
        "api": ["src.code_service.app", "granian", "src.code_service.v2.postgres", "src.code_service.v2.catalog_update"],
        "vkbot": ["src.vkbot.main"],
    }[sys.argv[1]]
    assert os.getuid() == os.getgid() == 10001, "Application must run without root"
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert int(status["CapEff"], 16) == 0, "Application must not retain Linux capabilities"
    assert status["NoNewPrivs"].strip() == "1", "New privileges must be disabled"
    root = next(
        line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines() if line.split()[4] == "/"
    )
    assert "ro" in root[5].split(","), "Root filesystem must be read-only"
    with TemporaryFile(dir="/tmp") as stream:
        stream.write(b"bounded temporary storage")
    for module in modules:
        importlib.import_module(module)
    print(json.dumps({"uid": os.getuid(), "capabilities": 0, "no_new_privileges": True, "root_readonly": True}))


if __name__ == "__main__":
    main()

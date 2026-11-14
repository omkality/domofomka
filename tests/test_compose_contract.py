"""Проверка объединения реальных Compose: секреты dev, аргументы БД и ограничения."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configuration(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required to validate its merge semantics")
    for name in ["docker-compose.yml", "compose.dev.yml"]:
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=prod-password\nVK_GROUP_TOKEN=prod-token\nPROD_ONLY_SECRET=must-not-leak\n",
        encoding="utf8",
    )
    (tmp_path / ".env.dev").write_text(
        "POSTGRES_PASSWORD=dev-password\nVK_GROUP_TOKEN=dev-token\nDADATA_TOKEN=dev-dadata\n"
        "QUEUE_WORK_MEM=3MB\nAPI_HEALTH_REQUEST_TIMEOUT_SECONDS=2\n",
        encoding="utf8",
    )
    environment = dict(os.environ)
    for name in ["DOMOFOMKA_ENV_FILE", "POSTGRES_PASSWORD", "VK_GROUP_TOKEN", "QUEUE_WORK_MEM", "API_PUBLISH_PORT"]:
        environment.pop(name, None)
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.dev", "-f", "compose.dev.yml", "config", "--format", "json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf8",
        check=True,
    )
    return json.loads(result.stdout)


def test_dev_does_not_merge_production_environment(configuration):
    serialized = json.dumps(configuration)
    assert "prod-token" not in serialized
    assert "must-not-leak" not in serialized
    assert "prod-password" not in serialized
    assert configuration["services"]["vkbot"]["environment"]["VK_GROUP_TOKEN"] == "dev-token"
    assert configuration["services"]["api"]["environment"]["DADATA_TOKEN"] == "dev-dadata"
    assert configuration["services"]["api"]["environment"]["VK_GROUP_TOKEN"] == ""


def test_dev_inherits_resource_limits_and_postgres_tuning(configuration):
    services = configuration["services"]
    assert services["api"]["read_only"] is True
    assert services["vkbot"]["cap_drop"] == ["ALL"]
    assert int(services["queue"]["mem_limit"]) == 192 * 1024 * 1024
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["api"]["ports"][0]["published"] == "18000"
    assert "--work_mem=3MB" in services["queue"]["command"]
    assert services["queue"]["healthcheck"]["test"] == ["CMD", "pg_isready", "-U", "domofomka", "-d", "domofomka"]
    assert services["api"]["healthcheck"]["test"][:5] == ["CMD", "wget", "-q", "-T", "2"]

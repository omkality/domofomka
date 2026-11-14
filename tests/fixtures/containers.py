"""Одноразовая инфраструктура из закреплённых образов рабочего Compose."""

import json
import os
import re
import subprocess
import uuid
from collections.abc import Generator

import pytest
from psycopg.conninfo import make_conninfo
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import ExecWaitStrategy

from tests.paths import ROOT

# Служебный сборщик ресурсов Testcontainers, а не сервис приложения.
RYUK_IMAGE = "testcontainers/ryuk:0.8.1@sha256:bf3f74a47dee0acda89aba4b2fc9c7fdcf994a084db02a2d06566f07baae022e"


@pytest.fixture(scope="session")
def compose_services() -> dict:
    """Разрешить рабочий Compose с примером env, не читая личные настройки."""
    variable_names = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", (ROOT / "docker-compose.yml").read_text()))
    environment = {key: value for key, value in os.environ.items() if key not in variable_names}
    environment.update(DOMOFOMKA_ENV_FILE=".env.example", POSTGRES_PASSWORD="local-test-only")
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf8",
        check=True,
    )
    return json.loads(result.stdout)["services"]


@pytest.fixture(scope="session")
def container_network() -> Generator[Network, None, None]:
    """Соединить только тестовые приложения и PostgreSQL в отдельной сети."""
    testcontainers_config.ryuk_image = RYUK_IMAGE
    with Network(docker_network_kw={"labels": {"domofomka.test": "network"}}) as network:
        yield network


@pytest.fixture(scope="session")
def postgres_container(compose_services, container_network) -> Generator[DockerContainer, None, None]:
    """Запустить PostgreSQL один раз с рабочими digest, командой и лимитами."""
    config = compose_services["queue"]
    assert "@sha256:" in config["image"], "Test the exact pinned Compose image"
    readiness = [*config["healthcheck"]["test"][1:], "-h", "127.0.0.1"]
    container = DockerContainer(
        config["image"],
        mem_limit=config["mem_limit"],
        memswap_limit=config["memswap_limit"],
        pids_limit=config["pids_limit"],
        init=config["init"],
        labels={"domofomka.test": "postgres"},
    ).with_envs(**config["environment"])
    container.with_network(container_network).with_network_aliases("queue")
    # Docker поддерживает случайный порт с явной привязкой к loopback.
    container.ports["5432/tcp"] = ("127.0.0.1", None)
    container.with_command(config["command"]).waiting_for(ExecWaitStrategy(readiness).with_startup_timeout(60))
    with container:
        yield container


@pytest.fixture(scope="session")
def postgres_dsn(postgres_container, compose_services) -> str:
    """Выдать только адрес созданного тестами PostgreSQL, без внешнего DSN."""
    environment = compose_services["queue"]["environment"]
    host = postgres_container.get_container_host_ip()
    return make_conninfo(
        host="127.0.0.1" if host == "localhost" else host,
        port=postgres_container.get_exposed_port(5432),
        dbname=environment["POSTGRES_DB"],
        user=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


@pytest.fixture(scope="session")
def runtime_images(compose_services, tmp_path_factory) -> Generator[dict[str, str], None, None]:
    """Собрать приложения из рабочих Dockerfile один раз; CI передаёт те же готовые образы."""
    images, built = {}, []
    logs = tmp_path_factory.mktemp("runtime-build")
    suffix = uuid.uuid4().hex
    try:
        for service in ("api", "vkbot"):
            existing = os.getenv("TEST_" + service.upper() + "_IMAGE")
            image = existing or f"domofomka-test-{service}:{suffix}"
            if existing:
                subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, check=True)
            else:
                dockerfile = compose_services[service]["build"]["dockerfile"]
                with (logs / (service + ".log")).open("w", encoding="utf8") as output:
                    result = subprocess.run(
                        ["docker", "build", "-f", dockerfile, "-t", image, "."],
                        cwd=ROOT,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                if result.returncode:
                    pytest.fail(f"Docker build failed; see {logs / (service + '.log')}")
                built.append(image)
            images[service] = image
        yield images
    finally:
        for image in built:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, check=True)


@pytest.fixture
def runtime_container(runtime_images, compose_services, container_network):
    """Создать контейнер приложения с ограничениями из того же Compose."""

    def create(service: str) -> DockerContainer:
        """Вернуть ещё не запущенное приложение без монтирования исходников."""
        config = compose_services[service]
        container = DockerContainer(
            runtime_images[service],
            read_only=config["read_only"],
            cap_drop=config["cap_drop"],
            security_opt=config["security_opt"],
            mem_limit=config["mem_limit"],
            memswap_limit=config["memswap_limit"],
            pids_limit=config["pids_limit"],
            init=config["init"],
            labels={"domofomka.test": service},
        )
        return container.with_network(container_network).with_tmpfs_mount("/tmp", "size=16m,mode=1777")

    return create

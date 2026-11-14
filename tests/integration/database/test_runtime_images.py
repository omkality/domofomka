"""Рабочие Linux-образы, реальный HTTP и PostgreSQL без VK или DaData."""

import json
from urllib.parse import quote

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from testcontainers.core.wait_strategies import ExecWaitStrategy

from src.infrastructure.database.migrate import apply
from tests.paths import ROOT


def test_production_api_image_searches_postgres_and_bot_image_runs_queue_commands(
    isolated_database, runtime_container, compose_services
):
    apply(isolated_database, "catalog")
    apply(isolated_database, "queue")
    with psycopg.connect(isolated_database) as db:
        db.execute("INSERT INTO metadata VALUES ('catalog_schema_version','1'),('publication_state','ready')")
        db.execute(
            "INSERT INTO buildings(id,display,city,street,house,street_key,city_key,lineage) "
            "VALUES ('test-house','Москва, улица Тестовая, дом 10к1','Москва','Тестовая','10к1',"
            "'тестовая','москва','москва')"
        )
        db.execute("INSERT INTO entrances VALUES ('test-house','2')")
        db.execute("INSERT INTO code_sources VALUES ('test')")
        db.execute("INSERT INTO codes VALUES ('test-house','2','test','ONLY-10K1')")
        db.execute("INSERT INTO streets VALUES ('street','москва','тестовая','москва тестовая',1)")
        db.execute("INSERT INTO aliases VALUES ('test-house','москва','тестовая','10к1')")
    internal_dsn = make_conninfo(isolated_database, host="queue", port=5432)
    api_config = compose_services["api"]
    api = runtime_container("api")
    api.with_envs(
        API_HOST="0.0.0.0",
        API_PORT="8000",
        WORKERS_NUM="1",
        DADATA_TOKEN="unused-test-token",
        DB_NAME="/tmp/no-legacy.db",
        LEGACY_INDEX_PATH="/tmp/no-legacy-index.db",
        CATALOG_DATABASE_URL=internal_dsn,
        SEARCH_CACHE_TTL_SECONDS="0",
        USE_PROXY="false",
    )
    api.ports["8000/tcp"] = ("127.0.0.1", None)
    api.with_command(api_config["command"])
    api.waiting_for(ExecWaitStrategy(api_config["healthcheck"]["test"][1:]).with_startup_timeout(60))
    with api:
        host = api.get_container_host_ip()
        host = "127.0.0.1" if host == "localhost" else host
        with httpx.Client(
            base_url=f"http://{host}:{api.get_exposed_port(8000)}", trust_env=False, timeout=10
        ) as client:
            assert client.get("/version").json()["version"]
            response = client.get("/api/v2/codes/msg", params={"message": "Москва Тестовая 10к1"})
            assert response.status_code == 200
            answer = response.json()
            assert answer["status"] == "found", answer
            assert answer["engine"] == "v2"
            assert answer["data"] == {"2": [["ONLY-10K1", "test"]]}
            building = client.get("/api/v2/buildings/" + quote(answer["building_id"], safe=""))
            assert building.json()["data"] == answer["data"]
            assert client.get("/api/v2/buildings/missing").status_code == 404
        check = api.exec(["python", "-c", (ROOT / "tools/operations/check_runtime_image.py").read_text(), "api"])
        assert check.exit_code == 0, check.output.decode()
    bot = runtime_container("vkbot")
    bot.with_envs(
        VK_GROUP_TOKEN="unused-test-token",
        VK_GROUP_ID="223949627",
        EXPECTED_VK_GROUP_ID="223949627",
        QUEUE_DATABASE_URL=internal_dsn,
        ANTI_SPAM_TIME="2",
        DOMOFOMKA_API_HOST="unused",
        DOMOFOMKA_API_PORT="8000",
    )
    bot.with_command(["python", "-m", "src.vkbot.manage", "status"])
    with bot:
        assert bot.wait() == 0, bot.get_logs()
        result = json.loads(bot.get_logs()[0])
        assert result["bot_inbox"] == []
        assert result["bot_outbox"] == []
    with psycopg.connect(isolated_database) as db:
        assert db.execute("SELECT count(*) FROM bot_inbox").fetchone()[0] == 0
        assert conninfo_to_dict(internal_dsn)["dbname"] == conninfo_to_dict(isolated_database)["dbname"]

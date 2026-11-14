"""Согласованность настоящего тестового PostgreSQL с рабочим Compose."""

import psycopg


def test_postgres_container_uses_compose_digest_tuning_and_limits(postgres_container, postgres_dsn, compose_services):
    config = compose_services["queue"]
    container = postgres_container.get_wrapped_container()
    container.reload()
    assert container.attrs["Config"]["Image"] == config["image"]
    assert container.attrs["Config"]["Cmd"] == config["command"]
    assert container.attrs["HostConfig"]["Memory"] == int(config["mem_limit"])
    assert container.attrs["HostConfig"]["MemorySwap"] == int(config["memswap_limit"])
    assert container.attrs["HostConfig"]["PidsLimit"] == config["pids_limit"]
    assert container.attrs["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] == "127.0.0.1"
    with psycopg.connect(postgres_dsn) as db:
        settings = dict(db.execute("SELECT name, setting FROM pg_settings"))
        expected_connections = next(
            arg.split("=", 1)[1] for arg in config["command"] if arg.startswith("--max_connections=")
        )
        assert settings["max_connections"] == expected_connections
        assert db.execute("SELECT current_database()").fetchone()[0] == config["environment"]["POSTGRES_DB"]

"""Явное применение yoyo-миграций; рабочие процессы не изменяют SQL-схему."""

import argparse
import json
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from yoyo import get_backend, read_migrations
from yoyo.backends import DatabaseBackend

from src.infrastructure.network import network_settings

ROOT = Path(__file__).resolve().parent / "migrations"


@contextmanager
def connected(dsn: str) -> Generator[DatabaseBackend, None, None]:
    """Открыть ограниченное соединение yoyo через уже используемый Psycopg 3."""
    parameters = conninfo_to_dict(dsn.replace("postgresql+psycopg://", "postgresql://", 1))
    user = parameters.pop("user", "")
    password = parameters.pop("password", "")
    database = parameters.pop("dbname")
    host = parameters.pop("host", "")
    port = parameters.pop("port", "5432")
    if host.startswith("/"):
        parameters["host"], host = host, ""
    if ":" in host:
        host = "[" + host + "]"
    credentials = (quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@") if user else ""
    parameters.setdefault("connect_timeout", str(network_settings.queue_db_connect_timeout_seconds))
    parameters.setdefault(
        "options",
        (
            f"-c statement_timeout={network_settings.migration_statement_timeout_ms} "
            f"-c lock_timeout={network_settings.queue_db_lock_timeout_ms}"
        ),
    )
    authority = credentials + host + (":" + port if host else "")
    url = "postgresql+psycopg://" + authority + "/" + quote(database, safe="") + "?" + urlencode(parameters)
    backend = get_backend(url)
    try:
        yield backend
    finally:
        backend.connection.close()


def apply(dsn: str, target: str = "queue", through: str | None = None) -> list[str]:
    """Применить только ожидающие миграции под блокировкой yoyo."""
    migrations = read_migrations(str(ROOT / target))
    if through is not None:
        if through not in {migration.id for migration in migrations}:
            raise ValueError("Unknown migration revision")
        migrations = migrations.__class__([m for m in migrations if m.id <= through], migrations.post_apply)
    with connected(dsn) as backend, backend.lock(timeout=network_settings.queue_db_connect_timeout_seconds):
        pending = backend.to_apply(migrations)
        backend.apply_migrations(pending)
    return [migration.id for migration in pending]


def main() -> None:
    """Применить, показать или явно откатить миграции без секретов в аргументах."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "status", "rollback"])
    parser.add_argument("target", choices=["queue", "catalog"])
    parser.add_argument("--adopt-baseline", action="store_true")
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--allow-destructive", action="store_true")
    args = parser.parse_args()
    if args.steps < 1 or (args.action == "rollback" and not args.allow_destructive):
        parser.error("Rollback requires positive --steps and explicit --allow-destructive")
    environment = dict(dotenv_values(".env")) | dict(os.environ)
    dsn = environment["QUEUE_DATABASE_URL"]
    if args.target == "catalog":
        owner = conninfo_to_dict(dsn)
        reader = conninfo_to_dict(environment["CATALOG_DATABASE_URL"])
        if owner.get("host") != reader.get("host") or owner.get("port", "5432") != reader.get("port", "5432"):
            parser.error("Catalog migrations require the queue administrator on the same PostgreSQL server")
        dsn = make_conninfo(dsn, dbname=reader["dbname"])
    if args.adopt_baseline:
        if args.action != "apply" or args.target != "catalog":
            parser.error("Baseline adoption is only supported for the published catalog")
        from src.infrastructure.database.catalog import adopt_baseline

        adopt_baseline(dsn)
    if args.action == "apply":
        result = {"applied": apply(dsn, args.target)}
    else:
        migrations = read_migrations(str(ROOT / args.target))
        with connected(dsn) as backend, backend.lock(timeout=network_settings.queue_db_connect_timeout_seconds):
            pending = backend.to_apply(migrations)
            if args.action == "rollback":
                selected = backend.to_rollback(migrations)[: args.steps]
                backend.rollback_migrations(selected)
                result = {"rolled_back": [m.id for m in selected]}
            else:
                result = {"pending": [m.id for m in pending]}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()

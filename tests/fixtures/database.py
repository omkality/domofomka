"""Настоящий тестовый PostgreSQL с миграциями и отдельным namespace на тест."""

import uuid
from collections.abc import Generator

import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def migrated_database(postgres_dsn) -> str:
    """Применить настоящие yoyo-миграции к PostgreSQL из Testcontainers."""
    from src.infrastructure.database.migrate import apply

    apply(postgres_dsn)
    return postgres_dsn


@pytest_asyncio.fixture
async def queue_factory(migrated_database):
    """Создать очередь в своём сообществе и удалить только её строки после теста."""
    from src.vkbot.queue import QueueStore

    created = []

    async def create(*, telemetry=False, lease_seconds=3):
        """Выдать независимую очередь с нужным режимом диагностики."""
        group = 1_000_000_000 + uuid.uuid4().int % 1_000_000_000
        queue = QueueStore(migrated_database, group, lease_seconds=lease_seconds, telemetry=telemetry)
        queue.environment = "development"
        await queue.initialize()
        created.append(queue)
        return queue

    yield create
    for queue in created:
        async with queue.connect() as conn:
            for table in ("request_log", "bot_outbox", "bot_inbox", "bot_state", "bot_message_context", "bot_receipts"):
                await conn.execute(f"DELETE FROM {table} WHERE group_id=%s", (queue.group_id,))


@pytest_asyncio.fixture
async def store(queue_factory):
    """Выдать очередь без диагностики с короткой арендой заданий."""
    return await queue_factory()


@pytest_asyncio.fixture
async def observed_store(queue_factory):
    """Выдать очередь с записью этапов обработки и сетевых попыток."""
    return await queue_factory(telemetry=True, lease_seconds=120)


@pytest.fixture
def isolated_database(postgres_dsn) -> Generator[str, None, None]:
    """Выдать пустую БД для проверки DDL, не затрагивая общие таблицы очереди."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "domofomka_test_" + uuid.uuid4().hex
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            yield make_conninfo(postgres_dsn, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

"""Изолированное окружение тестов и регистрация общих фикстур."""

import asyncio
import os
import sys

import pytest

pytest_plugins = ["tests.fixtures.containers", "tests.fixtures.database"]


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


# Значения приложений не должны попадать в тесты из личного .env разработчика.
os.environ.update(
    {
        "API_HOST": "127.0.0.1",
        "API_PORT": "8000",
        "DB_NAME": "unused-test.db",
        "DADATA_TOKEN": "unused-test-token",
        "DADATA_PROXY_URL": "",
        "VK_GROUP_TOKEN": "unused-test-token",
        "VK_GROUP_ID": "223949627",
        "EXPECTED_VK_GROUP_ID": "223949627",
        "QUEUE_DATABASE_URL": "postgresql://unused",
        "ANTI_SPAM_TIME": "2",
        "DOMOFOMKA_API_HOST": "localhost",
        "DOMOFOMKA_API_PORT": "8000",
        "VK_PROXY_URL": "",
        "CATALOG_DATABASE_URL": "postgresql://unused@127.0.0.1:1/unused",
        "OPENSEARCH_URL": "",
    }
)

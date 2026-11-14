"""Изолированное окружение тестов и регистрация общих фикстур."""

import os

pytest_plugins = ["tests.fixtures.database"]

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
        "QUEUE_DATABASE_URL": os.environ.get("TEST_QUEUE_DATABASE_URL", "postgresql://unused"),
        "ANTI_SPAM_TIME": "2",
        "DOMOFOMKA_API_HOST": "localhost",
        "DOMOFOMKA_API_PORT": "8000",
        "VK_PROXY_URL": "",
        "SEARCH_BACKEND": "sqlite",
        "OPENSEARCH_URL": "",
    }
)

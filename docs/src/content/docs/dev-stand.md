---
title: Стенд разработки
description: Запуск dev и безопасная проверка отдельного сообщества VK.
---

## Назначение

Dev использует отдельное тестовое сообщество VK, свою очередь и опубликованный каталог.
Токен и ожидаемый ID сообщества проверяются при запуске. Второй активный сборщик того же
сообщества не допускается блокировкой PostgreSQL. В `master` и prod изменения переходят отдельно.

## Локальный запуск

Подготовьте `.env.dev` по `.env.dev.example`, исходную SQLite v1, её индекс и каталог.
Все поля окружения указаны явно; секреты заполняются только в игнорируемом файле.
Docker Compose 2.24.4+ нужен для безопасного `!override` окружения.

```bash
docker compose --env-file .env.dev -f compose.dev.yml up -d --build queue
docker compose --env-file .env.dev -f compose.dev.yml build api vkbot
docker compose --env-file .env.dev -f compose.dev.yml run --rm --no-deps vkbot /backend/.venv/bin/python -m src.infrastructure.database.migrate apply queue
docker compose --env-file .env.dev -f compose.dev.yml up -d --build api vkbot
```

Для PostgreSQL-поиска предварительно публикуется отдельная БД каталога и роль чтения.
HTTP-сервис v2 требует PostgreSQL-каталог и `CATALOG_DATABASE_URL`.
Для офлайн-проверок без сервера используется файловый эталон из `tools/catalog/sqlite_catalog.py`;
он не включается настройкой HTTP API.
Сведения о загрузке и миграциях находятся в [БД](database.mdx).

## Публикация на VDS

Push `dev` запускает проверки, сборку и деплой через GitHub Actions. На VDS передаются
проверенные digest API и бота. Сервер не собирает исходники; `.env.dev`, базы и VPN сохраняются.
Миграции выполняются отдельным одноразовым контейнером до запуска нового бота.

## Проверки

`tests/unit` содержит изолированные правила и сценарии, `tests/integration` — HTTP,
реальный PostgreSQL, подготовку каталогов и контракты операций. Общие ресурсы находятся
в `tests/fixtures`, их подключает `tests/conftest.py`.

```bash
uv sync --frozen --all-groups
uv run python -m pytest tests/unit
uv run python -m pytest tests/integration
```

SQL-тесты сами запускают отдельный PostgreSQL через Testcontainers; нужен работающий Docker.
Живая приёмка `tools.operations.dev_acceptance` проверяет тестовый API и сетевые маршруты
без рассылки пользователям. Управление очередью и подстановка отказов доступны через
`python -m src.vkbot.manage`; fault-команды действуют только при разрешённом dev-режиме.

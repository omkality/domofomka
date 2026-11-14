---
title: "Миграции yoyo вручную"
---

Yoyo поддерживает SQL-файлы и Python-скрипты. В проекте используется SQL-формат:
он подходит нашим изменениям схемы и не требует ORM. Это полноценные миграции yoyo,
с зависимостями, журналом применения и откатом. Обычный `yoyo new` создаёт Python,
а `yoyo new --sql` — SQL. Оба формата можно использовать в одной цепочке.

## Где лежат миграции

| Каталог | Что меняет | Последняя исходная ревизия |
| --- | --- | --- |
| `src/infrastructure/database/migrations/queue/` | Inbox/outbox, контекст, состояние и диагностику | `0002-telemetry` |
| `src/infrastructure/database/migrations/catalog/` | Каталог адресов, коды, индексы и связи | `0002-catalog-indexes` |

Это две отдельные БД и две независимые цепочки. Применённые файлы **не редактируют
и не переименовывают**, включая пробелы: yoyo сохраняет контрольную сумму.
Для нового изменения добавляют следующую ревизию. Новые Python-миграции также
попадут в образ: Docker копирует весь каталог миграций.

## Создать SQL-миграцию

Все команды выполняются из корня репозитория. Зависимости берутся из `uv.lock`.
Следующая команда только создаёт файл, не подключается к БД и не применяет DDL:

```bash
uv run --frozen --all-groups yoyo new --no-config-file --batch --sql -m "Add queue diagnostic field" src/infrastructure/database/migrations/queue
```

Yoyo создаст имя вида `20261114_01_abcde-add-queue-diagnostic-field.sql`.
`--batch` отключает запуск редактора; файл заполняют вручную. Без `--batch` yoyo
откроет настроенный редактор. Для каталога замените последний путь на `.../catalog`.

Например, новая ревизия очереди может выглядеть так (это образец, такого столбца
в текущей схеме нет):

```sql
-- Add optional diagnostic tag
-- depends: 0002-telemetry

ALTER TABLE bot_inbox
  ADD COLUMN diagnostic_tag TEXT;
```

Рядом вручную создайте файл с **тем же именем**, но окончанием `.rollback.sql`:

```sql
-- Remove optional diagnostic tag

ALTER TABLE bot_inbox
  DROP COLUMN diagnostic_tag;
```

`-- depends:` содержит ID предыдущей миграции без расширения. После появления новых
ревизий указывайте актуальный конец цепочки, а не всегда `0002-telemetry`.
У каталога зависимость своя. Пустой rollback уместен только при осознанной
необратимости; в таком случае восстановление данных должно быть описано отдельно.

## Проверить, применить, повторить

Сначала используйте отдельную локальную тестовую БД или dev. Файл `.env.dev`
в примерах — ваш локальный, неотслеживаемый Git файл подключения к **выбранной** БД;
в проекте он автоматически не создаётся. Адрес внутри Docker (`queue-db`) не
доступен с Windows: для локального запуска нужен опубликованный loopback-порт
тестового PostgreSQL или разрешённый SSH-туннель.

```bash
uv run --frozen --all-groups --env-file .env.dev python -m src.infrastructure.database.migrate status queue
uv run --frozen --all-groups --env-file .env.dev python -m src.infrastructure.database.migrate apply queue
uv run --frozen --all-groups --env-file .env.dev python -m src.infrastructure.database.migrate status queue
```

`status` выводит JSON с `pending`; пустой список означает отсутствие ожидающих
миграций. `apply` выводит ID применённых ревизий. Повторный запуск возвращает
`{"applied": []}`. Для каталога замените `queue` на `catalog`.

Команда читает `.env`, затем переменные процесса имеют приоритет. `uv --env-file`
передаёт выбранные значения процессу; уже экспортированные переменные оболочки
также имеют приоритет, поэтому перед запуском проверьте окружение.
Без `--env-file` используется обычный `.env` и переменные оболочки.
Пароли не передаются в аргументах команд и не должны попадать в историю Git.

Для очереди нужен `QUEUE_DATABASE_URL` владельца БД. Для каталога нужны обе
переменные: административный пользователь берётся из `QUEUE_DATABASE_URL`, а имя
целевой БД — из `CATALOG_DATABASE_URL`. Хост и порт должны совпадать. Read-only роль
HTTP API не применяется для DDL. Обёртка сама выбирает yoyo-бэкенд
`postgresql+psycopg`; Psycopg 3 и yoyo остаются синхронными в этой отдельной команде.

Приложения не создают таблицы при старте. В CI/CD миграции выполняются перед
переключением API/бота, через тот же модуль в отдельном образе миграций.
Обычный деплой идёт через [GitHub Actions](cicd.mdx), ручная команда нужна для
администрирования и проверки. `--adopt-baseline` используется только при первом
принятии уже существующего каталога под управление yoyo; для новых ревизий не нужен.

## Откат и тесты

Просмотрите SQL отката, остановите зависимые приложения и сохраните данные,
которые откат может удалить. На изолированной БД проверьте полный цикл:

```bash
uv run --frozen --all-groups --env-file .env.dev python -m src.infrastructure.database.migrate rollback queue --steps 1 --allow-destructive
uv run --frozen --all-groups --env-file .env.dev python -m src.infrastructure.database.migrate apply queue
```

Откат базовых ревизий удаляет таблицы; откат последующего столбца удалит его значения.
`--steps 1` выбирает одну последнюю применённую ревизию цепочки.
В CI/CD откатывают **образы**, а не схему БД: миграции должны быть совместимы
с предыдущим приложением, пока оно используется для rollback.

SQL-интеграции сами запускают PostgreSQL через Testcontainers из образа рабочего
Compose. Временные БД для миграций создаются на каждый тест и удаляются после него;
достаточно работающего Docker, DSN вручную задавать не нужно:

```powershell
uv run --frozen --all-groups python -X utf8 -m pytest tests/integration/database/test_migrations.py -q
```

Новая миграция должна получить проверку применения на существующих данных,
повторного запуска и отката. Длительный DDL ограничен
`MIGRATION_STATEMENT_TIMEOUT_MS`, ожидание блокировок — `QUEUE_DB_LOCK_TIMEOUT_MS`.

## Если нужен Python-скрипт

Уберите `--sql` из команды создания. Python уместен, когда нужен программный
перенос данных, который неудобно выразить SQL. Обычное добавление столбца или индекса
остаётся SQL. Минимальный эквивалент приведённого образца:

```python
"""Добавить необязательную диагностическую метку."""

from yoyo import step

__depends__ = {"0002-telemetry"}

steps = [
    step(
        "ALTER TABLE bot_inbox ADD COLUMN diagnostic_tag TEXT",
        "ALTER TABLE bot_inbox DROP COLUMN diagnostic_tag",
    ),
]
```

Для Python обратное действие находится в `step(forward, backward)`;
отдельный `.rollback.sql` к этому скрипту не нужен. Модуль `migrate` обнаруживает
SQL- и Python-файлы одинаково. Переводить существующие SQL-ревизии в Python не нужно.

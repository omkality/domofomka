# Инструменты проекта

Рабочий бот и HTTP-сервис находятся в `src/`. Здесь — команды подготовки данных,
оценки качества и развёртывания. Они не импортируются рабочими приложениями.

| Пакет | Назначение |
| --- | --- |
| `catalog` | Импорт ГАР, построение каталогов и индекса v1, проверяемые исправления исходных связей. |
| `history` | Выгрузка переписок и аудит пользовательских запросов. |
| `evaluation` | Повтор запросов, сравнение ответов и контроль скорости. |
| `experiments` | Сравнение поисковых движков, анализаторов и библиотек. Не часть рабочего окружения. |
| `operations` | Передача выпуска в CI/CD, проверка образов и приёмка dev. |
| `configuration` | Проверка полноты и единого порядка переменных окружения без изменения значений. |

Запускайте из корня репозитория через `python -m`, например:

```bash
uv run --frozen --all-groups python -m tools.catalog.build_catalog --help
uv run --frozen --all-groups python -m tools.history.export_vk --help
uv run --frozen --all-groups python -m tools.evaluation.evaluate_search --help
uv run --frozen --all-groups python -m tools.operations.dev_acceptance --help
uv run --frozen --all-groups python -m tools.configuration.environment --check-examples
```

У каждой команды свой `--help`. Не запускайте импорт или исправление каталога
на единственном экземпляре данных. Переписки, токены, исходные базы и построчные
отчёты сохраняются в игнорируемых каталогах; в Git попадают код и сводные выводы.
Команды оценки фиксируют контрольные суммы кода, словарей и входных данных.

Существующие этапы подготовки разделены по пакетам, без дополнительного
CLI-фреймворка и универсального «конвейера»: каждый этап можно повторить отдельно.
Экспериментальные зависимости не добавляются в рабочие образы.

Отдельное локальное окружение OpenSearch включается явно:

```bash
docker compose --env-file .env.dev -f tools/experiments/compose.opensearch.yml --profile search-comparison up -d opensearch
```

Порт `19200` доступен только через localhost. Dashboards не используется и не запускается.
Это окружение сравнения, его не следует поднимать на маленьких VDS.

[Оценка поиска](../docs/src/content/docs/search-v2.md),
[CI/CD](../docs/src/content/docs/cicd.mdx), [адресный язык](../docs/src/content/docs/address-language.md).

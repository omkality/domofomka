# Группы изменений dev

Исходная опубликованная база — master v1.5.6 (`367563c`). При этой разработке прод не менялся.
Прежние незакоммиченные изменения inbox/outbox сохранены и опубликованы в dev коммитом `2e7745a`.

Первоначальный план исходных изменений уже выполнен:

1. `888036c` — каталог ГАР и версионируемый поиск адресов.
   - `src/search/`, `src/api/app.py`, `src/api/config.py`, `src/api/v2.py`.
   - `tools/import_gar.py`, `tools/build_catalog.py`, `tools/index_search.py`.
   - `pyproject.toml`, `uv.lock`, тесты API и поиска, примеры настроек.
2. `c3b3cb1` — выбор адреса в боте и удаление Redis из рабочего окружения.
   - `src/vkbot/config.py`, `processor.py`, `queue.py`, `src/network.py`.
   - Упрощение `src/storages/interfaces.py`, удаление `src/storages/redis.py`.
   - Файлы Compose и тесты бота с настоящим PostgreSQL.
3. `1ead8d8` — локальное окружение поиска, закрытый аудит переписок и проверки CI.
   - `compose.search.yml`, `tools/export_vk.py`, `audit_history.py`, `evaluate_search.py`.
   - `.github/workflows/dev-tests.yml`, `ruff.toml`, проверки копии алгоритма v1.

Последующие исправления уточняют нормализацию, сохранность вариантов адресов и проверки корпуса.
Подробный список опубликованных изменений доступен через `git log dev`.
Документация не включена в эти группы исходных изменений по прежней просьбе владельца.

Следующий этап разработки:

- `6a1d7b0` — очистка очереди с журналом идентификаторов, резервные копии, контроль ресурсов и отложенная геолокация.
  Изменения: `src/vkbot/`, тесты очереди и бота, `ops/`, файлы Compose и исключения для резервных копий.
- `7f53651` — индекс v1, исправления типов улиц и пометок города, инструменты аудита и проверки качества.
  Изменения: `src/services/`, `src/search/`, API v2, инструменты в `tools/`, тесты и примеры настроек.
- `ffe8f6a` — сохранение исторических схем нумерации при проверке резервного ответа v1.
- `6b6ad78` — чтение срока хранения копий и запаса диска из окружения.
- `691f359` — ускорение точных запросов по адресам с кодами без изменения результатов полного корпуса.
  Изменения: `src/search/engine.py`, тесты поиска, `tools/profile_search.py`.
- `793a6a5` — индексы незавершённых заданий и времени обновления контекста для обслуживания очереди.
  Изменения: `src/vkbot/queue.py`.
- `da85913` — чтение новых сообщений для последующей проверки и обобщение сводки ручной разметки.
  Изменения: `tools/export_recent_vk.py`, `tools/quality_suite.py`.
Переписки, закрытые отчёты, доступы, исходные базы и файлы ГАР остаются вне Git.

Компактный каталог уже работает на немецком тестовом сервере без зависимости от ПК.
OpenSearch остаётся локальным инструментом сравнения; постоянный запуск на маленьких VDS не планируется.
На момент этого описания ближайшие дома по расстоянию не предлагаются: достоверные координаты ещё не добавлены.

`334e4d2` — группа исходных изменений, [#1](https://github.com/omkality/domofomka/issues/1), `priority:P1`:
ограниченный учёт запросов, поисков v1/v2 и сетевых попыток в существующем PostgreSQL.
Файлы: `src/telemetry.py`, `src/api/telemetry.py`, `src/vkbot/telemetry.py`, интеграция в API,
поиск, сетевые клиенты, очередь и runtime; `tests/test_telemetry.py`, примеры `.env`.
`0debe70` — пакетная запись измерений после операции и восстановление неполной сводки обслуживанием очереди.
Следом — [#2](https://github.com/omkality/domofomka/issues/2), административные формулировки и подтверждённые дубли.

- `672d4df` — изолированное сравнение нормализованного PostgreSQL с SQLite без изменения алгоритма v2.
  Файлы: `tools/postgres_catalog_experiment.py`, `tools/postgres_search_experiment.py`,
  `tools/compare_postgres_catalog.py`, `tests/test_postgres_catalog_experiment.py`.
- `db34cb1` — проверка муниципальных вариантов населённых пунктов по официальному ГАР
  и разбор длинного адреса с сохранением области и округа.
  Файлы: `src/search/localities.py`, `src/search/normalize.py`, `tools/build_catalog.py`,
  `tools/build_locality_facts.py`, `tests/test_localities.py`.
- `ee05e55` — отдельная причина привязки муниципального варианта в происхождении исходных строк.
  Файлы: `tools/build_catalog.py`, `tests/test_localities.py`.

- `321152e` — рабочий PostgreSQL-клиент v2 с ограниченными соединениями и ожиданиями,
  проверкой ревизии, кешем и fallback v1.
  Файлы: `src/search/postgres.py`, `src/api/`, `pyproject.toml`, `uv.lock`,
  `tools/build_postgres_catalog.py`, `tools/compare_postgres_catalog.py`, `tests/test_postgres_catalog.py`.
- `530adcb` — пределы WAL/временных файлов и исключение дублирующей codes.db из API-образа.
  Файлы: `docker-compose.yml`, `Dockerfile.api`, примеры `.env`.
- `87295aa` — рабочие образы без dev-зависимостей и кеша, стандартная проверка HTTP,
  одинаковые ограничения PostgreSQL в обоих вариантах Compose.
  Файлы: Dockerfile API/бота, `docker-compose.yml`, `compose.dev.yml`, уточнение комментария `src/api/telemetry.py`.
- `4c2df8e` — сборка минимальных образов и проверка их рабочих импортов в CI.
  Файл: `.github/workflows/dev-tests.yml`.

Исправления идентичности адресов 7 ноября:

- `9008dde` — сохранение значимых названий и точной нумерации кварталов.
  Файлы: `src/search/normalize.py`, `constraints.py`, `engine.py`, `postgres.py`, `catalog_update.py`,
  `src/api/v2.py`, `src/search/catalog_release.json`, построители и профили в `tools/`,
  регрессионные тесты адресных ограничений.
- `5cb1a2d` — сохранение выбора для сокращённых и частичных официальных имён.
  Файлы: `src/search/constraints.py`, `engine.py`, `tests/test_search_name_identity.py`.
- `facc929` — точная привязка неправильно разделённых полей источника к единственному дому ГАР.
  Файлы: `tools/reconcile_source_fields.py`, `reconcile_street_order.py`, `repair_house_identity.py`,
  `tests/test_source_field_alignment.py`, `src/search/catalog_release.json`.

Закрытые патчи каталога доставляются отдельно с проверкой SHA-256. Ветка master и prod не меняются.

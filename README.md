# domofomka

Бот для поиска кодов домофонов по адресу.
Технологии рабочего окружения: *Litestar*, *Granian*, *PostgreSQL* (`tsvector` / GIN для кандидатов v2, очередь и диагностика), *RapidFuzz* (опечатки и ранжирование), *SQLite* (источник v1), *VK API*.

Это лексический поиск: расширение `pgvector`, эмбеддинги и семантический поиск сейчас не используются.
OpenSearch не запускается вместе с ботом; отдельные инструменты сравнения находятся в `tools/experiments`.

[Документация с интерактивными LikeC4-схемами](https://omkality.github.io/domofomka/): архитектура, процессы по шагам,
схема БД, эксплуатация и результаты исследований. Сайт собирается в статические HTML/CSS/JS;
Node и дополнительные сервисы документации на VDS не нужны.
[Локальный просмотр и сборка](docs/README.md).

## Устройство

```mermaid
flowchart LR
    VK[VK] <-->|Long Poll| B[Бот]
    B --> I[(Входящие события PostgreSQL)]
    H[Сверка истории VK] --> I
    I --> W[Обработчики]
    W --> A[HTTP API]
    A --> V2[API v2: нормализация запроса]
    V2 --> C[(PostgreSQL: улицы, точный дом и коды)]
    V2 -->|Сбой или адрес не найден| V1[API v1: прежний поиск]
    V1 --> DB[(Исходная SQLite с кодами)]
    A --> D[Геокодирование DaData]
    D -->|Определённый адрес| V2
    W --> O[(Исходящие действия PostgreSQL)]
    O -->|Отправка или редактирование с повторами| VK
```

События и подготовленные ответы сохраняются при перезапуске. Записанные нажатия кнопок можно повторить; нажатия, которые VK не доставил боту, из истории восстановить нельзя.
Частота обработки ограничена отдельно для каждого пользователя; из лишних ожидающих запросов сохраняется последний. [Настройки лимитера](docs/src/content/docs/message-rate-limit.md).
Отложенный повтор геолокации пропускает новые сообщения вперёд. Очистка очереди сохраняет защиту от дублей;
резервные копии и контроль ресурсов описаны в [инструкции эксплуатации](docs/src/content/docs/operations.md).
Этапы поиска и сетевые маршруты описаны в [измерениях запросов](docs/src/content/docs/telemetry.md),
ближайшие задачи — в [плане dev](docs/src/content/docs/research/roadmap.md).

```mermaid
erDiagram
    buildings ||--o{ entrances : "дом"
    entrances ||--o{ codes : "подъезд"
    code_sources ||--o{ codes : "источник"
    buildings ||--o{ aliases : "варианты адреса"
    buildings ||--o{ provenance : "исходные строки"
```

[Полная схема БД](docs/src/content/docs/database.mdx) показывает поля, ключи, поисковые таблицы и очередь.
[Архитектура кода](docs/src/content/docs/architecture.mdx): VK-бот, сервис кодов с реализациями v1/v2 и инфраструктура.

```mermaid
flowchart LR
    D[Push dev] --> CI[Тесты и сборка в GitHub Actions]
    CI --> R[GHCR: проверенные образы]
    R --> DEV[Автоматический dev]
    R --> G[Ручной выпуск master с подтверждением]
    G --> P[Prod: ручное подтверждение выпуска]
```

[CI/CD и откат](docs/src/content/docs/cicd.mdx): сервер запускает готовые образы без сборки; базы и `.env` остаются на сервере.

```mermaid
flowchart LR
    A[Клиенты VK и DaData] --> P{Соединение с прокси доступно?}
    P -->|Да| T[SSH-туннель]
    T --> R[Tinyproxy в России]
    R --> S[VK или DaData]
    P -->|Нет| D[Прямое соединение из Европы]
    D --> S
    D -.->|Повтор прокси через 30–300 секунд| P
```

Необязательные `VK_PROXY_URL` и `DADATA_PROXY_URL` указывают на SSH-туннель на хосте; пустые значения включают прямые запросы. Tinyproxy слушает localhost и передаёт HTTPS через CONNECT без расшифровки. OpenSSH и systemd поддерживают туннель вне Docker.

Переход к прямому соединению происходит при ошибке подключения к прокси. Ошибки API и таймауты ожидания ответа маршрут не меняют. Безопасные повторы VK сохраняют `random_id` или тот же результат редактирования. Таймауты, повторы и интервалы восстановления задаются в `.env`; значения по умолчанию приведены в [`.env.example`](.env.example). После изменения настроек перезапустите контейнеры.

## Поиск v2

Основные GET-пути: `/api/v2/codes?address=...` и `/api/v2/codes/nearby?lat=...&lon=...`.
Версия `/api/v1` сохраняет прежний поиск; старые `/codes/*` и версионированные `/codes/msg`, `/codes/geo` совместимы.
v2 обрабатывает лишние символы, сокращения городов и номера улиц, написанные словами. Номера домов, корпусов и строений проверяются точно.
Один подходящий адрес с кодами открывается сразу; несколько — через выбор с кнопкой «Ни один не подходит».
После выбора кнопка «Назад к адресам» возвращает ту же страницу списка, в том числе после просмотра кода.
Если в ответе один подъезд, его коды показываются сразу. Кнопки подъездов нужны только для выбора из нескольких.
Восстановленный по префиксу город требует подтверждения даже при одном варианте.
Дома без кодов не заполняют меню выбора. Если конкретный дом распознан, но кодов в базе нет, бот сообщает об этом явно.
Повторные запросы ускоряет небольшой ограниченный кэш в процессе API; сохранность событий обеспечивает PostgreSQL.

```mermaid
flowchart LR
    GAR[XML ГАР: действующие дома и иерархия] --> IMP[Потоковый импорт по регионам]
    OLD[Исходная codes.db] --> N[Адреса, варианты написания и происхождение записей]
    IMP --> N
    N --> CAT[Новая БД каталога PostgreSQL]
    HISTORY[Закрытая выгрузка истории VK] --> AUDIT[Аудит и набор проверок с разделением по диалогам]
    AUDIT --> TEST[Сравнение v1 и v2 перед выпуском]
    CAT --> TEST
```

Полная сборка ГАР хранится отдельно от рабочего сервера. Компактный каталог содержит упакованные номера известных домов и отдельные записи домов с кодами.
Большие данные и исходные переписки остаются в игнорируемой папке `artifacts/`. Они не попадают в Git и контекст сборки Docker.
Каталог v2 работает в отдельной БД PostgreSQL на сервере приложения
и не зависит от компьютера разработчика. API читает его через отдельную роль с ограниченными правами;
при сбое поиска сохраняется fallback на исходную SQLite v1. Файловый каталог v2 поддержан для отката.
Подробности: [поиск и результаты проверок](docs/src/content/docs/search-v2.md), [очередь и сценарии отказов](docs/src/content/docs/message-delivery.mdx), [тестовое окружение](docs/src/content/docs/dev-stand.md).

## Требования

* Docker & Docker Compose 2.24.4+ (`!override` изолирует dev-настройки)
* Python 3.12+ для локальной разработки

## Установка

Склонируйте проект:
~~~~bash
git clone https://github.com/omkality/domofomka
cd domofomka
~~~~

В корне проекта нужны `.env` и база SQLite3 с именем из `DB_NAME`.

Основные параметры `.env`:

```dosini
# Настройки API
API_HOST=0.0.0.0
API_PORT=8000
DB_NAME=codes.db
DADATA_TOKEN=your_dadata_token_here

# Настройки бота VK
VK_GROUP_TOKEN=your_vk_group_token_here
VK_GROUP_ID=your_vk_group_id_here
VK_PROXY_URL=

# Постоянная очередь VK
EXPECTED_VK_GROUP_ID=your_vk_group_id_here
POSTGRES_PASSWORD=replace_with_long_random_password
QUEUE_DATABASE_URL=postgresql://domofomka:${POSTGRES_PASSWORD}@queue:5432/domofomka

# Каталог v2 — отдельная опубликованная БД и роль только для чтения
SEARCH_BACKEND=postgres
CATALOG_DATABASE_URL=postgresql://catalog_reader:replace_with_reader_password@queue:5432/domofomka_catalog_release
CATALOG_REVISION=replace_with_published_catalog_sha256

ANTI_SPAM_TIME=2

# Внутренний API для бота VK
DOMOFOMKA_API_HOST=api
DOMOFOMKA_API_PORT=8000
```

До запуска PostgreSQL-варианта нужно загрузить проверенный каталог и выдать роли API права чтения:
[публикация и восстановление](docs/src/content/docs/database.mdx#обновление-и-восстановление).
SQLite-вариант выбирается через `SEARCH_BACKEND=sqlite` и `SEARCH_CATALOG_PATH`.

Исходная база SQLite3 для v1 должна содержать таблицу `codes`:

~~~~sql
CREATE TABLE codes (
    "id" INTEGER PRIMARY KEY,
    "city" TEXT NOT NULL,
    "street_type" TEXT NOT NULL,
    "street" TEXT NOT NULL,
    "house" TEXT NOT NULL,
    "entrance" TEXT NOT NULL,
    "code_type" TEXT NOT NULL,
    "code" TEXT NOT NULL
);
~~~~

## Запуск

Выполните в корне проекта:

~~~~bash
docker compose up --build -d
~~~~

## API
### Поиск кодов по тексту

#### Запрос

`GET /codes/msg/`

    GET http://localhost:8000/codes/msg?message=трофимова 3

#### Ответ

~~~~json
{
"address":"Москва, улица Трофимова, дом 3",
"data":
    {
    "1":[["#7546","yaeda"],["#4230","yaeda"],["*#7546","delivery"]],
    "2":[["#4230","yaeda"],["#4230","delivery"],["К4230","oldcodes"]]
    }
}
~~~~

### Поиск кодов по координатам

`GET /codes/geo/`

    GET http://localhost:8000/codes/geo?lat=55.617586&lon=37.495482

#### Ответ

~~~~json
{
"address":"Москва, улица Профсоюзная, дом 156к5",
"data":
    {
    "1":[["255К2580","yaeda"],["12К2889","yaeda"],["28К3185","yaeda"]],
    "2":[["72К3108","yaeda"]],
    "3":[["110*4082","yaeda"],["97*4840","yaeda"],["75*6818","yaeda"]],
    "4":[["133К2489","yaeda"],["135К3001","yaeda"]],
    "5":[["170*3304","yaeda"],["151*6631","yaeda"],["173*9572","yaeda"]],
    "6":[["200К4578","yaeda"],["200К4578","delivery"]]
    }
}
~~~~

## Бот VK

### Поиск по тексту

![Поиск кодов по тексту](https://github.com/omka0708/domofomka/assets/56554057/d21e6146-95a7-4f09-a501-31d8fd2ae7df)

### Поиск по координатам

![Поиск кодов по координатам](https://github.com/omka0708/domofomka/assets/56554057/85f2b4f6-7634-4b3a-b7a7-5ae10cdc9219)

API опубликован только на localhost. Лимиты памяти и процессов контейнеров, ресурсы очереди и ротация журналов настраиваются в `.env`.

Методика и результаты сравнения поиска: [эксперименты](docs/src/content/docs/research/search-experiments.md).
Проверки значимых слов, кварталов, корпусов и совместимого fallback: [разбор исправлений](docs/src/content/docs/research/search-name-identity.mdx).
Сохранность вариантов и геопоиск: [проверка поиска](docs/src/content/docs/research/search-prefix-performance.md).
Стоимость широких запросов и обслуживание: [проверка PostgreSQL](docs/src/content/docs/catalog-street-layout.mdx).

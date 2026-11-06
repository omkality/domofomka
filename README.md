# domofomka

Application for getting the intercom code at the given address.
Used stack: *Litestar*, *Granian*, *SQLite* (legacy source and indexed read-only catalog), *PostgreSQL* (bot queue), optional *OpenSearch*, *VK API*.

## Architecture

```mermaid
flowchart LR
    VK[VK] <-->|Long Poll| B[Bot]
    B --> I[(PostgreSQL inbox)]
    H[VK history recovery] --> I
    I --> W[Workers]
    W --> A[HTTP API]
    A --> V2[API v2: query normalization]
    V2 --> C[(Catalog: street index, exact house and codes)]
    V2 --> OS[Optional OpenSearch candidates]
    OS --> C
    V2 -->|Unavailable or no match| V1[API v1: legacy search]
    V1 --> DB[(Original SQLite codes)]
    A --> D[DaData geocoding]
    W --> O[(PostgreSQL outbox)]
    O -->|Send or edit with retries| VK
```

Events and replies survive restarts. Saved button clicks can be retried; clicks never received from VK cannot be recovered from history.

```mermaid
flowchart LR
    A[VK and DaData clients] --> P{Proxy connection available?}
    P -->|Yes| T[SSH tunnel]
    T --> R[Tinyproxy in Russia]
    R --> S[VK or DaData]
    P -->|No| D[Direct connection from Europe]
    D --> S
    D -.->|Retry proxy after 30 to 300 seconds| P
```

Optional `VK_PROXY_URL` and `DADATA_PROXY_URL` point to the host-side SSH tunnel; empty values use direct connections. Tinyproxy listens on localhost and forwards HTTPS with CONNECT without decrypting it. OpenSSH and systemd maintain the tunnel outside Docker.

Only connection failures trigger direct fallback. API errors and response timeouts do not switch routes. Safe VK retries keep the same `random_id` or edit result. Timeouts, retries and recovery intervals are configurable in `.env`; see [`.env.example`](.env.example) for defaults. Restart containers after changing them.

## Search v2

`/api/v1/codes/msg` and `/api/v1/codes/geo` preserve the legacy API; `/codes/*` remains compatible.
`/api/v2/codes/*` handles noisy input, city aliases and written street numbers. House numbers and blocks remain exact.
Ambiguous addresses require a choice. A recognized house without codes gets an explicit answer.

```mermaid
flowchart LR
    GAR[GAR XML: active houses and hierarchy] --> IMP[Streaming import by region]
    OLD[Original codes.db] --> N[Canonical identities, aliases and provenance]
    IMP --> N
    N --> CAT[Read-only catalog]
    N --> IDX[Versioned OpenSearch index]
    HISTORY[Private VK history export] --> AUDIT[Aggregate audit and dialog-split regression corpus]
    AUDIT --> TEST[Compare v1 and v2 before release]
```

The full GAR build is kept offline; the serving catalog stores packed known house numbers and separate code-bearing identities.
Large datasets and raw message histories stay in ignored `artifacts/`. They never go into Git or Docker build contexts.
The compact serving catalog runs beside the German test bot; it does not depend on a development PC.
OpenSearch is optional: its candidates are merged with exact catalog candidates to protect recall. The local Docker lab supports comparative testing before moving OpenSearch to a suitably sized host.
Build and validation details: [search v2 notes](docs/search-v2.md).

## Prerequisites
* Docker & Docker Compose
* Python 3.12+ (for local development)

## Install

Build `domofomka` from source:
~~~~bash
git clone https://github.com/omka0708/domofomka
cd domofomka
~~~~

You should have `.env` file and SQLite3 database with `DB_NAME` name (configured in environment file) at the */domofomka* folder.

Environment file `.env` should contain:

```dosini
# API Configuration
API_HOST=0.0.0.0
API_PORT=8000
DB_NAME=codes.db
DADATA_TOKEN=your_dadata_token_here

# VK Bot Configuration
VK_GROUP_TOKEN=your_vk_group_token_here
VK_GROUP_ID=your_vk_group_id_here
VK_PROXY_URL=

# Durable VK queue (required for this development version)
EXPECTED_VK_GROUP_ID=your_vk_group_id_here
POSTGRES_PASSWORD=replace_with_long_random_password
QUEUE_DATABASE_URL=postgresql://domofomka:${POSTGRES_PASSWORD}@queue:5432/domofomka

CACHE_EXPIRE_TIME=1200
ANTI_SPAM_TIME=5

# API for VK Bot
DOMOFOMKA_API_HOST=api
DOMOFOMKA_API_PORT=8000
```

Your SQLite3 database should contain table `codes`, that has the structure:

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

## Run

Run this command at the working directory */domofomka*:

~~~~bash
docker compose up --build -d
~~~~

## API
### Get codes by message
#### Request

`GET /codes/msg/`

    GET http://localhost:8000/codes/msg?message=трофимова 3

#### Response

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

### Get codes by latitude and longitude

`GET /codes/geo/`

    GET http://localhost:8000/codes/geo?lat=55.617586&lon=37.495482

#### Response

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

## VK Bot
### Get codes by message
![codes_by_msg](https://github.com/omka0708/domofomka/assets/56554057/d21e6146-95a7-4f09-a501-31d8fd2ae7df)

### Get codes by latitude and longitude
![codes_by_lat_lon](https://github.com/omka0708/domofomka/assets/56554057/85f2b4f6-7634-4b3a-b7a7-5ae10cdc9219)

## Telegram Bot

*soon*

The service API is published on localhost only. Container memory, process counts, queue resources and log rotation are configurable in `.env`.

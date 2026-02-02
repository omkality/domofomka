# domofomka

Application for getting the intercom code at the given address.
Used stack: *Litestar*, *Granian*, *SQLite*, *Redis*, *VK API*.

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

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=
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

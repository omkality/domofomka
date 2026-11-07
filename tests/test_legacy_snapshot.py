import sqlite3

import pytest

from src.config import settings
from src.services.codes import get_data_from_db
from tools import evaluate_search as replay


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["перерва 62к1", "Перерва 62к1", "москва перерва 62к1", "Перерва 62к1 подъезд 2", "нет 99"]
)
async def test_fast_legacy_replay_matches_real_legacy(query, tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    rows = [
        (1, "Москва", "улица", "Перерва", "62к1", "2", "test", "A"),
        (2, "Москва", "улица", "Перерва", "62к1", "3", "test", "B"),
        (3, "Москва, тест", "улица", "Перерва", "62к1", "2", "test", "C"),
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER,city TEXT,street_type TEXT,street TEXT,"
            "house TEXT,entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany("INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)", rows)
    monkeypatch.setattr(settings, "db_name", str(path))
    replay.legacy_groups[:] = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
    replay.legacy_cache.clear()
    original = await get_data_from_db(query)
    assert replay.legacy_lookup(query) == original.get("address")

import sqlite3

import pytest

from src.api.config import settings
from src.services.codes import get_data_from_db, scan_data_from_db
from src.services.legacy_index import lookup
from tools.build_legacy_index import build


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["перерва 62к1", "Перерва 62к1", "москва перерва 62к1", "нет 99", "рва 62к1", "14", "", "улица 9 мая 1"]
)
async def test_index_preserves_codes_order_duplicates_and_legacy_matching(tmp_path, monkeypatch, query):
    source, index = tmp_path / "source.db", tmp_path / "index.db"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE codes(id INTEGER PRIMARY KEY,city TEXT,street_type TEXT,street TEXT,house TEXT,"
            "entrance TEXT,code_type TEXT,code TEXT)"
        )
        db.executemany(
            "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
            [
                (1, "Москва", "улица", "Перерва", "62к1", "2", "test", "A"),
                (2, "Москва, тест", "улица", "Перерва", "62к1", "2", "test", "B"),
                (3, "Москва", "улица", "Перерва", "62к1", "2", "test", "A"),
                (4, "Москва", "улица", "Перерва", "62к1", "3", "test", "C"),
                (5, "Другой город", "улица", "9 Мая", "1", "1", "test", "D"),
            ],
        )
    build(source, index)
    monkeypatch.setattr(settings, "db_name", str(source))
    monkeypatch.setattr(settings, "legacy_index_path", str(index))
    assert await get_data_from_db(query) == await scan_data_from_db(query)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE codes SET code='changed' WHERE id=1")
    assert lookup("перерва 62к1", str(source), str(index)) is None
    assert await get_data_from_db(query) == await scan_data_from_db(query)

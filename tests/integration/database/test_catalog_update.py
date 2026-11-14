import copy
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.code_service.services.v2.catalog_update import migrate
from tools.catalog.build_postgres_catalog import INDEXES, SCHEMA


@pytest.fixture
def patch_database(postgres_dsn):
    dsn = postgres_dsn
    name = "domofomka_catalog_build_patch_" + uuid.uuid4().hex[:12]
    info = conninfo_to_dict(dsn)
    info["dbname"] = name
    target = make_conninfo(**info)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(target) as db:
                db.execute(SCHEMA)
                db.execute(
                    "INSERT INTO buildings VALUES('legacy',NULL,'old','city','street','1','street','city','77','city')"
                )
                db.execute("INSERT INTO codes VALUES('legacy','1','src','TEST-CODE')")
                db.execute("INSERT INTO provenance VALUES(1,'legacy','unresolved')")
                for statement in INDEXES:
                    db.execute(statement)
                db.execute(
                    """INSERT INTO metadata VALUES('source_catalog_sha256',%s),('build','{}'),
                        ('catalog_schema_version','1'),('publication_state','ready')""",
                    ("a" * 64,),
                )
            yield target
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))


def payload():
    before = ["legacy", None, "old", "city", "street", "1", "street", "city", "77", "city"]
    after = ["gar", "guid", "canonical", "city", "street", "1", "street", "city", "77", "city"]
    return {
        "source_revision": "a" * 64,
        "target_revision": "b" * 64,
        "source_counts": {"buildings": 1, "codes": 1, "provenance": 1, "metadata": 1},
        "target_counts": {"buildings": 1, "codes": 1, "provenance": 1, "metadata": 2},
        "changes": {
            "buildings": [[before, None], [None, after]],
            "codes": [[["legacy", "1", "src", "TEST-CODE"], None], [None, ["gar", "1", "src", "TEST-CODE"]]],
            "provenance": [[[1, "legacy", "unresolved"], [1, "gar", "gar_street_word_order"]]],
            "metadata": [
                [["build", "{}"], ["build", '{"normalization_schema":5}']],
                [None, ["building_redirects", '{"legacy":"gar"}']],
            ],
        },
    }


def snapshot(dsn):
    with psycopg.connect(dsn) as db:
        return {
            table: db.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall()
            for table in ["buildings", "codes", "provenance", "metadata", "entrances", "code_sources"]
        }


def test_catalog_patch_is_atomic_idempotent_and_reversible_with_foreign_keys(patch_database):
    original = snapshot(patch_database)
    assert migrate(patch_database, payload())["changed"]
    assert not migrate(patch_database, payload())["changed"]
    with psycopg.connect(patch_database) as db:
        assert db.execute("SELECT building_id FROM codes").fetchone()[0] == "gar"
    assert migrate(patch_database, payload(), True)["changed"]
    assert snapshot(patch_database) == original
    with psycopg.connect(patch_database) as db:
        assert db.execute("SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'catalog_patch_%'").fetchone()[0] == 0


def test_bad_preimage_does_not_change_other_rows(patch_database):
    patch = payload()
    patch["changes"]["codes"][0][0][3] = "WRONG"
    original = snapshot(patch_database)
    with pytest.raises(ValueError, match="differs from the reviewed patch"):
        migrate(patch_database, patch)
    assert snapshot(patch_database) == original
    with psycopg.connect(patch_database) as db:
        assert db.execute("SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'catalog_patch_%'").fetchone()[0] == 0


def test_failed_postcondition_rolls_back_all_changes(patch_database):
    patch = copy.deepcopy(payload())
    patch["target_counts"]["codes"] = 2
    original = snapshot(patch_database)
    with pytest.raises(ValueError, match="row count differs"):
        migrate(patch_database, patch)
    assert snapshot(patch_database) == original
    with psycopg.connect(patch_database) as db:
        assert db.execute("SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'catalog_patch_%'").fetchone()[0] == 0

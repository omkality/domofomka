import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ConnectError
from litestar.testing import TestClient
from requests import Response, Session
from requests.exceptions import ProxyError

from src.code_service.config import settings as api_settings
from src.code_service.main import app
from src.code_service.services.geocoding import DadataClient
from src.code_service.services.v2 import search
from src.code_service.services.v2.engine import Catalog
from src.infrastructure.network import network_settings
from src.infrastructure.proxy import proxy_health
from src.infrastructure.telemetry import Capture, merge, note, search_result, settings, span
from src.vkbot import telemetry as database
from src.vkbot.http import TimeoutSession
from src.vkbot.maintenance import compact
from src.vkbot.queue import event_record


def test_async_threads_share_capture_but_preserve_parentage_and_errors():
    def threaded():
        with span("thread"):
            raise TimeoutError("secret must not enter diagnostics")

    async def run():
        with span("root"), pytest.raises(TimeoutError):
            await asyncio.to_thread(threaded)

    with Capture() as capture:
        asyncio.run(run())
    child, parent = capture.rows
    assert child["parent_id"] == parent["id"]
    assert child["status"] == "error"
    assert child["error_type"] == "TimeoutError"
    assert "secret" not in json.dumps(capture.packet())
    assert parent["duration_ms"] >= child["duration_ms"] >= 0


def test_capture_limits_and_disabled_measurements(monkeypatch):
    monkeypatch.setattr(settings, "telemetry_max_spans", 8)
    with Capture() as capture:
        for _ in range(20):
            with span("bounded"):
                pass
        note(too_large="x" * 5000)
        note(not_serializable=object())
    assert len(capture.rows) == 8
    assert capture.dropped == 14
    with Capture(enabled=False) as disabled, span("not_recorded"):
        note(ignored=True)
    assert disabled.rows == []
    assert disabled.notes == {}


def test_result_snapshot_never_copies_codes_and_merge_marks_api_service():
    result = search_result({"status": "found", "data": {"1": [["secret-code", "source"]]}, "address": "House"})
    assert "secret-code" not in json.dumps(result)
    with Capture() as remote, span("remote"):
        pass
    with Capture() as capture, span("client"):
        merge(remote.packet())
    assert capture.rows[0]["service"] == "api"
    assert capture.rows[0]["parent_id"] == capture.rows[1]["id"]


def test_entire_packet_including_notes_respects_byte_limit(monkeypatch):
    monkeypatch.setattr(settings, "telemetry_max_packet_bytes", 4096)
    with Capture() as capture:
        for _ in range(20):
            with span("bounded", detail="x" * 200):
                pass
        note(summary="y" * 3000)
    packet = capture.packet()
    assert len(json.dumps(packet, ensure_ascii=False).encode()) <= 4096
    assert packet["dropped"] > 0


@pytest.fixture
def observed_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    source = tmp_path / "source.db"
    source.touch()
    catalog = tmp_path / "catalog.db"
    catalog.touch()
    monkeypatch.setattr(api_settings, "db_name", str(source))
    monkeypatch.setattr(api_settings, "search_catalog_path", str(catalog))
    monkeypatch.setattr(api_settings, "legacy_index_path", str(tmp_path / "absent-index.db"))
    monkeypatch.setattr(api_settings, "opensearch_url", None)
    monkeypatch.setattr(api_settings, "search_cache_ttl_seconds", 120)
    search.result_cache.configure(0, 1, 1024)
    yield
    search.result_cache.configure(0, 1, 1024)


def test_api_cache_does_not_fake_new_search_or_reuse_old_measurements(observed_api, monkeypatch):
    resolve = MagicMock(
        return_value={"status": "found", "engine": "v2", "address": "House", "data": {"1": [["CODE", "test"]]}}
    )
    monkeypatch.setattr(Catalog, "resolve", resolve)
    with TestClient(app) as client:
        first = client.get("/api/v2/codes/msg", params={"message": "test", "observe": "true"}).json()
        second = client.get("/api/v2/codes/msg", params={"message": "test", "observe": "true"}).json()
        plain = client.get("/api/v2/codes/msg", params={"message": "test"}).json()
    assert resolve.call_count == 1
    assert any(s["stage"] == "search.v2" for s in first["_diagnostics"]["spans"])
    assert not any(s["kind"] == "search" for s in second["_diagnostics"]["spans"])
    assert second["_diagnostics"]["notes"]["cache_hit"] is True
    assert "CODE" not in json.dumps(first["_diagnostics"])
    assert "_diagnostics" not in plain
    first.pop("_diagnostics")
    second.pop("_diagnostics")
    assert first == second == plain


def test_api_records_v2_error_and_actual_v1_result(observed_api, monkeypatch):
    from src.code_service.services.v1 import codes

    monkeypatch.setattr(Catalog, "resolve", MagicMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(
        codes,
        "scan_data_from_db",
        AsyncMock(return_value={"address": "Москва, улица Перерва, дом 62к1", "data": {"2": [["CODE", "test"]]}}),
    )
    with TestClient(app) as client:
        response = client.get("/api/v2/codes/msg", params={"message": "Перерва 62к1", "observe": "true"})
    data = response.json()
    assert response.status_code == 200
    assert data["engine"] == "v1"
    runs = [s for s in data["_diagnostics"]["spans"] if s["kind"] == "search"]
    assert [(s["attributes"]["engine"], s["status"]) for s in runs] == [("v2", "error"), ("v1", "success")]
    assert runs[1]["attributes"]["result"]["status"] == "found"
    assert data["_diagnostics"]["notes"]["search_fallback_reason"] == "unavailable"


def test_api_failed_geo_keeps_diagnostic_packet(observed_api, monkeypatch):
    from src.code_service.services.geocoding import DadataUnavailable

    geocoder = MagicMock()
    geocoder.get_address_by_geo = AsyncMock(side_effect=DadataUnavailable("offline"))
    monkeypatch.setattr(
        "src.code_service.services.geocoding.DadataClient.get_address_by_geo", geocoder.get_address_by_geo
    )
    with TestClient(app) as client:
        response = client.get("/api/v2/codes/geo", params={"lat": 1, "lon": 2, "observe": "true"})
    assert response.status_code == 503
    assert any(s["stage"] == "geo.resolve" and s["status"] == "error" for s in response.json()["_diagnostics"]["spans"])


def test_dadata_attempts_record_proxy_failure_then_direct_success(monkeypatch):
    proxy_health.routes.clear()
    monkeypatch.setattr(DadataClient, "_unavailable_until", 0)
    request = AsyncMock(side_effect=[ConnectError("secret proxy"), {"suggestions": []}])
    monkeypatch.setattr(DadataClient, "_request", request)
    with Capture() as capture:
        asyncio.run(DadataClient("SECRET", proxy_url="http://user:password@127.0.0.1:9999").geolocate(1, 2))
    assert [(s["attributes"]["route"], s["status"]) for s in capture.rows] == [
        ("ru_proxy", "error"),
        ("direct", "success"),
    ]
    assert capture.rows[1]["attributes"]["route_reason"] == "proxy_connect_failed"
    assert "password" not in json.dumps(capture.packet())
    assert "SECRET" not in json.dumps(capture.packet())
    proxy_health.routes.clear()


def test_vk_attempts_record_proxy_switch_and_provider_error(monkeypatch):
    proxy_health.routes.clear()
    monkeypatch.setattr(network_settings, "vk_retry_delay_seconds", 0)
    response = Response()
    response.status_code = 200
    response._content = b'{"error":{"error_code":6}}'
    with Capture() as capture, patch.object(Session, "request", side_effect=[ProxyError("password"), response]):
        TimeoutSession("http://127.0.0.1:9999").get("https://api.vk.com/method/groups.isMember")
    attempts = [s for s in capture.rows if s["kind"] == "network"]
    assert [s["attributes"]["route"] for s in attempts] == ["ru_proxy", "direct"]
    assert attempts[1]["attributes"]["provider_error_code"] == "6"
    assert "password" not in json.dumps(capture.packet())
    proxy_health.routes.clear()


def incoming(store):
    return event_record(
        store.group_id,
        "message_new",
        {"message": {"id": 1, "peer_id": 10, "from_id": 10, "text": "query", "date": time.time()}},
    )


def test_automatic_code_is_logged_as_code_without_button_context(observed_store):
    store = observed_store
    store.ingest([incoming(store)])
    store.plan(
        store.claim("bot_inbox"),
        [{"method": "send", "params": {"peer_id": 10, "message": "House and codes"}, "automatic_code": True}],
    )
    store.finish(store.claim("bot_outbox"))
    with store.connect() as conn:
        outcome = conn.execute("SELECT outcome FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()[
            "outcome"
        ]
    assert outcome["automatic_code"] is True
    assert outcome["effects"][0]["role"] == "code"
    assert outcome["delivery_status"] == "accepted_by_vk"


def test_postgres_keeps_attempts_partial_delivery_and_survives_queue_cleanup(observed_store):
    store = observed_store
    store.ingest([incoming(store)])
    job = store.claim("bot_inbox")
    with Capture() as capture, span("search.v1", kind="search", engine="v1") as detail:
        detail["result"] = {"status": "found", "address": "House"}
    database.persist(store, "bot_inbox", job, capture.packet(), "development")
    database.persist(store, "bot_inbox", job, capture.packet(), "development")
    store.plan(
        job,
        [
            {"method": "send", "params": {"peer_id": 10, "message": "first"}},
            {"method": "send", "params": {"peer_id": 10, "message": "second"}},
        ],
    )
    first = store.claim("bot_outbox")
    store.finish(first)
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()
        assert row["outcome"]["delivery_status"] == "partial"
        assert row["outcome"]["outbox_done"] == 1
        summary = conn.execute("SELECT * FROM request_summary WHERE id=%s", (row["id"],)).fetchone()
        assert summary["v1_executed"] is True
        assert summary["v2_executed"] is False
        assert summary["v1_result"] == "found"
        assert summary["v2_result"] is None
        assert summary["v1_total_ms"] >= 0
        assert summary["v2_total_ms"] is None
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM search_runs s JOIN request_spans p ON p.id=s.span_id WHERE p.request_id=%s",
                (row["id"],),
            ).fetchone()["n"]
            == 1
        )
    second = store.claim("bot_outbox")
    store.finish(second)
    with store.connect() as conn:
        conn.execute("UPDATE bot_inbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
        conn.execute("UPDATE bot_outbox SET completed_at=1 WHERE group_id=%s", (store.group_id,))
    compact(store, 10, 10, 10)
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()
        assert row["outcome"]["delivery_status"] == "accepted_by_vk"
        assert row["outcome"]["outbox_done"] == 2
        assert (
            conn.execute("SELECT count(*) AS n FROM bot_inbox WHERE group_id=%s", (store.group_id,)).fetchone()["n"]
            == 0
        )


def test_postgres_analytics_error_does_not_rollback_ingest(observed_store, monkeypatch):
    monkeypatch.setattr(database, "refresh", MagicMock(side_effect=RuntimeError("analytics failed")))
    assert observed_store.ingest([incoming(observed_store)]) == 1
    assert observed_store.claim("bot_inbox") is not None


def test_postgres_budget_does_not_block_queue_and_size_eviction_cascades(observed_store, monkeypatch):
    store = observed_store
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", 1)
    assert store.ingest([incoming(store)]) == 1
    with store.connect() as conn:
        assert not conn.execute("SELECT id FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", 33554432)
    job = store.claim("bot_inbox")
    with Capture() as capture, span("search.v2", kind="search", engine="v2"):
        pass
    database.persist(store, "bot_inbox", job, capture.packet(), "development")
    store.plan(job, [])
    with store.connect() as conn:
        rid = database.identity(store.group_id, job["id"])
        conn.execute("UPDATE request_log SET updated_at=to_timestamp(1) WHERE id=%s", (rid,))
    # Old history survives indefinitely while there is room in the budget.
    assert database.maintain(store, 10)["requests_removed"] == 0
    with store.connect() as conn:
        assert conn.execute("SELECT id FROM request_spans WHERE request_id=%s", (rid,)).fetchone()
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", 1)
    assert database.maintain(store, 10)["requests_removed"] == 1
    with store.connect() as conn:
        assert not conn.execute("SELECT id FROM request_spans WHERE request_id=%s", (rid,)).fetchone()


def test_full_budget_replaces_old_completed_diagnostics_without_removing_queue(observed_store, monkeypatch):
    store = observed_store
    store.ingest([incoming(store)])
    job = store.claim("bot_inbox")
    store.plan(job, [])
    with Capture() as capture, span("finished"):
        pass
    database.persist(store, "bot_inbox", job, capture.packet(), "development")
    with store.connect() as conn:
        used = database.budget_used(conn, store.group_id)
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", used + 2000)
    second = incoming(store)
    second["event_key"] += ":second"
    assert store.ingest([second]) == 1
    with store.connect() as conn:
        assert not conn.execute(
            "SELECT id FROM request_log WHERE id=%s", (database.identity(store.group_id, job["id"]),)
        ).fetchone()
        assert conn.execute("SELECT status FROM bot_inbox WHERE id=%s", (job["id"],)).fetchone()["status"] == "done"
        assert (
            conn.execute("SELECT count(*) AS n FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()["n"]
            == 1
        )
        assert not conn.execute(
            "SELECT id FROM request_spans WHERE request_id=%s", (database.identity(store.group_id, job["id"]),)
        ).fetchone()


def test_budget_never_evicts_diagnostics_of_unfinished_work(observed_store, monkeypatch):
    store = observed_store
    store.ingest([incoming(store)])
    with store.connect() as conn:
        original = conn.execute("SELECT id FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()["id"]
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", 1)
    with store.connect() as conn:
        conn.execute("UPDATE request_log SET updated_at=to_timestamp(1) WHERE id=%s", (original,))
    assert database.maintain(store, 10)["requests_removed"] == 0
    monkeypatch.setattr(settings, "telemetry_max_storage_bytes", 4000)
    second = incoming(store)
    second["event_key"] += ":second"
    assert store.ingest([second]) == 1
    with store.connect() as conn:
        assert conn.execute("SELECT id FROM request_log WHERE id=%s", (original,)).fetchone()
        assert (
            conn.execute("SELECT count(*) AS n FROM bot_inbox WHERE group_id=%s", (store.group_id,)).fetchone()["n"]
            == 2
        )


def test_sql_timeout_in_diagnostics_preserves_queue_transaction(observed_store, monkeypatch):
    store = observed_store
    monkeypatch.setattr(settings, "telemetry_db_timeout_ms", 10)

    def slow(conn, *args: object):
        conn.execute("SELECT pg_sleep(0.05)")

    monkeypatch.setattr(database, "refresh", slow)
    assert store.ingest([incoming(store)]) == 1
    with store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM bot_inbox WHERE group_id=%s", (store.group_id,)).fetchone()["status"]
            == "pending"
        )
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15s"


def test_missing_api_measurements_stay_partial_after_successful_delivery(observed_store):
    store = observed_store
    store.ingest([incoming(store)])
    job = store.claim("bot_inbox")
    with Capture() as capture:
        note(api_measurements="missing")
    database.persist(store, "bot_inbox", job, capture.packet(), "development")
    store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "answer"}}])
    outgoing = store.claim("bot_outbox")
    store.finish(outgoing)
    with Capture() as delivered, span("outbox.deliver"):
        pass
    database.persist(store, "bot_outbox", outgoing, delivered.packet(), "development")
    with store.connect() as conn:
        row = conn.execute(
            "SELECT telemetry_state,outcome FROM request_log WHERE group_id=%s", (store.group_id,)
        ).fetchone()
        assert row["telemetry_state"] == "partial"
        assert row["outcome"]["delivery_status"] == "accepted_by_vk"


def test_skipped_delivery_is_not_counted_as_vk_acceptance(observed_store):
    store = observed_store
    store.ingest([incoming(store)])
    job = store.claim("bot_inbox")
    store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "obsolete"}}])
    outgoing = store.claim("bot_outbox")
    store.finish(outgoing)
    with Capture() as capture:
        note(decision="delivery_obsolete")
    database.persist(store, "bot_outbox", outgoing, capture.packet(), "development")
    with store.connect() as conn:
        row = conn.execute("SELECT outcome FROM request_log WHERE group_id=%s", (store.group_id,)).fetchone()["outcome"]
        assert row["outbox_done"] == 0
        assert row["outbox_skipped"] == 1
        assert row["delivery_status"] == "cancelled"


def test_schema_failure_disables_only_telemetry(observed_store, monkeypatch):
    store = observed_store

    def unavailable(conn):
        conn.execute("THIS IS NOT SQL")

    monkeypatch.setattr(database, "verify_schema", unavailable)
    store.initialize()
    assert store.telemetry is False
    assert store.ingest([incoming(store)]) == 1


def test_maintenance_repairs_state_when_process_dies_before_diagnostic_flush(observed_store):
    store = observed_store
    store.ingest([incoming(store)])
    job = store.claim("bot_inbox")
    with Capture():
        store.plan(job, [{"method": "send", "params": {"peer_id": 10, "message": "answer"}}])
    # Simulate losing the capture after commit: no persist() call.
    outgoing = store.claim("bot_outbox")
    with Capture():
        store.finish(outgoing)
    database.maintain(store, 10)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT telemetry_state,outcome FROM request_log WHERE group_id=%s", (store.group_id,)
        ).fetchone()
        assert row["telemetry_state"] == "partial"
        assert row["outcome"]["delivery_status"] == "accepted_by_vk"

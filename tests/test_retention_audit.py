import json
from datetime import UTC, datetime

from tools.audit_retention import audit


def test_history_volume_deduplicates_overlapping_exports_and_counts_empty_days(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    message = {"id": 1, "date": int(datetime(2026, 11, 6, tzinfo=UTC).timestamp()), "out": 0, "text": "secret"}
    for root in roots:
        root.mkdir()
        (root / "conversations.json").write_text(json.dumps({"group": 123}))
        (root / ("a" * 20 + ".json")).write_text(json.dumps({"messages": [message]}))
    report = audit(roots)
    assert report["incoming"] == 1
    assert report["last_30"]["incoming"] == 1
    assert report["last_30"]["daily_average"] == 0.03
    assert "secret" not in json.dumps(report)

"""Оценить объём входящих по выгрузкам VK, не публикуя тексты и идентификаторы."""

import argparse
import collections
import json
import re
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path


def audit(roots: list[Path]) -> dict:
    seen, dialogs = set(), set()
    daily, outgoing = collections.Counter(), collections.Counter()
    first, last, sizes = None, None, []
    groups = set()
    for root in roots:
        metadata = root / "conversations.json"
        if metadata.exists():
            groups.add(json.loads(metadata.read_text(encoding="utf-8"))["group"])
        for path in root.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{20}", path.stem):
                continue
            for message in json.loads(path.read_text(encoding="utf-8")).get("messages", []):
                key = (path.stem, message.get("id", message.get("conversation_message_id")))
                if key in seen:
                    continue
                seen.add(key)
                stamp = message["date"]
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
                day = datetime.fromtimestamp(stamp, UTC).date()
                if message.get("out"):
                    outgoing[day] += 1
                else:
                    daily[day] += 1
                    dialogs.add(path.stem)
                    sizes.append(len(json.dumps(message, ensure_ascii=False).encode()))
    if len(groups) > 1 or first is None:
        raise ValueError("Use non-empty exports of one community")
    end = datetime.fromtimestamp(last, UTC).date()
    report = {
        "messages": len(seen),
        "incoming": sum(daily.values()),
        "outgoing": sum(outgoing.values()),
        "dialogs_with_incoming": len(dialogs),
        "from": str(datetime.fromtimestamp(first, UTC).date()),
        "through": str(end),
    }
    for days in (30, 90, 365):
        values = sorted(daily[end - timedelta(days=i)] for i in range(days))
        report[f"last_{days}"] = {
            "incoming": sum(values),
            "daily_average": round(statistics.mean(values), 2),
            "daily_p95": values[int(days * 0.95)],
            "daily_max": max(values),
        }
    report["incoming_payload_avg_bytes"] = round(statistics.mean(sizes)) if sizes else None
    report["note"] = "История API не включает callback-нажатия и редактирования как отдельные сообщения."
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.history)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result))

"""Прочитать новые переписки после исходной выгрузки для отдельной проверки качества."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values
from vk_api import VkApi
from vk_api.exceptions import ApiError

from src.vkbot.http import TimeoutSession


def export(env: Path, baseline: Path, output: Path, group: int) -> dict:
    if output.exists():
        raise FileExistsError("Preserve the frozen cohort; select a new output directory")
    config = dotenv_values(env)
    if int(config["VK_GROUP_ID"]) != group:
        raise ValueError("Unexpected community")
    since = 0
    for path in baseline.glob("*.json"):
        if path.name == "conversations.json":
            continue
        for message in json.loads(path.read_text(encoding="utf8")).get("messages", []):
            since = max(since, message.get("date", 0))
    if not since:
        raise ValueError("Baseline history has no timestamps")
    until = int(time.time())
    session = TimeoutSession(vk_proxy_url="")
    vk = VkApi(token=config["VK_GROUP_TOKEN"], api_version="5.199", session=session)

    def call(method: str, params: dict):
        assert method in {"messages.getConversations", "messages.getHistory"}
        for attempt in range(5):
            time.sleep(1)
            try:
                return vk.method(method, params)
            except ApiError as exc:
                if exc.code not in {6, 9, 10, 29}:
                    raise RuntimeError(f"VK read error code={exc.code}") from None
            except Exception:
                if attempt == 4:
                    raise RuntimeError("VK history temporarily unavailable") from None
            time.sleep(min(15, 3 * 2**attempt))
        raise RuntimeError("VK read rate limit persists")

    try:
        peers = set()
        offset = 0
        while True:
            page = call("messages.getConversations", {"count": 200, "offset": offset, "filter": "all"})
            items = page.get("items", [])
            peers.update(
                i["conversation"]["peer"]["id"]
                for i in items
                if i["conversation"]["peer"]["type"] == "user" and i.get("last_message", {}).get("date", 0) > since
            )
            offset += len(items)
            if not items or offset >= page.get("count", 0):
                break
        output.mkdir(parents=True)
        messages = 0
        incoming = 0
        for index, peer in enumerate(sorted(peers)):
            rows = []
            offset = 0
            while True:
                page = call("messages.getHistory", {"peer_id": peer, "count": 200, "offset": offset})
                items = page.get("items", [])
                for message in items:
                    if not since < message.get("date", 0) <= until:
                        continue
                    row = {
                        k: message[k] for k in ("id", "conversation_message_id", "date", "out", "text") if k in message
                    }
                    if message.get("reply_message"):
                        row["reply_message"] = {
                            k: message["reply_message"][k] for k in ("id", "text") if k in message["reply_message"]
                        }
                    rows.append(row)
                offset += len(items)
                if not items or offset >= page.get("count", 0) or min(i.get("date", 0) for i in items) <= since:
                    break
            pseudonym = hashlib.sha256(f"{group}:{peer}".encode()).hexdigest()[:20]
            (output / (pseudonym + ".json")).write_text(
                json.dumps({"dialog": pseudonym, "messages": rows}, ensure_ascii=False), encoding="utf8"
            )
            messages += len(rows)
            incoming += sum(not r.get("out") and bool(r.get("text", "").strip()) for r in rows)
            if index % 5 == 0:
                print(json.dumps({"dialogs_read": index + 1, "dialogs_total": len(peers)}), flush=True)
        report = {
            "group": group,
            "snapshot_at": until,
            "since": since,
            "dialogs": len(peers),
            "messages": messages,
            "incoming_text": incoming,
            "search_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "search_source_sha256": hashlib.sha256(Path("src/search/engine.py").read_bytes()).hexdigest(),
            "note": "Новые записи после исходной выгрузки; чтение не отправляет сообщения и не меняет отметку прочтения. Снимок API не атомарный.",
        }
        (output / "conversations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        return report
    finally:
        session.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", type=Path, required=True)
    p.add_argument("--group-id", type=int, required=True)
    p.add_argument("--baseline", type=Path, default=Path("artifacts/private/vk-prod"))
    p.add_argument("--output", type=Path, default=Path("artifacts/private/fresh-cohort"))
    a = p.parse_args()
    print(json.dumps(export(a.env, a.baseline, a.output, a.group_id)), flush=True)

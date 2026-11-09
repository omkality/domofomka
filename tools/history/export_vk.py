"""Read-only, resumable export. Raw history is private and never belongs in Git."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from dotenv import dotenv_values
from vk_api import VkApi
from vk_api.exceptions import ApiError

from src.vkbot.http import TimeoutSession


def export(env: str, target: Path, interval: float) -> None:
    config = dotenv_values(env)
    group = int(config["VK_GROUP_ID"])
    target.mkdir(parents=True, exist_ok=True)
    vk = VkApi(token=config["VK_GROUP_TOKEN"], api_version="5.199", session=TimeoutSession(vk_proxy_url=""))

    def call(method, params):
        assert method in {"messages.getConversations", "messages.getHistory", "execute"}
        for attempt in range(8):
            time.sleep(interval * (params.pop("_weight", 1)))
            try:
                return vk.method(method, params)
            except ApiError as exc:
                if exc.code not in {6, 9, 10, 29}:
                    raise RuntimeError(f"VK export error code={exc.code}") from None
                time.sleep(min(120, 2**attempt * 3))
            except Exception:
                if attempt == 7:
                    raise RuntimeError("VK export network unavailable") from None
                time.sleep(min(60, 2**attempt))
        raise RuntimeError("VK export rate limit persists")

    manifest = target / "conversations.json"
    if not manifest.exists():
        peers, offset = [], 0
        while True:
            response = call("messages.getConversations", {"count": 200, "offset": offset, "filter": "all"})
            items = response.get("items", [])
            peers.extend(i["conversation"]["peer"]["id"] for i in items if i["conversation"]["peer"]["type"] == "user")
            offset += len(items)
            if not items or offset >= response["count"]:
                break
        manifest.write_text(
            json.dumps({"group": group, "peers": sorted(set(peers)), "snapshot_at": int(time.time())}), encoding="utf8"
        )
    data = json.loads(manifest.read_text(encoding="utf8"))
    assert data["group"] == group
    pending = [
        peer
        for peer in data["peers"]
        if not (target / (hashlib.sha256(f"{group}:{peer}".encode()).hexdigest()[:20] + ".json")).exists()
    ]
    prefetched = {}
    for index, peer in enumerate(pending):
        if index % 10 == 0:
            batch = pending[index : index + 10]
            code = (
                "return ["
                + ",".join("API.messages.getHistory(" + json.dumps({"peer_id": p, "count": 200}) + ")" for p in batch)
                + "];"
            )
            result = call("execute", {"code": code, "_weight": len(batch)})
            prefetched = dict(zip(batch, result, strict=False)) if isinstance(result, list) else {}
        pseudonym = hashlib.sha256(f"{group}:{peer}".encode()).hexdigest()[:20]
        path = target / (pseudonym + ".json")
        if path.exists():
            continue
        offset, messages = 0, []
        while True:
            response = prefetched.pop(peer, None) if offset == 0 else None
            if not isinstance(response, dict):
                response = call("messages.getHistory", {"peer_id": peer, "count": 200, "offset": offset})
            items = response.get("items", [])
            # Keep only analysis fields, never profile/contact/attachment blobs.
            messages.extend(
                {k: m[k] for k in ("id", "conversation_message_id", "date", "out", "text", "reply_message") if k in m}
                for m in items
            )
            offset += len(items)
            if not items or offset >= response["count"]:
                break
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"dialog": pseudonym, "messages": messages}, ensure_ascii=False), encoding="utf8")
        temp.replace(path)
        if index % 25 == 0:
            print(json.dumps({"dialogs_done": index + 1, "dialogs_total": len(data["peers"])}), flush=True)
    print(json.dumps({"complete": True, "dialogs": len(data["peers"])}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", default=".env")
    p.add_argument("--output", type=Path, default=Path("artifacts/private/vk"))
    p.add_argument("--interval", type=float, default=0.45)
    a = p.parse_args()
    export(a.env, a.output, max(a.interval, 0.35))

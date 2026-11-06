"""Incrementally reconcile VK message history with the durable inbox."""

import time
from typing import Any

from src.vkbot.queue import QueueStore, event_record


class Reconciler:
    def __init__(self, vk: Any, store: QueueStore, settings: Any):
        self.vk = vk
        self.store = store
        self.settings = settings

    def run(self) -> int:
        upper = time.time()
        tracking = self.store.get_state("tracking_started", upper)
        watermark = self.store.get_state("reconcile_watermark")
        lower = (
            watermark - self.settings.recovery_overlap_seconds
            if watermark is not None
            else tracking - self.settings.recovery_bootstrap_seconds
        )
        added = 0
        offset = 0
        while True:
            page = self.vk.messages.getConversations(count=200, offset=offset)
            items = page.get("items", [])
            for item in items:
                conversation = item["conversation"]
                if conversation["peer"]["type"] != "user":
                    continue
                if item.get("last_message", {}).get("date", 0) < lower:
                    continue
                peer = conversation["peer"]["id"]
                history = self._history(peer, lower)
                replies = {m["reply_message"]["id"] for m in history if m.get("out") and m.get("reply_message")}
                last_out = max((m["date"] for m in history if m.get("out")), default=0)
                records = []
                for message in sorted(history, key=lambda m: (m["date"], m.get("conversation_message_id", m["id"]))):
                    if message.get("out") or not lower <= message["date"] <= upper:
                        continue
                    if message.get("from_id") != peer:
                        continue
                    record = event_record(self.store.group_id, "message_new", {"message": message})
                    record["payload"]["_event_source"] = "history"
                    # Pre-installation messages have no local processing ledger.
                    # Only their trailing unanswered tail is safe to replay.
                    historical_answer = message["date"] < tracking and message["date"] <= last_out
                    if historical_answer or message["id"] in replies:
                        record["status"] = "done"
                    records.append(record)
                added += self.store.ingest(records)
            offset += len(items)
            if (
                not items
                or offset >= page.get("count", 0)
                or min(item.get("last_message", {}).get("date", 0) for item in items) < lower
            ):
                break
        # Failed or partial scans must never advance this checkpoint.
        self.store.set_state("reconcile_watermark", upper)
        return added

    def _history(self, peer: int, lower: float) -> list[dict]:
        offset = 0
        messages = []
        while True:
            page = self.vk.messages.getHistory(peer_id=peer, count=200, offset=offset)
            batch = page.get("items", [])
            messages.extend(m for m in batch if m["date"] >= lower)
            offset += len(batch)
            if not batch or offset >= page.get("count", 0) or min(m["date"] for m in batch) < lower:
                return messages

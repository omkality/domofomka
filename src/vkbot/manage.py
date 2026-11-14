"""Operator commands; credentials come from the environment, never arguments."""

import argparse
import json
import sys

from src.infrastructure.telemetry import settings as telemetry_settings
from src.vkbot.config import settings
from src.vkbot.queue import QueueStore
from src.vkbot.runtime import validate_environment


def main() -> None:
    """Operator commands; credentials come from the environment, never arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["status", "health", "telemetry", "pause", "resume", "retry-failed", "fault"]
    )
    parser.add_argument("name", nargs="?", choices=["api", "vk-send", "vk-send-after", "vk-edit", "vk-edit-after"])
    parser.add_argument("count", nargs="?", type=int, default=1)
    args = parser.parse_args()
    validate_environment(settings)
    store = QueueStore(
        settings.queue_database_url,
        int(settings.vk_group_id),
        settings.queue_lease_seconds,
        telemetry=telemetry_settings.telemetry_enabled,
    )
    store.environment = settings.app_environment
    store.initialize()
    if args.command == "telemetry":
        if not store.telemetry:
            parser.error("Telemetry is disabled")
        from src.vkbot.telemetry import report

        sys.stdout.write(json.dumps(report(store), ensure_ascii=False, default=str) + "\n")
    elif args.command == "health":
        from src.vkbot.maintenance import health

        sys.stdout.write(json.dumps(health(store)) + "\n")
    elif args.command == "status":
        sys.stdout.write(json.dumps(store.stats(), ensure_ascii=False, default=str) + "\n")
    elif args.command in {"pause", "resume"}:
        store.set_state("paused", args.command == "pause")
    elif args.command == "retry-failed":
        store.retry_failed(settings.geo_retry_max_age_seconds)
    elif args.command == "fault":
        if not settings.dev_faults_enabled or args.name is None:
            parser.error("Fault injection is disabled or no fault name provided")
        if args.count < -1:
            parser.error("Fault count must be -1 (persistent), 0 (off), or positive")
        store.set_state("fault:" + args.name, args.count)


if __name__ == "__main__":
    main()

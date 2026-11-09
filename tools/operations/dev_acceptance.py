"""Ограниченная приёмочная проверка dev на хосте VDS, без отправки сообщений VK.

В stdin — JSON со списком queries; stdout — закрытый отчёт, содержащий адреса.
Не меняет .env, БД, контейнеры, VPN или прокси. Python 3.10+, только stdlib.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def inspect():
    result = {}
    for service in ("api", "vkbot", "queue"):
        item = json.loads(subprocess.check_output(["docker", "inspect", f"domofomka-dev-{service}-1"]))[0]
        result[service] = {
            "id": item["Id"],
            "started": item["State"]["StartedAt"],
            "oom": item["State"]["OOMKilled"],
            "running": item["State"]["Running"],
            "restarts": item["RestartCount"],
        }
    return result


def lookup(query, version):
    start = time.monotonic()
    with urlopen(
        "http://127.0.0.1:18000/api/" + version + "/codes/msg?" + urlencode({"message": query}), timeout=15
    ) as response:
        body = json.load(response)
    return (
        {
            "status": body.get("status"),
            "engine": body.get("engine"),
            "address": body.get("address"),
            "candidates": [c["address"] for c in body.get("candidates", [])],
        }
        if version == "v2"
        else {"addresses": [body["address"]] if body.get("address") else []}
    ), round((time.monotonic() - start) * 1000, 3)


def probe(service, variable, route, code):
    # None оставляет рабочую настройку; переопределение действует лишь у docker exec.
    start = time.monotonic()
    wrapped = (
        "import json\nfrom src.infrastructure.telemetry import Capture\nwith Capture() as capture:\n try:\n  exec("
        + repr(code)
        + ")\n except Exception as error:\n  print(type(error).__name__)\n  raise SystemExit(1)\n"
        "print(json.dumps({'attempts': [dict(stage=r['stage'], status=r['status'], "
        "milliseconds=r['duration_ms'], **{k:v for k,v in r['attributes'].items() "
        "if k in {'route','route_reason','attempt','http_status','operation'}}) "
        "for r in capture.rows if r['kind']=='network']}))"
    )
    error_type = None
    attempts = []
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                *(["-e", variable + "=" + route] if route is not None else []),
                f"domofomka-dev-{service}-1",
                "uv",
                "run",
                "--frozen",
                "python",
                "-c",
                wrapped,
            ],
            check=True,
            timeout=75,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        attempts = json.loads(result.stdout.splitlines()[-1])["attempts"]
        outcome = "success"
    except subprocess.CalledProcessError as error:
        outcome = "failed"
        error_type = error.stdout.strip() if error.stdout and error.stdout.strip().isidentifier() else "UnknownError"
    except subprocess.TimeoutExpired:
        outcome = "failed_or_timed_out"
        error_type = "TimeoutExpired"
    return {
        "outcome": outcome,
        "error_type": error_type,
        "milliseconds": round((time.monotonic() - start) * 1000, 3),
        "attempts": attempts,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/root/domofomka-dev"))
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    if not 1 <= args.samples <= 600 or not 0.5 <= args.interval <= 10:
        raise ValueError("Use bounded dev-only load")
    environment = dict(
        line.split("=", 1)
        for line in (args.root / ".env.dev").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    if environment.get("VK_GROUP_ID") != "223949627":
        raise ValueError("Dev community required")
    queries = json.load(sys.stdin)["queries"]
    if len(queries) > 200 or any(not isinstance(q, str) or len(q) > 500 for q in queries):
        raise ValueError("Invalid bounded cohort")
    before = inspect()
    results = []
    for query in queries:
        v1, t1 = lookup(query, "v1")
        v2, t2 = lookup(query, "v2")
        results.append({"query": query, "v1": v1, "v2": v2, "v1_ms": t1, "v2_ms": t2})
    timings = []
    resources = []
    cases = ["мичурина 1а", "мск Перерва 62к1", "СПБ Плесецкая 10", "Томск Профсоюзная 15к3"]
    for index in range(args.samples):
        _, elapsed = lookup(cases[index % 4] + (" " * (index + 10) if index % 2 else ""), "v2")
        timings.append(elapsed)
        if index % 15 == 0:
            resources.append(
                subprocess.check_output(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.Name}} {{.MemUsage}}",
                        *[f"domofomka-dev-{s}-1" for s in before],
                    ],
                    text=True,
                ).splitlines()
            )
        time.sleep(args.interval)
    dadata = (
        "import asyncio; from src.code_service.geocoding import DadataClient; from src.code_service.config import settings; "
        "c=DadataClient(settings.dadata_token,proxy_url=settings.dadata_proxy_url); "
        "assert asyncio.run(c.get_address_by_geo(lat=55.617586,lon=37.495482))"
    )
    vk = (
        "from src.vkbot.runtime import Runtime; from src.vkbot.config import settings; Runtime(settings).verify_token()"
    )
    routes = {}
    for service, variable, code in [("api", "DADATA_PROXY_URL", dadata), ("vkbot", "VK_PROXY_URL", vk)]:
        for label, value in [
            ("configured", None),
            ("direct", ""),
            ("unreachable_proxy_then_direct", "http://127.0.0.1:9"),
        ]:
            routes[service + ":" + label] = probe(service, variable, value, code)
    after = inspect()
    assert before == after
    assert all(s["running"] and not s["oom"] for s in after.values())
    timings.sort()
    print(
        json.dumps(
            {
                "revision": (args.root / ".deployed-revision").read_text().strip(),
                "results": results,
                "load": {
                    "samples": len(timings),
                    "p50_ms": timings[len(timings) // 2],
                    "p95_ms": timings[int(len(timings) * 0.95)],
                    "max_ms": max(timings),
                },
                "resources": resources,
                "routes": routes,
                "containers_unchanged": True,
                "messages_sent": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest

from src.services.dadata_client import DadataClient, DadataUnavailable
from src.services.outbound import proxy_health
from src.vkbot.processor import Processor


@pytest.fixture
def clients(monkeypatch):
    original = httpx.AsyncClient
    created, configurations = [], []

    def install(handler):
        def factory(**kwargs: object):
            configurations.append(kwargs)
            client = original(
                transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k != "proxy"}
            )
            created.append(client)
            return client

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return configurations

    DadataClient._unavailable_until = 0
    proxy_health.routes.clear()
    yield install
    assert all(client.is_closed for client in created)
    DadataClient._unavailable_until = 0
    proxy_health.routes.clear()


class SlowBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        """Удержать тело после успешного получения заголовков."""
        await asyncio.sleep(1)
        yield b"{}"

    async def aclose(self):
        self.closed = True


def test_internal_lookup_deadline_includes_response_body_and_closes_client(clients):
    body = SlowBody()
    configurations = clients(lambda request: httpx.Response(200, stream=body))
    processor = object.__new__(Processor)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        processor._get("http://internal.test/lookup", timeout=0.03)
    assert time.monotonic() - started < 0.5
    assert body.closed
    assert configurations[0]["trust_env"] is False
    assert configurations[0]["timeout"].connect <= 0.03


def test_internal_query_is_encoded_and_retains_special_characters(clients):
    received = []

    def handler(request):
        received.append(request.url.params["message"])
        return httpx.Response(200, json={"status": "found"})

    clients(handler)
    processor = object.__new__(Processor)
    query = "Москва, Перерва 62к1 & корпус=1"
    assert processor._get("http://internal.test/lookup", {"message": query}).json() == {"status": "found"}
    assert received == [query]


def test_dadata_proxy_handshake_failure_retries_direct_with_scoped_clients(clients):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ProxyError("Proxy CONNECT failed", request=request)
        return httpx.Response(200, json={"suggestions": []})

    configurations = clients(handler)
    assert asyncio.run(DadataClient("unused", proxy_url="http://proxy.test:8888").geolocate(1, 2)) == {
        "suggestions": []
    }
    assert [c["proxy"] for c in configurations] == ["http://proxy.test:8888", None]
    assert all(c["trust_env"] is False for c in configurations)


def test_dadata_response_error_keeps_route_and_closes_client(clients):
    configurations = clients(lambda request: httpx.Response(429, json={"error": "rate limited"}))
    with pytest.raises(DadataUnavailable):
        asyncio.run(DadataClient("unused", proxy_url="http://proxy.test:8888").geolocate(1, 2))
    assert len(configurations) == 1


def test_dadata_total_deadline_also_covers_body_consumption(clients):
    body = SlowBody()
    clients(lambda request: httpx.Response(200, stream=body))
    started = time.monotonic()
    with pytest.raises(DadataUnavailable):
        asyncio.run(DadataClient("unused", timeout_seconds=0.03).geolocate(1, 2))
    assert time.monotonic() - started < 0.5
    assert body.closed


def test_geocoding_process_exits_cleanly_after_repeated_event_loops():
    # Настоящие сокеты и отдельный интерпретатор: моки не обнаруживают сбой завершения.
    script = r"""
import asyncio, json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from src.services.dadata_client import DadataClient

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.dumps({'suggestions': [{'data': {
            'city': 'Город', 'street': 'Тестовая', 'house': '1', 'block': None
        }}]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

server = HTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever)
thread.start()
try:
    for _ in range(4):
        client = DadataClient('unused')
        client.base_url = 'http://127.0.0.1:' + str(server.server_port)
        assert asyncio.run(client.get_address_by_geo(1, 2)) == 'Город Тестовая 1'
    print('geocoding_completed', flush=True)
finally:
    server.shutdown()
    thread.join()
    server.server_close()
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        env={**os.environ, "PYTHONFAULTHANDLER": "1"},
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "geocoding_completed" in result.stdout

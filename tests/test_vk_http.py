import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from requests import Response, Session
from requests.exceptions import ReadTimeout

from src.vkbot.http import TimeoutSession


def test_vk_api_calls_have_connect_and_read_timeouts() -> None:
    with patch.object(Session, "request") as request:
        TimeoutSession().post("https://api.vk.com/method/test", data={})
    assert request.call_args.kwargs["timeout"] == (5, 20)

def test_none_does_not_disable_the_default_timeout() -> None:
    with patch.object(Session, "request") as request:
        TimeoutSession().get("https://api.vk.com/method/test", timeout=None)
    assert request.call_args.kwargs["timeout"] == (5, 20)

def test_longpoll_preserves_its_explicit_wait_timeout() -> None:
    with patch.object(Session, "request") as request:
        TimeoutSession().get("https://lp.vk.com/test", timeout=35)
    assert request.call_args.kwargs["timeout"] == 35


def test_transient_vk_failure_retries_with_same_message_id() -> None:
    data = {"peer_id": 123, "random_id": 456, "message": "test"}
    response = Response()
    with (
        patch.object(Session, "request", side_effect=[ReadTimeout(), response]) as request,
        patch("src.vkbot.http.time.sleep"),
    ):
        result = TimeoutSession().post("https://api.vk.com/method/messages.send", data=data)
    assert result is response
    assert request.call_count == 2
    assert all(call.kwargs["data"]["random_id"] == 456 for call in request.call_args_list)


def test_send_without_deduplication_is_not_retried() -> None:
    with (
        patch.object(Session, "request", side_effect=ReadTimeout()) as request,
        pytest.raises(ReadTimeout),
    ):
        TimeoutSession().post("https://api.vk.com/method/messages.send", data={"random_id": 0})
    assert request.call_count == 1


def test_persistent_vk_failure_has_a_bounded_number_of_attempts() -> None:
    with (
        patch.object(Session, "request", side_effect=ReadTimeout()) as request,
        patch("src.vkbot.http.time.sleep"),
        pytest.raises(ReadTimeout),
    ):
        TimeoutSession().post("https://api.vk.com/method/groups.isMember", data={})
    assert request.call_count == 3


def test_unresponsive_server_raises_read_timeout() -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            time.sleep(21)
            try:
                self.send_response(200)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with TimeoutSession() as session, pytest.raises(ReadTimeout):
            session.get(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

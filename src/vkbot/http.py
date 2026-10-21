import time
from typing import Any
from urllib.parse import urlsplit

from requests import Response, Session
from requests.exceptions import ConnectionError, Timeout


class TimeoutSession(Session):
    """Bound VK API requests while preserving explicit Long Poll timeouts."""

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (5, 20)

        endpoint = urlsplit(url)
        api_method = endpoint.path.removeprefix("/method/")
        safe_methods = {
            "groups.isMember",
            "groups.getLongPollServer",
            "messages.getById",
            "messages.getByConversationMessageId",
            "messages.edit",
        }
        data = kwargs.get("data")
        deduplicated_send = (
            api_method == "messages.send"
            and isinstance(data, dict)
            and bool(data.get("random_id"))
        )
        retryable = endpoint.hostname == "api.vk.com" and (
            api_method in safe_methods or deduplicated_send
        )
        attempts = 3 if retryable else 1
        for attempt in range(attempts):
            try:
                return super().request(method, url, **kwargs)
            except (ConnectionError, Timeout):
                if attempt == attempts - 1:
                    raise
                time.sleep(attempt + 1)
        raise RuntimeError("Request attempts exhausted")

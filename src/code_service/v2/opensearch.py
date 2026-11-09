"""HTTP-клиент необязательного поискового индекса."""

import httpx

from src.code_service.config import Settings
from src.code_service.v2.engine import search_body
from src.infrastructure.network import tls_context


async def candidate_ids(message: str, config: Settings) -> list[str]:
    if not config.opensearch_url:
        raise ValueError("Search index is not configured")
    async with httpx.AsyncClient(
        verify=tls_context(), trust_env=False, timeout=config.opensearch_timeout_seconds
    ) as client:
        response = await client.post(
            config.opensearch_url + "/" + config.opensearch_index + "/_search",
            json=search_body(
                message,
                config.search_candidate_limit,
                fuzziness=config.opensearch_fuzziness,
                prefix_length=config.opensearch_prefix_length,
            ),
        )
        if response.status_code != 200:
            raise RuntimeError("Search index unavailable")
        return [row["_id"] for row in response.json()["hits"]["hits"]]

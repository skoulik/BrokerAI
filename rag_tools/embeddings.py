from box import Box
import httpx
from typing import Optional, List, Tuple, Dict
import copy
import asyncio

class Embedder:
    def __init__(self, config : Box):
        self.config = config
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=600)
        self.http_client = httpx.AsyncClient(
            base_url = config.embeddings.base_url,
            timeout  = config.embeddings.timeout,
            limits   = limits,
            headers  = {'Accept': "application/json"}
        )

    async def close(self):
        await self.http_client.aclose()

    async def embed_strings(self, strings : List[str]) -> List[List[float]]:
        request_json = copy.deepcopy(self.config.embeddings.template)
        request_json['input'] = strings
        response = await self.http_client.post(
            url  = self.config.embeddings.endpoint,
            json = request_json
        )
        embeddings = [r['embedding'] for r in response.json()['data']]
        return embeddings

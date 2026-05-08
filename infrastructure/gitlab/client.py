from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from domain.models import PostResult


class GitLabClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        retries: int = 2,
        backoff: float = 0.5,
        timeout_seconds: int = 60,
    ) -> None:
        self._base_url = base_url.rstrip('/')
        self._retries = retries
        self._backoff = backoff
        self._timeout_seconds = timeout_seconds
        self._headers = {
            'PRIVATE-TOKEN': token,
            'Content-Type': 'application/json',
        }

    def proj_url(self, project_id: str | int, *segments: str) -> str:
        pid = quote(str(project_id), safe='')
        return f'{self._base_url}/projects/{pid}/' + '/'.join(str(segment) for segment in segments)

    async def get_one(self, url: str) -> dict:
        return await asyncio.to_thread(self._get_one_sync, url)

    async def get_paged(self, url: str) -> list[dict]:
        return await asyncio.to_thread(self._get_paged_sync, url)

    async def post(self, url: str, payload: dict) -> PostResult:
        return await asyncio.to_thread(self._post_sync, url, payload)

    def _get_one_sync(self, url: str) -> dict:
        data = self._request_json(Request(url, headers=self._headers))
        if not isinstance(data, dict):
            raise RuntimeError('GitLab API returned a non-object response.')
        return cast(dict, data)

    def _get_paged_sync(self, url: str) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            sep = '&' if '?' in url else '?'
            request = Request(f'{url}{sep}page={page}&per_page=100', headers=self._headers)
            chunk = self._request_json(request)
            if not chunk:
                break

            if isinstance(chunk, dict):
                page_items = [chunk]
            else:
                page_items = [item for item in chunk if isinstance(item, dict)]

            results.extend(page_items)
            if len(page_items) < 100:
                break
            page += 1
        return results

    def _post_sync(self, url: str, payload: dict) -> PostResult:
        data = json.dumps(payload).encode()
        for attempt in range(self._retries + 1):
            request = Request(url, data=data, headers=self._headers, method='POST')
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    return PostResult(ok=True, data=json.loads(response.read()))
            except HTTPError as exc:
                err_body = exc.read().decode()
                if exc.code >= 500 and attempt < self._retries:
                    time.sleep(self._backoff * (2 ** attempt))
                    continue
                return PostResult(ok=False, status=exc.code, error=err_body)
            except URLError as exc:
                if attempt < self._retries:
                    time.sleep(self._backoff * (2 ** attempt))
                    continue
                return PostResult(ok=False, status=None, error=f'network: {exc}')
        return PostResult(ok=False, status=None, error='exhausted retries')

    def _request_json(self, request: Request) -> dict[str, Any] | list[dict[str, Any]]:
        for attempt in range(self._retries + 1):
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    return json.loads(response.read())
            except HTTPError as exc:
                body = exc.read().decode()
                if exc.code >= 500 and attempt < self._retries:
                    time.sleep(self._backoff * (2 ** attempt))
                    continue
                raise RuntimeError(f'GitLab API error {exc.code}: {body}') from exc
            except URLError as exc:
                if attempt < self._retries:
                    time.sleep(self._backoff * (2 ** attempt))
                    continue
                raise RuntimeError(f'GitLab network error: {exc}') from exc
        raise RuntimeError('Exhausted retries while calling GitLab API.')

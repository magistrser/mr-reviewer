from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any

from domain.workspace import WorkspacePaths
from runtime.async_ops import AsyncPathIO


class ReviewProgressStore:
    SCHEMA_VERSION = 1
    COMPLETED = 'completed'
    FAILED = 'failed'
    RUNNING = 'running'

    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths

    @property
    def path(self) -> Path:
        return self._paths.progress

    async def exists(self) -> bool:
        return await AsyncPathIO.exists(self.path)

    async def load(self) -> dict[str, Any]:
        if not await self.exists():
            return {}
        payload = await AsyncPathIO.read_json(self.path)
        if not isinstance(payload, dict):
            raise ValueError(f'{self.path} must contain a JSON object')
        return payload

    async def initialize(
        self,
        *,
        mr_url: str,
        project_path: str,
        mr_iid: str,
        model: str,
        preview_mode: bool,
    ) -> dict[str, Any]:
        current = await self.load()
        if current:
            updated = dict(current)
            updated.update(
                {
                    'mr_url': mr_url,
                    'project_path': project_path,
                    'mr_iid': mr_iid,
                    'model': model,
                    'preview_mode': preview_mode,
                    'updated_at': self._now(),
                }
            )
            await self.save(updated)
            return updated

        timestamp = self._now()
        progress = {
            'schema_version': self.SCHEMA_VERSION,
            'mr_url': mr_url,
            'project_path': project_path,
            'mr_iid': mr_iid,
            'workspace': str(self._paths.root),
            'model': model,
            'preview_mode': preview_mode,
            'created_at': timestamp,
            'updated_at': timestamp,
            'status': self.RUNNING,
            'current_stage': None,
            'completed_stages': {},
            'stage_status': {},
            'warnings': [],
        }
        await self.save(progress)
        return progress

    async def save(self, progress: dict[str, Any]) -> None:
        progress['updated_at'] = self._now()
        await AsyncPathIO.mkdir(self.path.parent, parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f'{self.path.suffix}.tmp')
        await AsyncPathIO.write_text(tmp_path, json.dumps(progress, indent=2))
        await asyncio.to_thread(tmp_path.replace, self.path)

    async def mark_stage_started(
        self,
        stage_id: str,
        title: str,
        *,
        warnings: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        progress = await self.load()
        progress['status'] = self.RUNNING
        progress['current_stage'] = stage_id
        progress.setdefault('stage_status', {})[stage_id] = {
            'status': self.RUNNING,
            'title': title,
            'started_at': self._now(),
        }
        if warnings is not None:
            progress['warnings'] = list(warnings)
        await self.save(progress)

    async def mark_stage_completed(
        self,
        stage_id: str,
        title: str,
        data: dict[str, Any] | None = None,
        *,
        warnings: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        progress = await self.load()
        progress['status'] = self.RUNNING
        progress['current_stage'] = None
        progress.setdefault('completed_stages', {})[stage_id] = {
            'title': title,
            'completed_at': self._now(),
            'data': data or {},
        }
        progress.setdefault('stage_status', {})[stage_id] = {
            'status': self.COMPLETED,
            'title': title,
            'completed_at': self._now(),
        }
        if warnings is not None:
            progress['warnings'] = list(warnings)
        await self.save(progress)

    async def mark_stage_failed(
        self,
        stage_id: str,
        title: str,
        exc: BaseException,
        *,
        warnings: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        progress = await self.load()
        progress['status'] = self.FAILED
        progress['current_stage'] = stage_id
        progress.setdefault('stage_status', {})[stage_id] = {
            'status': self.FAILED,
            'title': title,
            'failed_at': self._now(),
            'error': {
                'type': type(exc).__name__,
                'message': str(exc),
            },
        }
        if warnings is not None:
            progress['warnings'] = list(warnings)
        await self.save(progress)

    async def mark_run_completed(
        self,
        *,
        result: dict[str, Any],
        warnings: list[str] | tuple[str, ...],
    ) -> None:
        progress = await self.load()
        progress['status'] = self.COMPLETED
        progress['current_stage'] = None
        progress['result'] = result
        progress['warnings'] = list(warnings)
        progress['completed_at'] = self._now()
        await self.save(progress)

    async def is_stage_complete(self, stage_id: str) -> bool:
        progress = await self.load()
        return stage_id in progress.get('completed_stages', {})

    async def stage_data(self, stage_id: str) -> dict[str, Any]:
        progress = await self.load()
        completed = progress.get('completed_stages', {})
        stage = completed.get(stage_id, {}) if isinstance(completed, dict) else {}
        data = stage.get('data', {}) if isinstance(stage, dict) else {}
        return data if isinstance(data, dict) else {}

    async def warnings(self) -> list[str]:
        progress = await self.load()
        raw_warnings = progress.get('warnings', [])
        return [str(warning) for warning in raw_warnings] if isinstance(raw_warnings, list) else []

    @staticmethod
    def is_complete_payload(progress: dict[str, Any]) -> bool:
        if progress.get('status') == ReviewProgressStore.COMPLETED:
            return True
        completed = progress.get('completed_stages', {})
        return isinstance(completed, dict) and 'publish' in completed

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

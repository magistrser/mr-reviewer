from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from application.dto import ReviewRunOptions
from application.review_jobs import (
    ReviewJobError,
    ReviewJobRequest,
    ReviewJobSnapshot,
    ReviewJobStatus,
    ReviewResultSerializer,
)
from infrastructure.api.output import JobReviewOutput
from infrastructure.composition import ReviewApplicationFactory
from infrastructure.workspace.setup import WorkspaceBuilder
from settings import Settings


class FileReviewJobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def create(self, request: ReviewJobRequest) -> ReviewJobSnapshot:
        snapshot = ReviewJobSnapshot(
            job_id=uuid.uuid4().hex,
            status='queued',
            request=request,
        )
        self.save(snapshot)
        return snapshot

    def get(self, job_id: str) -> ReviewJobSnapshot | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return self._decode(json.loads(path.read_text()))

    def update(
        self,
        job_id: str,
        *,
        status: ReviewJobStatus | None = None,
        progress: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | None = None,
        result: dict[str, Any] | None = None,
        error: ReviewJobError | None = None,
    ) -> ReviewJobSnapshot:
        snapshot = self.get(job_id)
        if snapshot is None:
            raise KeyError(job_id)
        updated = replace(
            snapshot,
            status=status or snapshot.status,
            progress=progress if progress is not None else snapshot.progress,
            warnings=warnings if warnings is not None else snapshot.warnings,
            result=result if result is not None else snapshot.result,
            error=error,
        )
        self.save(updated)
        return updated

    def save(self, snapshot: ReviewJobSnapshot) -> None:
        path = self._path(snapshot.job_id)
        tmp_path = path.with_suffix('.tmp')
        tmp_path.write_text(json.dumps(self._encode(snapshot), indent=2))
        tmp_path.replace(path)

    def _path(self, job_id: str) -> Path:
        return self._root / f'{job_id}.json'

    @classmethod
    def _encode(cls, snapshot: ReviewJobSnapshot) -> dict[str, Any]:
        return ReviewResultSerializer.to_jsonable(asdict(snapshot))

    @classmethod
    def _decode(cls, payload: dict[str, Any]) -> ReviewJobSnapshot:
        raw_error = payload.get('error')
        return ReviewJobSnapshot(
            job_id=str(payload['job_id']),
            status=payload['status'],
            request=ReviewJobRequest(
                mr_url=str(payload['request']['mr_url']),
                model=payload['request'].get('model'),
            ),
            progress=payload.get('progress') or {},
            warnings=tuple(payload.get('warnings') or ()),
            result=payload.get('result'),
            error=(
                ReviewJobError(
                    type=str(raw_error['type']),
                    message=str(raw_error['message']),
                )
                if raw_error else None
            ),
        )


class BackgroundReviewJobRunner:
    def __init__(
        self,
        app_settings: Settings,
        factory: type[ReviewApplicationFactory] = ReviewApplicationFactory,
    ) -> None:
        api_settings = app_settings.api
        review_root = api_settings.review_root.resolve()
        self._settings = app_settings
        self._factory = factory
        self._store = FileReviewJobStore(review_root / '.mr-review-api-jobs')
        self._semaphore = asyncio.Semaphore(api_settings.max_concurrent_jobs)

    def enqueue(self, request: ReviewJobRequest) -> ReviewJobSnapshot:
        snapshot = self._store.create(request)
        asyncio.create_task(self._run(snapshot.job_id))
        return snapshot

    def get(self, job_id: str) -> ReviewJobSnapshot | None:
        return self._store.get(job_id)

    def _update_job(self, job_id: str, **changes: Any) -> ReviewJobSnapshot:
        return self._store.update(job_id, **changes)

    async def _run(self, job_id: str) -> None:
        snapshot = self._store.get(job_id)
        if snapshot is None:
            return

        async with self._semaphore:
            self._store.update(
                job_id,
                status='running',
                progress={'kind': 'job', 'message': 'Review job started'},
            )
            output = JobReviewOutput(job_id, self._update_job)
            try:
                api_settings = self._settings.api
                use_case = await self._factory.build_use_case(
                    review_root=api_settings.review_root,
                    env_path=api_settings.env_path,
                    settings_path=api_settings.settings_path,
                    resources_dir=api_settings.resources_dir,
                    agents_dir=api_settings.agents_dir,
                    output=output,
                    previewer=None,
                    workspace_provisioner=WorkspaceBuilder.setup_workspace_job_safe,
                )
                result = await use_case.execute(
                    snapshot.request.mr_url,
                    options=ReviewRunOptions(
                        model=snapshot.request.model,
                        preview_mode=False,
                    ),
                )
            except Exception as exc:
                self._store.update(
                    job_id,
                    status='failed',
                    progress={'kind': 'job', 'message': 'Review job failed'},
                    error=ReviewJobError(type=type(exc).__name__, message=str(exc)),
                )
                return

            self._store.update(
                job_id,
                status='succeeded',
                progress={'kind': 'job', 'message': 'Review job completed'},
                result=ReviewResultSerializer.to_jsonable(result),
            )

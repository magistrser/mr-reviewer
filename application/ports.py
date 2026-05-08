from __future__ import annotations

from pathlib import Path
from typing import Protocol

from application.dto import ReviewResult, TaskProgress
from domain.models import PostResult
from domain.review.preview import PreviewSessionResult, PreviewSessionState
from domain.workspace import WorkspacePaths


class GitLabPort(Protocol):
    def proj_url(self, project_id: str | int, *segments: str) -> str:
        ...

    async def get_one(self, url: str) -> dict:
        ...

    async def get_paged(self, url: str) -> list[dict]:
        ...

    async def post(self, url: str, payload: dict) -> PostResult:
        ...


class AgentPort(Protocol):
    async def default_model(self) -> str:
        ...

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        ...


class WorkspaceProvisioner(Protocol):
    async def __call__(
        self,
        review_root: Path,
        repo_slug: str,
        mr_iid: str,
        http_url: str,
        head_sha: str,
        target_branch: str,
        base_sha: str = '',
        start_sha: str = '',
    ) -> WorkspacePaths:
        ...


class ReviewPreviewPort(Protocol):
    async def preview(self, session: PreviewSessionState) -> PreviewSessionResult:
        ...


class ReviewOutputPort(Protocol):
    def step_started(self, index: int, total: int, title: str) -> None:
        ...

    def detail(self, label: str, value: str) -> None:
        ...

    def warning(self, message: str) -> None:
        ...

    def task_progress(self, progress: TaskProgress) -> None:
        ...

    def completed(self, result: ReviewResult) -> None:
        ...

    def failed(self, exc: BaseException) -> None:
        ...

    def cancelled(self) -> None:
        ...

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from application.review_progress import ReviewProgressStore
from domain.workspace import WorkspacePaths


@dataclass(frozen=True)
class ResumeWorkspace:
    paths: WorkspacePaths
    progress: dict[str, Any]
    done: bool

    @property
    def name(self) -> str:
        return self.paths.root.name


class WorkspaceResumeResolver:
    @classmethod
    def resolve(cls, review_root: Path, workspace_name: str | None) -> ResumeWorkspace:
        workspaces_root = cls.workspaces_root(review_root)
        if workspace_name:
            candidate = workspaces_root / workspace_name
            if not candidate.is_dir():
                raise ValueError(f'Review workspace not found: {workspace_name}')
            return cls._resolve_candidate(candidate)

        candidates = [
            candidate
            for candidate in workspaces_root.iterdir()
            if candidate.is_dir() and cls._is_resumable_or_done(candidate)
        ] if workspaces_root.is_dir() else []
        if not candidates:
            raise ValueError(f'No resumable review workspaces found in {workspaces_root}')
        latest = max(candidates, key=cls._workspace_mtime)
        return cls._resolve_candidate(latest)

    @staticmethod
    def workspaces_root(review_root: Path) -> Path:
        return review_root / '.pr-review-workspaces'

    @classmethod
    def _resolve_candidate(cls, root: Path) -> ResumeWorkspace:
        paths = WorkspacePaths(root=root)
        progress = cls._read_progress(paths.progress)
        done = cls._is_done(paths, progress)
        if not progress and not done:
            raise ValueError(
                f'{root.name} is a legacy workspace without progress.json and cannot be resumed.'
            )
        return ResumeWorkspace(paths=paths, progress=progress, done=done)

    @classmethod
    def _is_resumable_or_done(cls, root: Path) -> bool:
        paths = WorkspacePaths(root=root)
        return paths.progress.exists() or cls._is_done(paths, {})

    @classmethod
    def _is_done(cls, paths: WorkspacePaths, progress: dict[str, Any]) -> bool:
        if progress and ReviewProgressStore.is_complete_payload(progress):
            return True
        return paths.publish_result.exists() and paths.quality_report.exists()

    @staticmethod
    def _read_progress(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f'{path} must contain a JSON object')
        return payload

    @staticmethod
    def _workspace_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0

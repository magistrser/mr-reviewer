from __future__ import annotations

import datetime
import json
from pathlib import Path
from urllib.parse import urlparse

from domain.models import WorkspaceMeta
from domain.workspace import WorkspacePaths
from runtime.async_ops import AsyncCommandRunner, AsyncPathIO


class WorkspaceBuilder:
    @staticmethod
    async def _run(cmd: list[str], cwd: Path | None = None) -> None:
        await AsyncCommandRunner.run_checked(cmd, cwd=cwd)

    @classmethod
    async def setup_workspace(
        cls,
        review_root: Path,
        repo_slug: str,
        mr_iid: str,
        http_url: str,
        head_sha: str,
        target_branch: str,
        base_sha: str = '',
        start_sha: str = '',
    ) -> WorkspacePaths:
        return await cls._setup_workspace(
            review_root=review_root,
            repo_slug=repo_slug,
            mr_iid=mr_iid,
            http_url=http_url,
            head_sha=head_sha,
            target_branch=target_branch,
            base_sha=base_sha,
            start_sha=start_sha,
            cleanup_existing=True,
        )

    @classmethod
    async def setup_workspace_job_safe(
        cls,
        review_root: Path,
        repo_slug: str,
        mr_iid: str,
        http_url: str,
        head_sha: str,
        target_branch: str,
        base_sha: str = '',
        start_sha: str = '',
    ) -> WorkspacePaths:
        return await cls._setup_workspace(
            review_root=review_root,
            repo_slug=repo_slug,
            mr_iid=mr_iid,
            http_url=http_url,
            head_sha=head_sha,
            target_branch=target_branch,
            base_sha=base_sha,
            start_sha=start_sha,
            cleanup_existing=False,
        )

    @classmethod
    async def _setup_workspace(
        cls,
        review_root: Path,
        repo_slug: str,
        mr_iid: str,
        http_url: str,
        head_sha: str,
        target_branch: str,
        base_sha: str,
        start_sha: str,
        cleanup_existing: bool,
    ) -> WorkspacePaths:
        project_path = urlparse(http_url).path.lstrip('/').removesuffix('.git')
        workspaces_root = review_root / '.pr-review-workspaces'

        if cleanup_existing:
            for old_result in await AsyncPathIO.glob(f'/tmp/pr-review-publish-result-*-{mr_iid}.json'):
                await AsyncPathIO.remove(Path(old_result))

        paths = WorkspacePaths(root=await cls._unique_workspace_path(workspaces_root, repo_slug, mr_iid))
        await AsyncPathIO.mkdir(paths.repo_dir, parents=True, exist_ok=True)

        meta = WorkspaceMeta(
            project_path=project_path,
            mr_iid=mr_iid,
            head_sha=head_sha,
            base_sha=base_sha,
            start_sha=start_sha,
        )
        await AsyncPathIO.write_text(paths.meta, json.dumps(meta))

        await cls._run(['git', 'clone', '--filter=blob:none', '--no-tags', http_url, str(paths.repo_dir)])
        await cls._run(['git', 'fetch', '--no-tags', 'origin', target_branch, head_sha], cwd=paths.repo_dir)
        await cls._run(['git', 'checkout', head_sha], cwd=paths.repo_dir)

        if not await AsyncPathIO.is_dir(paths.repo_dir / '.git'):
            raise RuntimeError('.git directory missing after clone')

        return paths

    @classmethod
    async def _unique_workspace_path(cls, workspaces_root: Path, repo_slug: str, mr_iid: str) -> Path:
        base_name = cls.workspace_name(repo_slug, mr_iid)
        candidate = workspaces_root / base_name
        suffix = 2
        while await AsyncPathIO.exists(candidate):
            candidate = workspaces_root / f'{base_name}-{suffix}'
            suffix += 1
        return candidate

    @staticmethod
    def workspace_name(repo_slug: str, mr_iid: str) -> str:
        stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        return f'{repo_slug}-mr-{mr_iid}-{stamp}'

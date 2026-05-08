from __future__ import annotations

from pathlib import Path

from application.ports import ReviewOutputPort, ReviewPreviewPort, WorkspaceProvisioner
from application.review_flow import ReviewDependencies, ReviewMergeRequestUseCase
from infrastructure.agents.openai_runner import OpenAIAgentRunner
from infrastructure.gitlab.client import GitLabClient
from infrastructure.workspace.setup import WorkspaceBuilder
from settings import Config, ConfigLoader


class ReviewApplicationFactory:
    @staticmethod
    def project_root() -> Path:
        return Path(__file__).resolve().parents[1]

    @classmethod
    def default_resources_dir(cls) -> Path:
        return cls.project_root() / 'resources' / 'review'

    @classmethod
    def default_agents_dir(cls) -> Path:
        return cls.project_root() / 'resources' / 'agents'

    @classmethod
    def resolve_settings_path(cls, review_root: Path, settings_path: Path | None) -> Path:
        if settings_path is not None:
            return settings_path.resolve()
        local_path = review_root / 'settings.yml'
        if local_path.exists():
            return local_path
        return cls.project_root() / 'settings.dev.yml'

    @classmethod
    async def load_config(
        cls,
        *,
        review_root: Path,
        env_path: Path | None = None,
        settings_path: Path | None = None,
        resources_dir: Path | None = None,
        agents_dir: Path | None = None,
    ) -> Config:
        resolved_review_root = review_root.resolve()
        return await ConfigLoader.load(
            env_path=(env_path.resolve() if env_path is not None else resolved_review_root / '.env'),
            settings_path=cls.resolve_settings_path(resolved_review_root, settings_path),
            review_root=resolved_review_root,
            resources_dir=(resources_dir.resolve() if resources_dir is not None else cls.default_resources_dir()),
            agents_dir=(agents_dir.resolve() if agents_dir is not None else cls.default_agents_dir()),
        )

    @classmethod
    async def build_use_case(
        cls,
        *,
        review_root: Path,
        output: ReviewOutputPort,
        env_path: Path | None = None,
        settings_path: Path | None = None,
        resources_dir: Path | None = None,
        agents_dir: Path | None = None,
        previewer: ReviewPreviewPort | None = None,
        workspace_provisioner: WorkspaceProvisioner = WorkspaceBuilder.setup_workspace,
    ) -> ReviewMergeRequestUseCase:
        config = await cls.load_config(
            review_root=review_root,
            env_path=env_path,
            settings_path=settings_path,
            resources_dir=resources_dir,
            agents_dir=agents_dir,
        )
        gitlab = GitLabClient(
            token=config.gitlab.token,
            base_url=config.gitlab.api_url,
            retries=config.review.http_retries,
            backoff=config.review.http_backoff_seconds,
            timeout_seconds=config.review.http_timeout_seconds,
        )
        agent = OpenAIAgentRunner(
            base_url=config.agent.base_url,
            api_key=config.agent.api_key,
            timeout_seconds=config.agent.timeout_seconds,
            agents_dir=config.review.agents_dir,
            retries=config.review.http_retries,
            backoff_seconds=config.review.http_backoff_seconds,
        )
        return ReviewMergeRequestUseCase(
            config,
            ReviewDependencies(
                gitlab=gitlab,
                agent=agent,
                output=output,
                previewer=previewer,
                workspace_provisioner=workspace_provisioner,
            ),
        )

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from application.comment_deduplication import ReviewCommentDeduplicator
from application.dto import ReviewResult, ReviewRunOptions, ReviewTask, TaskProgress, TaskProgressItem
from application.publish_review import ReviewPublisher
from application.review_preview import ReviewPreviewService
from application.review_progress import ReviewProgressStore
from application.review_translation import ReviewTranslationService
from application.ports import (
    AgentPort,
    GitLabPort,
    ReviewOutputPort,
    ReviewPreviewPort,
    WorkspaceProvisioner,
)
from settings import Config
from domain.models import ChangedFile
from domain.review.comment_dedup import CommentDedupPlanner
from domain.review.consolidate import FindingsConsolidator
from domain.review.context import ReviewContextBuilder, ReviewContextLimits
from domain.review.diff import DiffReviewTools
from domain.review.gate import ReviewGate
from domain.review.indexing import RepoCatalogBuilder, ReviewRetrievalPlanner
from domain.review.planning import ReviewScopePlanner
from domain.review.standards import Resources, ReviewStandards
from domain.workspace import WorkspacePaths
from infrastructure.gitlab.compact import GitLabCompactor
from runtime.async_ops import AsyncPathIO
from infrastructure.workspace.setup import WorkspaceBuilder


@dataclass(frozen=True)
class ReviewDependencies:
    gitlab: GitLabPort
    agent: AgentPort
    output: ReviewOutputPort
    previewer: ReviewPreviewPort | None = None
    workspace_provisioner: WorkspaceProvisioner = WorkspaceBuilder.setup_workspace


class MergeRequestUrlParser:
    MR_URL_RE = re.compile(r'https?://[^/]+/(.+?)/-/merge_requests/(\d+)')

    @classmethod
    def parse(cls, url: str) -> tuple[str, str]:
        match = cls.MR_URL_RE.search(url)
        if not match:
            raise ValueError(f'Cannot parse MR URL: {url}')
        return match.group(1), match.group(2)


class ReviewWorkspaceService:
    @classmethod
    async def collect_review_state(
        cls,
        client: GitLabPort,
        project_path: str,
        mr_iid: str,
        paths: WorkspacePaths,
    ) -> dict[str, int]:
        discussions_raw = await client.get_paged(
            client.proj_url(project_path, f'merge_requests/{mr_iid}/discussions')
        )
        discussions = GitLabCompactor.compact_discussions(discussions_raw)
        comments = GitLabCompactor.compact_mr_comments(discussions_raw)
        await AsyncPathIO.write_text(paths.existing_discussions, json.dumps(discussions))
        await AsyncPathIO.write_text(paths.existing_comments, json.dumps(comments))

        changes_raw = await client.get_one(client.proj_url(project_path, f'merge_requests/{mr_iid}/changes'))
        changes = GitLabCompactor.compact_changes(changes_raw)
        await AsyncPathIO.write_text(paths.changed_files, json.dumps(changes))
        return {
            'discussions_count': len(discussions),
            'comments_count': len(comments),
            'files_changed': len(changes),
        }

    @classmethod
    async def create_summary_input(cls, client: GitLabPort, paths: WorkspacePaths) -> Path:
        meta = await AsyncPathIO.read_json(paths.meta)
        project_path = meta['project_path']
        mr_iid = meta['mr_iid']
        raw_mr = await client.get_one(client.proj_url(project_path, f'merge_requests/{mr_iid}'))
        changes_raw = await client.get_one(client.proj_url(project_path, f'merge_requests/{mr_iid}/changes'))
        await AsyncPathIO.write_text(
            paths.summary_input,
            json.dumps(
                {
                    'summary_path': str(paths.pr_summary),
                    'mr': {
                        'title': raw_mr.get('title', ''),
                        'description': raw_mr.get('description', '') or '',
                    },
                    'changes': GitLabCompactor.compact_diff_for_summary(changes_raw),
                }
            ),
        )
        return paths.summary_input

    @classmethod
    async def build_repo_catalog(
        cls,
        paths: WorkspacePaths,
        *,
        enabled: bool,
        max_catalog_file_bytes: int,
    ) -> dict[str, object]:
        catalog = await RepoCatalogBuilder.build(
            paths.repo_dir,
            enabled=enabled,
            max_file_bytes=max_catalog_file_bytes,
        )
        await AsyncPathIO.write_json(paths.repo_catalog, catalog, indent=2)
        return {
            'enabled': bool(catalog.get('enabled', enabled)),
            'indexed_files': int(catalog.get('indexed_files', 0)),
            'skipped_large_files': int(catalog.get('skipped_large_files', 0)),
            'skipped_binary_files': int(catalog.get('skipped_binary_files', 0)),
            'artifact_path': str(paths.repo_catalog),
        }

    @classmethod
    async def gate_changed_files(
        cls,
        paths: WorkspacePaths,
        max_eligible: int,
        large_file_bytes: int,
        max_cluster_reviews: int,
    ) -> int:
        files: list[ChangedFile] = await AsyncPathIO.read_json(paths.changed_files)
        await ReviewGate.gate(files, paths.repo_dir, max_eligible=max_eligible, large_file_bytes=large_file_bytes)
        repo_catalog = await cls._load_repo_catalog(paths, enabled=True)
        await ReviewScopePlanner.enrich_files(files, paths.repo_dir, repo_catalog=repo_catalog)
        clusters = ReviewScopePlanner.build_clusters(files)[:max_cluster_reviews]
        file_index = {
            changed_file['new_path']: changed_file
            for changed_file in files
            if changed_file.get('new_path')
        }
        for cluster in clusters:
            cluster_files = [
                file_index[path]
                for path in cluster.get('files', [])
                if path in file_index and not file_index[path].get('skip')
            ]
            cluster['evidence'] = ReviewRetrievalPlanner.build_cluster_evidence(cluster_files)
            cluster['retrieval_requests'] = ReviewRetrievalPlanner.build_cluster_retrieval_requests(
                cluster,
                cluster_files,
            )
        await AsyncPathIO.write_text(paths.changed_files, json.dumps(files))
        await AsyncPathIO.write_text(
            paths.file_analysis,
            json.dumps(
                [
                    {
                        'path': changed_file.get('new_path', ''),
                        'analysis': changed_file.get('analysis', {}),
                        'skip': changed_file.get('skip', False),
                    }
                    for changed_file in files
                ],
                indent=2,
            ),
        )
        await AsyncPathIO.write_text(paths.cluster_plan, json.dumps(clusters, indent=2))
        return len([entry for entry in files if not entry.get('skip')])

    @classmethod
    async def next_review_task(
        cls,
        paths: WorkspacePaths,
        resources: Resources,
        max_findings_per_file: int,
        context_limits: ReviewContextLimits | None = None,
        indexing_enabled: bool = True,
        max_retrieved_artifacts_per_task: int = 3,
        max_retrieved_chars_per_task: int = 2500,
        max_retrieved_lines_per_artifact: int = 80,
        file_retrieval_policies: dict[str, object] | None = None,
    ) -> ReviewTask | None:
        await AsyncPathIO.mkdir(paths.findings_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.excerpts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.contexts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.review_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.retrieval_plans_dir, parents=True, exist_ok=True)

        files: list[ChangedFile] = await AsyncPathIO.read_json(paths.changed_files)
        eligible = [entry for entry in files if not entry.get('skip')]
        attempts = await AsyncPathIO.read_json(paths.attempts) if await AsyncPathIO.exists(paths.attempts) else {}
        discussions = await AsyncPathIO.read_json(paths.existing_discussions)
        repo_catalog = await cls._load_repo_catalog(paths, enabled=indexing_enabled)
        plan = cls._build_plan(eligible, resources)

        await FindingsConsolidator.fix_malformed_findings(paths.findings_dir)
        done = await cls._done_passes(paths.findings_dir)

        while len(done) < len(plan):
            current = next(item for item in plan if (item[0], item[1]) not in done)
            ordinal, pass_id, focus_area, standards_text, review_plan_text, changed_file = current
            key = f'{ordinal:03d}-{pass_id}'
            findings_path = paths.findings_dir / f'{key}.json'
            prior_attempts = attempts.get(key, 0)
            if prior_attempts >= 1:
                await AsyncPathIO.write_text(
                    findings_path,
                    json.dumps(
                        {
                            'file': changed_file['new_path'],
                            'findings': [],
                            'skipped_as_duplicate': [],
                            'anomalies': [
                                f'review-agent did not write findings after {prior_attempts} attempt(s)'
                            ],
                        },
                        indent=2,
                    ),
                )
                done.add((ordinal, pass_id))
                continue

            attempts[key] = prior_attempts + 1
            await AsyncPathIO.write_text(paths.attempts, json.dumps(attempts))
            existing = [
                {k: v for k, v in discussion.items() if k != 'file_path'}
                for discussion in discussions
                if discussion.get('file_path') == changed_file['new_path']
            ]
            excerpts_path = paths.excerpts_dir / f'{ordinal:03d}.txt'
            if not await AsyncPathIO.exists(excerpts_path):
                await DiffReviewTools.write_excerpts(
                    paths.repo_dir / changed_file['new_path'],
                    changed_file.get('hunks', []),
                    excerpts_path,
                    changed_file['new_path'],
                )
            retrieval_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id=pass_id,
                focus_area=focus_area,
                catalog=repo_catalog,
                repo_dir=paths.repo_dir,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id=pass_id,
                    max_artifacts=max_retrieved_artifacts_per_task,
                    max_chars=max_retrieved_chars_per_task,
                    max_lines_per_artifact=max_retrieved_lines_per_artifact,
                    policy_override=(file_retrieval_policies or {}).get(pass_id),
                ),
            )
            await cls._write_retrieval_plan(
                paths,
                key,
                pass_id=pass_id,
                focus_area=focus_area,
                subject=changed_file['new_path'],
                source_kind='file',
                retrieval_plan=retrieval_plan,
            )
            context_path = paths.contexts_dir / f'{ordinal:03d}-{pass_id}.md'
            await ReviewContextBuilder.write_file_context(
                changed_file,
                paths.repo_dir,
                excerpts_path,
                context_path,
                pass_id,
                focus_area,
                standards_text,
                review_plan_text,
                resources,
                pr_summary_path=paths.pr_summary,
                limits=context_limits,
                retrieved_artifacts=list(retrieval_plan.get('artifacts', [])),
            )
            input_path = paths.review_inputs_dir / f'{key}.json'
            await AsyncPathIO.write_text(
                input_path,
                json.dumps(
                    ReviewStandards.agent_input_payload(
                        changed_file,
                        ordinal,
                        findings_path,
                        context_path,
                        existing,
                        pass_id=pass_id,
                        focus_area=focus_area,
                        max_findings_per_file=max_findings_per_file,
                    )
                ),
            )
            await cls._update_retrieval_report(paths, retrieval_plan)
            return ReviewTask(
                ordinal=ordinal,
                pass_id=pass_id,
                file_path=changed_file['new_path'],
                input_path=input_path,
                completed_steps=len(done),
                total_steps=len(plan),
                focus_area=focus_area,
                source_kind='file',
                findings_path=findings_path,
                subject_key=changed_file['new_path'],
            )
        return None

    @classmethod
    async def build_review_tasks(
        cls,
        paths: WorkspacePaths,
        resources: Resources,
        max_findings_per_file: int,
        context_limits: ReviewContextLimits | None = None,
        indexing_enabled: bool = True,
        max_retrieved_artifacts_per_task: int = 3,
        max_retrieved_chars_per_task: int = 2500,
        max_retrieved_lines_per_artifact: int = 80,
        file_retrieval_policies: dict[str, object] | None = None,
    ) -> list[ReviewTask]:
        await AsyncPathIO.mkdir(paths.findings_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.excerpts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.contexts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.review_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.retrieval_plans_dir, parents=True, exist_ok=True)

        files: list[ChangedFile] = await AsyncPathIO.read_json(paths.changed_files)
        eligible = [entry for entry in files if not entry.get('skip')]
        attempts = await AsyncPathIO.read_json(paths.attempts) if await AsyncPathIO.exists(paths.attempts) else {}
        discussions = await AsyncPathIO.read_json(paths.existing_discussions)
        repo_catalog = await cls._load_repo_catalog(paths, enabled=indexing_enabled)
        plan = cls._build_plan(eligible, resources)

        await FindingsConsolidator.fix_malformed_findings(paths.findings_dir)
        done = await cls._done_passes(paths.findings_dir)

        task_specs: list[tuple[int, str, str, Path, str, Path, str]] = []
        for ordinal, pass_id, focus_area, standards_text, review_plan_text, changed_file in plan:
            if (ordinal, pass_id) in done:
                continue
            key = f'{ordinal:03d}-{pass_id}'
            findings_path = paths.findings_dir / f'{key}.json'
            prior_attempts = attempts.get(key, 0)
            if prior_attempts >= 1:
                await AsyncPathIO.write_text(
                    findings_path,
                    json.dumps(
                        {
                            'file': changed_file['new_path'],
                            'findings': [],
                            'skipped_as_duplicate': [],
                            'anomalies': [
                                f'review-agent did not write findings after {prior_attempts} attempt(s)'
                            ],
                        },
                        indent=2,
                    ),
                )
                done.add((ordinal, pass_id))
                continue

            existing = [
                {k: v for k, v in discussion.items() if k != 'file_path'}
                for discussion in discussions
                if discussion.get('file_path') == changed_file['new_path']
            ]
            excerpts_path = paths.excerpts_dir / f'{ordinal:03d}.txt'
            if not await AsyncPathIO.exists(excerpts_path):
                await DiffReviewTools.write_excerpts(
                    paths.repo_dir / changed_file['new_path'],
                    changed_file.get('hunks', []),
                    excerpts_path,
                    changed_file['new_path'],
                )
            retrieval_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id=pass_id,
                focus_area=focus_area,
                catalog=repo_catalog,
                repo_dir=paths.repo_dir,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id=pass_id,
                    max_artifacts=max_retrieved_artifacts_per_task,
                    max_chars=max_retrieved_chars_per_task,
                    max_lines_per_artifact=max_retrieved_lines_per_artifact,
                    policy_override=(file_retrieval_policies or {}).get(pass_id),
                ),
            )
            await cls._write_retrieval_plan(
                paths,
                key,
                pass_id=pass_id,
                focus_area=focus_area,
                subject=changed_file['new_path'],
                source_kind='file',
                retrieval_plan=retrieval_plan,
            )
            context_path = paths.contexts_dir / f'{ordinal:03d}-{pass_id}.md'
            await ReviewContextBuilder.write_file_context(
                changed_file,
                paths.repo_dir,
                excerpts_path,
                context_path,
                pass_id,
                focus_area,
                standards_text,
                review_plan_text,
                resources,
                pr_summary_path=paths.pr_summary,
                limits=context_limits,
                retrieved_artifacts=list(retrieval_plan.get('artifacts', [])),
            )
            input_path = paths.review_inputs_dir / f'{key}.json'
            await AsyncPathIO.write_text(
                input_path,
                json.dumps(
                    ReviewStandards.agent_input_payload(
                        changed_file,
                        ordinal,
                        findings_path,
                        context_path,
                        existing,
                        pass_id=pass_id,
                        focus_area=focus_area,
                        max_findings_per_file=max_findings_per_file,
                    )
                ),
            )
            await cls._update_retrieval_report(paths, retrieval_plan)
            task_specs.append((
                ordinal,
                pass_id,
                changed_file['new_path'],
                input_path,
                focus_area,
                findings_path,
                changed_file['new_path'],
            ))

        total_steps = len(task_specs)
        return [
            ReviewTask(
                ordinal=ordinal,
                pass_id=pass_id,
                file_path=file_path,
                input_path=input_path,
                completed_steps=index,
                total_steps=total_steps,
                focus_area=focus_area,
                source_kind='file',
                findings_path=findings_path,
                subject_key=subject_key,
            )
            for index, (
                ordinal,
                pass_id,
                file_path,
                input_path,
                focus_area,
                findings_path,
                subject_key,
            ) in enumerate(task_specs)
        ]

    @classmethod
    async def next_cluster_review_task(
        cls,
        paths: WorkspacePaths,
        resources: Resources,
        max_findings_per_cluster: int,
        context_limits: ReviewContextLimits | None = None,
        indexing_enabled: bool = True,
        max_retrieved_artifacts_per_task: int = 3,
        max_retrieved_chars_per_task: int = 2500,
        max_retrieved_lines_per_artifact: int = 80,
        cluster_retrieval_policies: dict[str, object] | None = None,
    ) -> ReviewTask | None:
        await AsyncPathIO.mkdir(paths.cluster_findings_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.cluster_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.contexts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.retrieval_plans_dir, parents=True, exist_ok=True)

        if not await AsyncPathIO.exists(paths.cluster_plan):
            return None
        clusters = await AsyncPathIO.read_json(paths.cluster_plan)
        if not clusters:
            return None

        files: list[ChangedFile] = await AsyncPathIO.read_json(paths.changed_files)
        file_index = {
            changed_file['new_path']: changed_file
            for changed_file in files
            if changed_file.get('new_path')
        }
        attempts = (
            await AsyncPathIO.read_json(paths.cluster_attempts)
            if await AsyncPathIO.exists(paths.cluster_attempts) else {}
        )
        discussions = (
            await AsyncPathIO.read_json(paths.existing_discussions)
            if await AsyncPathIO.exists(paths.existing_discussions) else []
        )
        repo_catalog = await cls._load_repo_catalog(paths, enabled=indexing_enabled)
        plan = cls._build_cluster_plan(clusters, file_index, resources)

        await FindingsConsolidator.fix_malformed_findings(paths.cluster_findings_dir)
        done = await cls._done_passes(paths.cluster_findings_dir)

        while len(done) < len(plan):
            current = next(item for item in plan if (item[0], item[1]) not in done)
            ordinal, pass_id, focus_area, standards_text, review_plan_text, cluster, cluster_files = current
            key = f'{ordinal:03d}-{pass_id}'
            findings_path = paths.cluster_findings_dir / f'{key}.json'
            prior_attempts = attempts.get(key, 0)
            if prior_attempts >= 1:
                await AsyncPathIO.write_text(
                    findings_path,
                    json.dumps(
                        {
                            'cluster_id': cluster['cluster_id'],
                            'findings': [],
                            'anomalies': [
                                f'cluster-review-agent did not write findings after {prior_attempts} attempt(s)'
                            ],
                        },
                        indent=2,
                    ),
                )
                done.add((ordinal, pass_id))
                continue

            attempts[key] = prior_attempts + 1
            await AsyncPathIO.write_text(paths.cluster_attempts, json.dumps(attempts))
            existing = [
                discussion for discussion in discussions
                if discussion.get('file_path') in set(cluster.get('files', []))
            ]
            retrieval_plan = await ReviewRetrievalPlanner.plan_cluster_task(
                cluster=cluster,
                cluster_files=cluster_files,
                catalog=repo_catalog,
                repo_dir=paths.repo_dir,
                policy=ReviewRetrievalPlanner.resolve_cluster_policy(
                    focus_area=focus_area,
                    max_artifacts=max_retrieved_artifacts_per_task,
                    max_chars=max_retrieved_chars_per_task,
                    max_lines_per_artifact=max_retrieved_lines_per_artifact,
                    policy_override=(cluster_retrieval_policies or {}).get(focus_area),
                ),
            )
            await cls._write_retrieval_plan(
                paths,
                f'cluster-{key}',
                pass_id=pass_id,
                focus_area=focus_area,
                subject=cluster['title'],
                source_kind='cluster',
                retrieval_plan=retrieval_plan,
            )
            context_path = paths.contexts_dir / f'cluster-{ordinal:03d}-{pass_id}.md'
            await ReviewContextBuilder.write_cluster_context(
                cluster,
                cluster_files,
                paths.repo_dir,
                context_path,
                standards_text,
                review_plan_text,
                resources,
                pr_summary_path=paths.pr_summary,
                limits=context_limits,
                retrieved_artifacts=list(retrieval_plan.get('artifacts', [])),
            )
            input_path = paths.cluster_inputs_dir / f'{key}.json'
            await AsyncPathIO.write_text(
                input_path,
                json.dumps(
                    ReviewStandards.cluster_input_payload(
                        cluster,
                        cluster_files,
                        findings_path,
                        context_path,
                        existing,
                        max_findings_total=max_findings_per_cluster,
                        pass_id=pass_id,
                        focus_area=focus_area,
                    )
                ),
            )
            await cls._update_retrieval_report(paths, retrieval_plan)
            return ReviewTask(
                ordinal=ordinal,
                pass_id=pass_id,
                file_path=cluster['title'],
                input_path=input_path,
                completed_steps=len(done),
                total_steps=len(plan),
                focus_area=focus_area,
                source_kind='cluster',
                findings_path=findings_path,
                subject_key=str(cluster.get('cluster_id', cluster['title'])),
            )
        return None

    @classmethod
    async def build_cluster_review_tasks(
        cls,
        paths: WorkspacePaths,
        resources: Resources,
        max_findings_per_cluster: int,
        context_limits: ReviewContextLimits | None = None,
        indexing_enabled: bool = True,
        max_retrieved_artifacts_per_task: int = 3,
        max_retrieved_chars_per_task: int = 2500,
        max_retrieved_lines_per_artifact: int = 80,
        cluster_retrieval_policies: dict[str, object] | None = None,
    ) -> list[ReviewTask]:
        await AsyncPathIO.mkdir(paths.cluster_findings_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.cluster_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.contexts_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.retrieval_plans_dir, parents=True, exist_ok=True)

        if not await AsyncPathIO.exists(paths.cluster_plan):
            return []
        clusters = await AsyncPathIO.read_json(paths.cluster_plan)
        if not clusters:
            return []

        files: list[ChangedFile] = await AsyncPathIO.read_json(paths.changed_files)
        file_index = {
            changed_file['new_path']: changed_file
            for changed_file in files
            if changed_file.get('new_path')
        }
        attempts = (
            await AsyncPathIO.read_json(paths.cluster_attempts)
            if await AsyncPathIO.exists(paths.cluster_attempts) else {}
        )
        discussions = (
            await AsyncPathIO.read_json(paths.existing_discussions)
            if await AsyncPathIO.exists(paths.existing_discussions) else []
        )
        repo_catalog = await cls._load_repo_catalog(paths, enabled=indexing_enabled)
        plan = cls._build_cluster_plan(clusters, file_index, resources)

        await FindingsConsolidator.fix_malformed_findings(paths.cluster_findings_dir)
        done = await cls._done_passes(paths.cluster_findings_dir)

        task_specs: list[tuple[int, str, str, Path, str, Path, str]] = []
        for ordinal, pass_id, focus_area, standards_text, review_plan_text, cluster, cluster_files in plan:
            if (ordinal, pass_id) in done:
                continue
            key = f'{ordinal:03d}-{pass_id}'
            findings_path = paths.cluster_findings_dir / f'{key}.json'
            prior_attempts = attempts.get(key, 0)
            if prior_attempts >= 1:
                await AsyncPathIO.write_text(
                    findings_path,
                    json.dumps(
                        {
                            'cluster_id': cluster['cluster_id'],
                            'findings': [],
                            'anomalies': [
                                f'cluster-review-agent did not write findings after {prior_attempts} attempt(s)'
                            ],
                        },
                        indent=2,
                    ),
                )
                done.add((ordinal, pass_id))
                continue

            existing = [
                discussion for discussion in discussions
                if discussion.get('file_path') in set(cluster.get('files', []))
            ]
            retrieval_plan = await ReviewRetrievalPlanner.plan_cluster_task(
                cluster=cluster,
                cluster_files=cluster_files,
                catalog=repo_catalog,
                repo_dir=paths.repo_dir,
                policy=ReviewRetrievalPlanner.resolve_cluster_policy(
                    focus_area=focus_area,
                    max_artifacts=max_retrieved_artifacts_per_task,
                    max_chars=max_retrieved_chars_per_task,
                    max_lines_per_artifact=max_retrieved_lines_per_artifact,
                    policy_override=(cluster_retrieval_policies or {}).get(focus_area),
                ),
            )
            await cls._write_retrieval_plan(
                paths,
                f'cluster-{key}',
                pass_id=pass_id,
                focus_area=focus_area,
                subject=cluster['title'],
                source_kind='cluster',
                retrieval_plan=retrieval_plan,
            )
            context_path = paths.contexts_dir / f'cluster-{ordinal:03d}-{pass_id}.md'
            await ReviewContextBuilder.write_cluster_context(
                cluster,
                cluster_files,
                paths.repo_dir,
                context_path,
                standards_text,
                review_plan_text,
                resources,
                pr_summary_path=paths.pr_summary,
                limits=context_limits,
                retrieved_artifacts=list(retrieval_plan.get('artifacts', [])),
            )
            input_path = paths.cluster_inputs_dir / f'{key}.json'
            await AsyncPathIO.write_text(
                input_path,
                json.dumps(
                    ReviewStandards.cluster_input_payload(
                        cluster,
                        cluster_files,
                        findings_path,
                        context_path,
                        existing,
                        max_findings_total=max_findings_per_cluster,
                        pass_id=pass_id,
                        focus_area=focus_area,
                    )
                ),
            )
            await cls._update_retrieval_report(paths, retrieval_plan)
            task_specs.append((
                ordinal,
                pass_id,
                cluster['title'],
                input_path,
                focus_area,
                findings_path,
                str(cluster.get('cluster_id', cluster['title'])),
            ))

        total_steps = len(task_specs)
        return [
            ReviewTask(
                ordinal=ordinal,
                pass_id=pass_id,
                file_path=file_path,
                input_path=input_path,
                completed_steps=index,
                total_steps=total_steps,
                focus_area=focus_area,
                source_kind='cluster',
                findings_path=findings_path,
                subject_key=subject_key,
            )
            for index, (
                ordinal,
                pass_id,
                file_path,
                input_path,
                focus_area,
                findings_path,
                subject_key,
            ) in enumerate(task_specs)
        ]

    @classmethod
    async def consolidate_findings(cls, paths: WorkspacePaths) -> dict[str, object]:
        return await FindingsConsolidator.consolidate(
            [paths.findings_dir, paths.cluster_findings_dir],
            paths.raw_findings,
        )

    @classmethod
    async def finalize_findings(cls, paths: WorkspacePaths, max_findings_total: int) -> dict[str, int]:
        findings = (
            await AsyncPathIO.read_json(paths.raw_findings)
            if await AsyncPathIO.exists(paths.raw_findings) else []
        )
        total = len(findings)
        kept = list(findings)
        kept.sort(key=CommentDedupPlanner.sort_key, reverse=True)
        kept = kept[:max_findings_total]
        await AsyncPathIO.write_text(paths.all_findings, json.dumps(kept))
        return {
            'kept': len(kept),
            'total': total,
        }

    @classmethod
    async def _load_repo_catalog(cls, paths: WorkspacePaths, *, enabled: bool) -> dict:
        if not enabled or not await AsyncPathIO.exists(paths.repo_catalog):
            return RepoCatalogBuilder.empty_catalog(enabled=enabled)
        try:
            catalog = await AsyncPathIO.read_json(paths.repo_catalog)
        except Exception:
            return RepoCatalogBuilder.empty_catalog(enabled=enabled)
        return catalog if isinstance(catalog, dict) else RepoCatalogBuilder.empty_catalog(enabled=enabled)

    @classmethod
    async def _write_retrieval_plan(
        cls,
        paths: WorkspacePaths,
        key: str,
        *,
        pass_id: str,
        focus_area: str,
        subject: str,
        source_kind: str,
        retrieval_plan: dict[str, object],
    ) -> None:
        await AsyncPathIO.mkdir(paths.retrieval_plans_dir, parents=True, exist_ok=True)
        await AsyncPathIO.write_json(
            paths.retrieval_plans_dir / f'{key}.json',
            {
                'subject': subject,
                'source_kind': source_kind,
                'pass_id': pass_id,
                'focus_area': focus_area,
                'policy': retrieval_plan.get('policy', ''),
                'applied_policy': retrieval_plan.get('applied_policy', {}),
                'requests': retrieval_plan.get('requests', []),
                'artifacts': retrieval_plan.get('artifacts', []),
                'stats': retrieval_plan.get('stats', {}),
            },
            indent=2,
        )

    @classmethod
    async def _update_retrieval_report(cls, paths: WorkspacePaths, retrieval_plan: dict[str, object]) -> None:
        report = (
            await AsyncPathIO.read_json(paths.retrieval_report)
            if await AsyncPathIO.exists(paths.retrieval_report) else cls._empty_retrieval_report()
        )
        stage = str(retrieval_plan.get('stage', 'file'))
        stats = retrieval_plan.get('stats', {})
        if not isinstance(report, dict):
            report = cls._empty_retrieval_report()
        totals = report.setdefault('totals', cls._empty_retrieval_stats())
        by_stage = report.setdefault('by_stage', {})
        stage_stats = by_stage.setdefault(stage, cls._empty_retrieval_stats())
        for label in ('planned_retrievals', 'executed_retrievals', 'empty_retrievals', 'skipped_by_policy'):
            increment = int(stats.get(label, 0)) if isinstance(stats, dict) else 0
            totals[label] = int(totals.get(label, 0)) + increment
            stage_stats[label] = int(stage_stats.get(label, 0)) + increment
        stage_stats['tasks'] = int(stage_stats.get('tasks', 0)) + 1
        totals['tasks'] = int(totals.get('tasks', 0)) + 1
        if int((stats or {}).get('executed_retrievals', 0)) > 0:
            stage_stats['tasks_with_artifacts'] = int(stage_stats.get('tasks_with_artifacts', 0)) + 1
            totals['tasks_with_artifacts'] = int(totals.get('tasks_with_artifacts', 0)) + 1
        await AsyncPathIO.write_json(paths.retrieval_report, report, indent=2)

    @classmethod
    def _empty_retrieval_report(cls) -> dict[str, object]:
        return {
            'totals': cls._empty_retrieval_stats(),
            'by_stage': {},
        }

    @classmethod
    def _empty_retrieval_stats(cls) -> dict[str, int]:
        return {
            'planned_retrievals': 0,
            'executed_retrievals': 0,
            'empty_retrievals': 0,
            'skipped_by_policy': 0,
            'tasks': 0,
            'tasks_with_artifacts': 0,
        }

    @classmethod
    def _build_plan(
        cls,
        eligible: list[ChangedFile],
        resources: Resources,
    ) -> list[tuple[int, str, str, str, str, ChangedFile]]:
        plan: list[tuple[int, str, str, str, str, ChangedFile]] = []
        for ordinal, changed_file in enumerate(eligible, start=1):
            for pass_id, focus_area, standards_text, review_plan_text in ReviewStandards.passes_for_file(
                changed_file,
                resources,
            ):
                plan.append((ordinal, pass_id, focus_area, standards_text, review_plan_text, changed_file))
        return plan

    @classmethod
    def _build_cluster_plan(
        cls,
        clusters: list[dict],
        file_index: dict[str, ChangedFile],
        resources: Resources,
    ) -> list[tuple[int, str, str, str, str, dict, list[ChangedFile]]]:
        plan: list[tuple[int, str, str, str, str, dict, list[ChangedFile]]] = []
        for ordinal, cluster in enumerate(clusters, start=1):
            cluster_files = [
                file_index[path]
                for path in cluster.get('files', [])
                if path in file_index and not file_index[path].get('skip')
            ]
            if len(cluster_files) < 2:
                continue
            cluster_materials = ReviewStandards.cluster_review_materials(
                cluster,
                cluster_files,
                resources,
            )
            if cluster_materials is None:
                continue
            pass_id, review_plan_text, standards_text = cluster_materials
            focus_area = str(cluster.get('focus_area', 'integration'))
            plan.append((
                ordinal,
                pass_id,
                focus_area,
                standards_text,
                review_plan_text,
                cluster,
                cluster_files,
            ))
        return plan

    @classmethod
    async def _done_passes(cls, findings_dir: Path) -> set[tuple[int, str]]:
        done: set[tuple[int, str]] = set()
        if not await AsyncPathIO.exists(findings_dir):
            return done
        for entry in await AsyncPathIO.iterdir(findings_dir):
            match = FindingsConsolidator.FINDINGS_RE.match(entry.name)
            if match:
                done.add((int(match.group(1)), match.group(2)))
        return done

    @classmethod
    async def count_done_passes(cls, findings_dir: Path) -> int:
        return len(await cls._done_passes(findings_dir))

    @classmethod
    async def record_attempt(cls, attempts_path: Path, task: ReviewTask) -> None:
        attempts = await AsyncPathIO.read_json(attempts_path) if await AsyncPathIO.exists(attempts_path) else {}
        attempts[task.key] = int(attempts.get(task.key, 0)) + 1
        await AsyncPathIO.write_json(attempts_path, attempts)

    @classmethod
    async def stub_missing_findings(cls, task: ReviewTask, agent_name: str) -> None:
        if task.findings_path is None or await AsyncPathIO.exists(task.findings_path):
            return

        anomaly = f'{agent_name} did not write findings after 1 attempt(s)'
        if task.source_kind == 'cluster':
            payload = {
                'cluster_id': task.subject_key or task.file_path,
                'findings': [],
                'anomalies': [anomaly],
            }
        else:
            payload = {
                'file': task.subject_key or task.file_path,
                'findings': [],
                'skipped_as_duplicate': [],
                'anomalies': [anomaly],
            }
        await AsyncPathIO.write_json(task.findings_path, payload, indent=2)


class ReviewMergeRequestUseCase:
    TOTAL_STEPS = 10

    def __init__(self, config: Config, dependencies: ReviewDependencies) -> None:
        self._config = config
        self._dependencies = dependencies

    async def execute(
        self,
        mr_url: str,
        model: str | None = None,
        options: ReviewRunOptions | None = None,
    ) -> ReviewResult:
        options = options or ReviewRunOptions(model=model)
        resume_workspace = options.resume_workspace
        workspace = WorkspacePaths(root=resume_workspace) if resume_workspace is not None else None
        progress_store = ReviewProgressStore(workspace) if workspace is not None else None
        saved_progress = await progress_store.load() if progress_store is not None else {}
        if resume_workspace is not None and not saved_progress:
            raise RuntimeError(f'{resume_workspace} is missing progress.json and cannot be resumed.')

        effective_mr_url = str(saved_progress.get('mr_url') or mr_url)
        requested_model = options.model if options.model is not None else model
        if requested_model:
            selected_model = requested_model
            self._write_detail('Model', selected_model)
        elif saved_progress.get('model'):
            selected_model = str(saved_progress['model'])
            self._write_detail('Model', selected_model)
        else:
            self._write_detail('Model', 'resolving default model from /models')
            selected_model = await self._dependencies.agent.default_model()
            self._write_detail('Model', selected_model)

        if options.preview_mode is None and saved_progress:
            preview_mode = bool(saved_progress.get('preview_mode', False))
        else:
            preview_mode = bool(options.preview_mode)

        warnings = await progress_store.warnings() if progress_store is not None else []

        def add_warning(message: str) -> None:
            if message not in warnings:
                warnings.append(message)
            self._write_warning(message)

        async def run_progress_stage(
            stage_id: str,
            index: int,
            title: str,
            runner: Callable[[], Awaitable[dict[str, Any]]],
        ) -> dict[str, Any]:
            if progress_store is None:
                raise RuntimeError('progress store is unavailable after workspace resolution')
            self._write_step(index, self.TOTAL_STEPS, title)
            if await progress_store.is_stage_complete(stage_id):
                self._write_detail('Progress', 'using completed workspace artifacts')
                return await progress_store.stage_data(stage_id)
            await progress_store.mark_stage_started(stage_id, title, warnings=warnings)
            try:
                stage_data = await runner()
            except Exception as exc:
                await progress_store.mark_stage_failed(stage_id, title, exc, warnings=warnings)
                raise
            await progress_store.mark_stage_completed(stage_id, title, self._jsonable(stage_data), warnings=warnings)
            return stage_data

        self._write_detail('Resources', str(self._config.review.resources_dir))
        resources = await ReviewStandards.load_resources(
            self._config.review.resources_dir,
            self._config.review.review_root,
            enabled_file_pass_ids=self._config.review.enabled_file_pass_ids,
            enabled_cluster_pass_ids=self._config.review.enabled_cluster_pass_ids,
        )

        stage_title = 'Resolve merge request and prepare workspace'
        self._write_step(1, self.TOTAL_STEPS, stage_title)
        if progress_store is not None and await progress_store.is_stage_complete('resolve_workspace'):
            resolve_data = await progress_store.stage_data('resolve_workspace')
            self._write_detail('Progress', 'using completed workspace artifacts')
            project_path = str(resolve_data.get('project_path') or saved_progress.get('project_path') or '')
            mr_iid = str(resolve_data.get('mr_iid') or saved_progress.get('mr_iid') or '')
            if not project_path or not mr_iid:
                project_path, mr_iid = MergeRequestUrlParser.parse(effective_mr_url)
            mr = dict(resolve_data.get('mr') or {})
            mr.setdefault('web_url', effective_mr_url)
            diff_refs = dict(resolve_data.get('diff_refs') or {})
            if not diff_refs and workspace is not None and await AsyncPathIO.exists(workspace.meta):
                meta = await AsyncPathIO.read_json(workspace.meta)
                diff_refs = {
                    'base_sha': str(meta.get('base_sha', '')),
                    'start_sha': str(meta.get('start_sha', '')),
                    'head_sha': str(meta.get('head_sha', '')),
                }
            mr['diff_refs'] = diff_refs
            assert workspace is not None
            await progress_store.initialize(
                mr_url=effective_mr_url,
                project_path=project_path,
                mr_iid=mr_iid,
                model=selected_model,
                preview_mode=preview_mode,
            )
        else:
            if progress_store is not None:
                await progress_store.mark_stage_started('resolve_workspace', stage_title, warnings=warnings)
            try:
                project_path, mr_iid = MergeRequestUrlParser.parse(effective_mr_url)
                self._write_detail('GitLab MR', f'loading {project_path}!{mr_iid}')
                mr = GitLabCompactor.compact_mr(
                    await self._dependencies.gitlab.get_one(
                        self._dependencies.gitlab.proj_url(project_path, f'merge_requests/{mr_iid}')
                    )
                )
                diff_refs = mr.get('diff_refs')
                if not diff_refs:
                    self._write_detail('GitLab MR', 'diff refs missing; loading versions')
                    versions = await self._dependencies.gitlab.get_paged(
                        self._dependencies.gitlab.proj_url(project_path, f'merge_requests/{mr_iid}/versions')
                    )
                    if not versions:
                        raise RuntimeError('MR is missing diff_refs and versions are unavailable.')
                    version = versions[0]
                    diff_refs = {
                        'base_sha': version['base_commit_sha'],
                        'start_sha': version['start_commit_sha'],
                        'head_sha': version['head_commit_sha'],
                    }
                    mr['diff_refs'] = diff_refs

                repo_slug = project_path.split('/')[-1]
                if workspace is None:
                    self._write_detail('Workspace', f'cloning {repo_slug} at {diff_refs["head_sha"][:12]}')
                    workspace = await self._dependencies.workspace_provisioner(
                        review_root=self._config.review.review_root,
                        repo_slug=repo_slug,
                        mr_iid=mr_iid,
                        http_url=mr['http_url_to_repo'],
                        head_sha=diff_refs['head_sha'],
                        target_branch=mr['target_branch'],
                        base_sha=diff_refs['base_sha'],
                        start_sha=diff_refs['start_sha'],
                    )
                    progress_store = ReviewProgressStore(workspace)
                    await progress_store.initialize(
                        mr_url=mr['web_url'],
                        project_path=project_path,
                        mr_iid=mr_iid,
                        model=selected_model,
                        preview_mode=preview_mode,
                    )
                    await progress_store.mark_stage_started('resolve_workspace', stage_title, warnings=warnings)
                else:
                    self._write_detail('Workspace', f'resuming {workspace.root.name}')
                    if not await AsyncPathIO.is_dir(workspace.repo_dir / '.git'):
                        raise RuntimeError(f'{workspace.root} does not contain a cloned git repository.')
                    await progress_store.initialize(
                        mr_url=mr['web_url'],
                        project_path=project_path,
                        mr_iid=mr_iid,
                        model=selected_model,
                        preview_mode=preview_mode,
                    )

                await progress_store.mark_stage_completed(
                    'resolve_workspace',
                    stage_title,
                    {
                        'mr_url': mr['web_url'],
                        'project_path': project_path,
                        'mr_iid': mr_iid,
                        'workspace': str(workspace.root),
                        'repo_slug': repo_slug,
                        'mr': mr,
                        'diff_refs': diff_refs,
                        'selected_model': selected_model,
                        'preview_mode': preview_mode,
                    },
                    warnings=warnings,
                )
            except Exception as exc:
                if progress_store is not None:
                    await progress_store.mark_stage_failed('resolve_workspace', stage_title, exc, warnings=warnings)
                raise

        assert workspace is not None
        assert progress_store is not None
        self._write_detail('MR', f'{project_path}!{mr_iid}')
        self._write_detail('Workspace', str(workspace.root))

        state = await run_progress_stage(
            'collect_state',
            2,
            'Collect changed files and existing discussions',
            lambda: ReviewWorkspaceService.collect_review_state(
                self._dependencies.gitlab,
                project_path,
                mr_iid,
                workspace,
            ),
        )
        self._write_detail('Changed files', str(state['files_changed']))
        self._write_detail('Open discussions', str(state['discussions_count']))
        self._write_detail('Existing comments', str(state['comments_count']))

        async def generate_summary() -> dict[str, Any]:
            summary_status = 'written'
            try:
                summary_input = await ReviewWorkspaceService.create_summary_input(self._dependencies.gitlab, workspace)
                await self._dependencies.agent.run('summarize-pr-agent', str(summary_input), selected_model)
                self._write_detail('Summary', f'written to {workspace.pr_summary}')
            except Exception as exc:
                summary_status = 'skipped'
                add_warning(f'Summary skipped: {exc}')
            return {'summary_status': summary_status}

        summary_data = await run_progress_stage(
            'summary',
            3,
            'Generate merge request summary',
            generate_summary,
        )
        summary_status = str(summary_data.get('summary_status', 'skipped'))

        async def analyze_files() -> dict[str, Any]:
            indexing = await ReviewWorkspaceService.build_repo_catalog(
                workspace,
                enabled=self._config.review.indexing_enabled,
                max_catalog_file_bytes=self._config.review.max_catalog_file_bytes,
            )
            eligible = await ReviewWorkspaceService.gate_changed_files(
                workspace,
                max_eligible=self._config.review.max_eligible_files,
                large_file_bytes=self._config.review.large_file_bytes,
                max_cluster_reviews=self._config.review.max_cluster_reviews,
            )
            clusters = (
                await AsyncPathIO.read_json(workspace.cluster_plan)
                if await AsyncPathIO.exists(workspace.cluster_plan) else []
            )
            if eligible == 0:
                add_warning(
                    'No eligible source files remained after gating; only the final summary note will be posted.'
                )
            return {
                'indexing_summary': indexing,
                'eligible_count': eligible,
                'cluster_plan': clusters,
            }

        analysis_data = await run_progress_stage(
            'analyze_files',
            4,
            'Select and analyze reviewable files',
            analyze_files,
        )
        indexing_summary = dict(analysis_data.get('indexing_summary') or {})
        eligible_count = int(analysis_data.get('eligible_count', 0))
        cluster_plan = analysis_data.get('cluster_plan')
        if not isinstance(cluster_plan, list):
            cluster_plan = (
                await AsyncPathIO.read_json(workspace.cluster_plan)
                if await AsyncPathIO.exists(workspace.cluster_plan) else []
            )
        self._write_detail('Indexed files', str(indexing_summary.get('indexed_files', 0)))
        self._write_detail('Catalog', str(indexing_summary.get('artifact_path', workspace.repo_catalog)))
        self._write_detail('Eligible files', f'{eligible_count} of {state["files_changed"]}')
        self._write_detail('Cluster groups', str(len(cluster_plan)))

        async def run_review_passes() -> dict[str, Any]:
            review_tasks = await ReviewWorkspaceService.build_review_tasks(
                workspace,
                resources,
                max_findings_per_file=self._config.review.max_findings_per_file,
                context_limits=self._config.review.context_limits,
                indexing_enabled=self._config.review.indexing_enabled,
                max_retrieved_artifacts_per_task=self._config.review.max_retrieved_artifacts_per_task,
                max_retrieved_chars_per_task=self._config.review.max_retrieved_chars_per_task,
                max_retrieved_lines_per_artifact=self._config.review.max_retrieved_lines_per_artifact,
                file_retrieval_policies=self._config.review.file_retrieval_policies,
            )
            parallel = min(self._config.review.parallel_reviews, max(1, len(review_tasks)))
            self._write_detail('Parallel reviews', str(parallel))
            await self._run_agent_tasks(
                stage_label='Review pass',
                agent_name='review-agent',
                tasks=review_tasks,
                model=selected_model,
                attempts_path=workspace.attempts,
                concurrency=self._config.review.parallel_reviews,
            )
            return {
                'review_passes': await ReviewWorkspaceService.count_done_passes(workspace.findings_dir),
                'parallel_reviews': parallel,
            }

        review_data = await run_progress_stage(
            'review_passes',
            5,
            'Run review passes',
            run_review_passes,
        )
        review_passes = int(review_data.get('review_passes', 0))
        if 'review_passes' not in review_data:
            review_passes = await ReviewWorkspaceService.count_done_passes(workspace.findings_dir)
        self._write_detail('Review passes', str(review_passes))

        async def run_cluster_passes() -> dict[str, Any]:
            cluster_tasks = await ReviewWorkspaceService.build_cluster_review_tasks(
                workspace,
                resources,
                max_findings_per_cluster=self._config.review.max_findings_per_cluster,
                context_limits=self._config.review.context_limits,
                indexing_enabled=self._config.review.indexing_enabled,
                max_retrieved_artifacts_per_task=self._config.review.max_retrieved_artifacts_per_task,
                max_retrieved_chars_per_task=self._config.review.max_retrieved_chars_per_task,
                max_retrieved_lines_per_artifact=self._config.review.max_retrieved_lines_per_artifact,
                cluster_retrieval_policies=self._config.review.cluster_retrieval_policies,
            )
            parallel = min(self._config.review.parallel_cluster_reviews, max(1, len(cluster_tasks)))
            self._write_detail('Parallel cluster reviews', str(parallel))
            await self._run_agent_tasks(
                stage_label='Cluster pass',
                agent_name='cluster-review-agent',
                tasks=cluster_tasks,
                model=selected_model,
                attempts_path=workspace.cluster_attempts,
                concurrency=self._config.review.parallel_cluster_reviews,
            )
            return {
                'cluster_reviews': await ReviewWorkspaceService.count_done_passes(workspace.cluster_findings_dir),
                'parallel_cluster_reviews': parallel,
            }

        cluster_data = await run_progress_stage(
            'cluster_passes',
            6,
            'Run cluster review passes',
            run_cluster_passes,
        )
        cluster_reviews = int(cluster_data.get('cluster_reviews', 0))
        if 'cluster_reviews' not in cluster_data:
            cluster_reviews = await ReviewWorkspaceService.count_done_passes(workspace.cluster_findings_dir)
        self._write_detail('Cluster passes', str(cluster_reviews))

        async def finalize_findings() -> dict[str, Any]:
            consolidation_result = await ReviewWorkspaceService.consolidate_findings(workspace)
            self._write_detail('Raw findings', str(consolidation_result['count']))
            self._write_detail('Invalid findings', str(consolidation_result['invalid']))
            self._write_detail('Validation notes', str(len(consolidation_result['anomalies'])))
            if consolidation_result['invalid']:
                add_warning(
                    f'Validation dropped {consolidation_result["invalid"]} malformed finding(s) before publish.'
                )
            if consolidation_result['anomalies']:
                add_warning(
                    f'Validation recorded {len(consolidation_result["anomalies"])} note(s); '
                    f'see {workspace.quality_report.name} for details.'
                )

            final_result = await ReviewCommentDeduplicator.deduplicate(
                workspace,
                self._dependencies.agent,
                selected_model,
                self._config.review.max_findings_total,
                self._config.review.dedup_line_window,
                self._config.review.parallel_dedup_reviews,
                self._dependencies.output,
            )
            if final_result['anomalies']:
                add_warning(
                    f'Deduplication recorded {len(final_result["anomalies"])} note(s); '
                    f'see {workspace.dedup_report.name} for details.'
                )
            return {
                'consolidation': consolidation_result,
                'final_findings': final_result,
            }

        finalization_data = await run_progress_stage(
            'finalize_findings',
            7,
            'Validate, consolidate, and deduplicate findings',
            finalize_findings,
        )
        consolidation = dict(finalization_data.get('consolidation') or {})
        final_findings = dict(finalization_data.get('final_findings') or {})
        self._write_detail('Nearby groups', str(final_findings.get('groups', 0)))
        self._write_detail('Dedup window', f'+/- {final_findings.get("line_window", 0)} lines')
        self._write_detail('Parallel dedup reviews', str(final_findings.get('parallel_reviews', 0)))
        self._write_detail('Deduplicated', str(final_findings.get('deduplicated', 0)))
        self._write_detail('Findings kept', str(final_findings.get('kept', 0)))
        self._write_detail('Dedup log', str(final_findings.get('report_path', workspace.dedup_report)))

        async def translate_results() -> dict[str, Any]:
            findings_for_publish = (
                await AsyncPathIO.read_json(workspace.all_findings)
                if await AsyncPathIO.exists(workspace.all_findings) else []
            )
            files_for_publish = (
                await AsyncPathIO.read_json(workspace.changed_files)
                if await AsyncPathIO.exists(workspace.changed_files) else []
            )
            prepared_publication = ReviewPublisher.prepare_publication(
                findings=findings_for_publish,
                files_data=files_for_publish,
            )
            translated, summary = await ReviewTranslationService.translate_publication(
                workspace,
                self._dependencies.agent,
                selected_model,
                prepared_publication,
                self._config.review.translation_language,
                self._config.review.parallel_translation_reviews,
                self._dependencies.output,
            )
            return {
                'translated_publication': translated,
                'translation_summary': summary,
            }

        translation_data = await run_progress_stage(
            'translation',
            8,
            'Translate publishable review output',
            translate_results,
        )
        translation_summary = dict(translation_data.get('translation_summary') or {})
        translated_publication = translation_data.get('translated_publication')
        if not isinstance(translated_publication, dict):
            translated_publication = (
                await AsyncPathIO.read_json(workspace.translated_publication)
                if await AsyncPathIO.exists(workspace.translated_publication) else {}
            )
        self._write_detail('Translation', str(translation_summary.get('status', 'unknown')))
        self._write_detail('Translation language', str(translation_summary.get('language', '')))
        self._write_detail('Translation jobs', str(translation_summary.get('jobs', 0)))
        self._write_detail('Parallel translations', str(translation_summary.get('parallel_reviews', 0)))
        self._write_detail(
            'Translation JSON',
            str(translation_summary.get('artifact_path', workspace.translated_publication)),
        )
        self._write_detail('Translation log', str(translation_summary.get('report_path', workspace.translation_report)))

        async def preview_results() -> dict[str, Any]:
            previewed, summary = await ReviewPreviewService.preview_publication(
                workspace,
                translated_publication,
                enabled=preview_mode,
                previewer=self._dependencies.previewer,
            )
            return {
                'preview_publication': previewed,
                'preview_summary': summary,
            }

        preview_data = await run_progress_stage(
            'preview',
            9,
            'Preview translated review findings',
            preview_results,
        )
        preview_summary = dict(preview_data.get('preview_summary') or {})
        preview_publication = preview_data.get('preview_publication')
        if not isinstance(preview_publication, dict):
            preview_publication = (
                await AsyncPathIO.read_json(workspace.preview_publication)
                if await AsyncPathIO.exists(workspace.preview_publication) else translated_publication
            )
        self._write_detail('Preview', str(preview_summary.get('status', 'unknown')))
        self._write_detail('Preview items', str(preview_summary.get('items', 0)))
        self._write_detail('Edited', str(preview_summary.get('edited', 0)))
        self._write_detail('Unpublished', str(preview_summary.get('unpublished', 0)))
        self._write_detail('Preview JSON', str(preview_summary.get('artifact_path', workspace.preview_publication)))
        self._write_detail('Preview log', str(preview_summary.get('report_path', workspace.preview_report)))

        async def publish_results() -> dict[str, Any]:
            retrieval_report = (
                await AsyncPathIO.read_json(workspace.retrieval_report)
                if await AsyncPathIO.exists(workspace.retrieval_report)
                else ReviewWorkspaceService._empty_retrieval_report()
            )
            publish_publication = (
                await AsyncPathIO.read_json(workspace.preview_publication)
                if await AsyncPathIO.exists(workspace.preview_publication)
                else preview_publication
            )
            existing_publish = await self._existing_publish_summary(workspace, publish_publication)
            if existing_publish is not None:
                publish_summary = existing_publish
            else:
                publish_summary = await ReviewPublisher.publish_prepared_review(
                    client=self._dependencies.gitlab,
                    publication=publish_publication,
                    project_path=project_path,
                    mr_iid=mr_iid,
                    base_sha=diff_refs['base_sha'],
                    start_sha=diff_refs['start_sha'],
                    head_sha=diff_refs['head_sha'],
                    result_path=workspace.publish_result,
                )
            if publish_summary['out_of_hunk_count']:
                add_warning(
                    f'{publish_summary["out_of_hunk_count"]} finding(s) were outside diff hunks and were only added '
                    f'to the final summary note.'
                )
            if publish_summary['overall_comment_status'] != 'ok':
                add_warning(
                    'Overall summary note could not be posted'
                    + (
                        f': {publish_summary["overall_comment_error"]}'
                        if publish_summary['overall_comment_error'] else '.'
                    )
                )
            await AsyncPathIO.write_json(
                workspace.quality_report,
                {
                    'model': selected_model,
                    'indexing': indexing_summary,
                    'retrieval': retrieval_report,
                    'cluster_plan': cluster_plan,
                    'summary_status': summary_status,
                    'consolidation': consolidation,
                    'final_findings': final_findings,
                    'deduplication': final_findings,
                    'translation': translation_summary,
                    'preview': preview_summary,
                    'publish': publish_summary,
                    'warnings': warnings,
                },
                indent=2,
            )
            return {'publish_summary': publish_summary}

        publish_data = await run_progress_stage(
            'publish',
            10,
            'Publish reviewed results',
            publish_results,
        )
        publish_summary = dict(publish_data.get('publish_summary') or {})
        self._write_detail('Inline notes', f'{publish_summary.get("posted", 0)} posted')
        self._write_detail('Anchor errors', str(publish_summary.get('anchor_errors', 0)))
        self._write_detail('Verdict', str(publish_summary.get('verdict', 'approve')))
        if publish_summary.get('overall_comment_status') == 'ok':
            note_suffix = (
                f' (note {publish_summary["overall_note_id"]})'
                if publish_summary.get('overall_note_id') else ''
            )
            self._write_detail('Overall comment', f'ok{note_suffix}')
        self._write_detail('Publish JSON', str(publish_summary.get('result_saved_to', workspace.publish_result)))

        result = ReviewResult(
            mr_url=str(mr.get('web_url', effective_mr_url)),
            project_path=project_path,
            mr_iid=mr_iid,
            workspace=workspace.root,
            files_changed=int(state['files_changed']),
            open_discussions=int(state['discussions_count']),
            eligible_files=eligible_count,
            review_passes=review_passes,
            cluster_reviews=cluster_reviews,
            findings_kept=int(final_findings.get('kept', 0)),
            invalid_findings=int(consolidation.get('invalid', 0)),
            posted=int(publish_summary.get('posted', 0)),
            anchor_errors=int(publish_summary.get('anchor_errors', 0)),
            out_of_hunk_findings=int(publish_summary.get('out_of_hunk_count', 0)),
            verdict=str(publish_summary.get('verdict', 'approve')),
            summary_status=summary_status,
            translation_status=str(translation_summary.get('status', 'unknown')),
            translation_language=str(translation_summary.get('language', '')),
            preview_status=str(preview_summary.get('status', 'unknown')),
            preview_edited=int(preview_summary.get('edited', 0)),
            preview_unpublished=int(preview_summary.get('unpublished', 0)),
            overall_comment_status=str(publish_summary.get('overall_comment_status', 'unknown')),
            overall_note_id=(
                str(publish_summary['overall_note_id'])
                if publish_summary.get('overall_note_id') is not None else None
            ),
            result_saved_to=str(publish_summary.get('result_saved_to', workspace.publish_result)),
            quality_report_path=str(workspace.quality_report),
            preview_report_path=str(preview_summary.get('report_path', workspace.preview_report)),
            model=selected_model,
            warnings=tuple(warnings),
        )
        await progress_store.mark_run_completed(result=self._jsonable(result), warnings=warnings)
        return result

    @classmethod
    async def _existing_publish_summary(
        cls,
        paths: WorkspacePaths,
        publication: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not await AsyncPathIO.exists(paths.publish_result):
            return None
        try:
            payload = await AsyncPathIO.read_json(paths.publish_result)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        inline_posts = payload.get('inline_posts', [])
        overall = payload.get('overall_comment', {})
        if not isinstance(inline_posts, list) or not isinstance(overall, dict):
            return None
        counts = publication.get('counts', {}) if isinstance(publication, dict) else {}
        review_verdict = str(publication.get('verdict', ReviewPublisher.verdict(counts)))
        return {
            'posted': sum(1 for post in inline_posts if isinstance(post, dict) and post.get('status') == 'ok'),
            'anchor_errors': sum(
                1 for post in inline_posts
                if isinstance(post, dict) and post.get('status') == 'anchor_error'
            ),
            'out_of_hunk_count': len(publication.get('out_of_hunk_findings', [])),
            'verdict': review_verdict,
            'findings_loaded': len(publication.get('findings', [])),
            'overall_comment_status': 'ok' if overall.get('status') == 'ok' else 'error',
            'overall_note_id': overall.get('note_id'),
            'overall_comment_error': overall.get('error'),
            'result_saved_to': str(paths.publish_result),
        }

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                key: cls._jsonable(item)
                for key, item in asdict(value).items()
            }
        if isinstance(value, dict):
            return {
                str(key): cls._jsonable(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(item) for item in value]
        return value

    @classmethod
    async def run(
        cls,
        mr_url: str,
        config: Config,
        gitlab: GitLabPort,
        agent: AgentPort,
        output: ReviewOutputPort,
        workspace_provisioner: WorkspaceProvisioner = WorkspaceBuilder.setup_workspace,
        model: str | None = None,
        options: ReviewRunOptions | None = None,
        previewer: ReviewPreviewPort | None = None,
    ) -> ReviewResult:
        use_case = cls(
            config,
            ReviewDependencies(
                gitlab=gitlab,
                agent=agent,
                output=output,
                previewer=previewer,
                workspace_provisioner=workspace_provisioner,
            ),
        )
        return await use_case.execute(mr_url, model=model, options=options)

    def _write_step(self, index: int, total: int, title: str) -> None:
        self._dependencies.output.step_started(index, total, title)

    def _write_detail(self, label: str, value: str) -> None:
        self._dependencies.output.detail(label, value)

    def _write_warning(self, message: str) -> None:
        self._dependencies.output.warning(message)

    async def _run_agent_tasks(
        self,
        stage_label: str,
        agent_name: str,
        tasks: list[ReviewTask],
        model: str,
        attempts_path: Path,
        concurrency: int,
    ) -> int:
        if not tasks:
            return 0

        concurrency = min(concurrency, len(tasks))
        semaphore = asyncio.Semaphore(concurrency)
        progress_lock = asyncio.Lock()
        active_tasks: dict[str, TaskProgressItem] = {}
        completed_count = 0

        def _task_item(index: int, task: ReviewTask) -> TaskProgressItem:
            return TaskProgressItem(
                index=index,
                total=len(tasks),
                subject=task.file_path,
                activity=task.pass_id,
            )

        def _task_progress(event: str, task_item: TaskProgressItem) -> TaskProgress:
            return TaskProgress(
                stage_label=stage_label,
                event=event,
                task=task_item,
                active_tasks=tuple(active_tasks.values()),
                completed_count=completed_count,
                total_count=len(tasks),
                parallel_limit=concurrency,
            )

        async def _run_one(index: int, task: ReviewTask) -> None:
            nonlocal completed_count
            async with semaphore:
                task_item = _task_item(index, task)
                async with progress_lock:
                    await ReviewWorkspaceService.record_attempt(attempts_path, task)
                    active_tasks[task.key] = task_item
                    self._dependencies.output.task_progress(_task_progress('started', task_item))
                try:
                    await self._dependencies.agent.run(agent_name, str(task.input_path), model)
                    await ReviewWorkspaceService.stub_missing_findings(task, agent_name)
                except Exception:
                    async with progress_lock:
                        active_tasks.pop(task.key, None)
                        self._dependencies.output.task_progress(_task_progress('failed', task_item))
                    raise
                async with progress_lock:
                    active_tasks.pop(task.key, None)
                    completed_count += 1
                    self._dependencies.output.task_progress(_task_progress('completed', task_item))

        await asyncio.gather(
            *(
                _run_one(index, task)
                for index, task in enumerate(tasks, start=1)
            )
        )
        return len(tasks)


parse_mr_url = MergeRequestUrlParser.parse
collect_review_state = ReviewWorkspaceService.collect_review_state
create_summary_input = ReviewWorkspaceService.create_summary_input
gate_changed_files = ReviewWorkspaceService.gate_changed_files
next_review_task = ReviewWorkspaceService.next_review_task
finalize_findings = ReviewWorkspaceService.finalize_findings
run_review = ReviewMergeRequestUseCase.run

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from application.dto import TaskProgress, TaskProgressItem
from application.ports import AgentPort, ReviewOutputPort
from domain.models import ExistingComment, Finding
from domain.review.comment_dedup import CommentDedupPlanner
from domain.workspace import WorkspacePaths
from runtime.async_ops import AsyncPathIO


@dataclass(frozen=True)
class DedupGroupJob:
    ordinal: int
    group: dict
    input_path: Path
    result_path: Path


@dataclass(frozen=True)
class DedupGroupOutcome:
    group: dict
    keep_indexes: list[int]
    duplicates_by_index: dict[int, dict]
    anomaly: str | None = None


class ReviewCommentDeduplicator:
    @classmethod
    async def deduplicate(
        cls,
        paths: WorkspacePaths,
        agent: AgentPort,
        model: str,
        max_findings_total: int,
        line_window: int,
        parallel_reviews: int = 1,
        output: ReviewOutputPort | None = None,
    ) -> dict[str, object]:
        findings: list[Finding] = (
            await AsyncPathIO.read_json(paths.raw_findings)
            if await AsyncPathIO.exists(paths.raw_findings) else []
        )
        findings = sorted(findings, key=CommentDedupPlanner.sort_key, reverse=True)
        existing_comments: list[ExistingComment] = (
            await AsyncPathIO.read_json(paths.existing_comments)
            if await AsyncPathIO.exists(paths.existing_comments) else []
        )

        await AsyncPathIO.mkdir(paths.dedup_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.dedup_results_dir, parents=True, exist_ok=True)

        groups = CommentDedupPlanner.build_groups(findings, existing_comments, line_window=line_window)
        jobs: list[DedupGroupJob] = []
        for ordinal, group in enumerate(groups, start=1):
            input_path = paths.dedup_inputs_dir / f'{ordinal:03d}.json'
            result_path = paths.dedup_results_dir / f'{ordinal:03d}.json'
            await AsyncPathIO.write_text(
                input_path,
                json.dumps(
                    {
                        'result_path': str(result_path),
                        'group': group,
                    },
                    indent=2,
                ),
            )
            jobs.append(
                DedupGroupJob(
                    ordinal=ordinal,
                    group=group,
                    input_path=input_path,
                    result_path=result_path,
                )
            )

        concurrency = min(parallel_reviews, len(jobs)) if jobs else 0
        outcomes = await cls._run_group_jobs(
            jobs=jobs,
            agent=agent,
            model=model,
            concurrency=concurrency,
            output=output,
        )

        keep_indexes: set[int] = set()
        deduplicated_comments: list[dict] = []
        anomalies: list[str] = []
        group_reports: list[dict] = []

        for outcome, job in zip(outcomes, jobs, strict=True):
            group = outcome.group
            result_path = job.result_path
            group_keep_indexes = outcome.keep_indexes
            duplicates_by_index = outcome.duplicates_by_index
            if outcome.anomaly:
                anomalies.append(outcome.anomaly)
            keep_indexes.update(group_keep_indexes)
            valid_indexes = {
                int(comment['index'])
                for comment in group.get('new_comments', [])
            }
            dropped_indexes = sorted(valid_indexes.difference(group_keep_indexes))
            dropped_lookup = {
                int(comment['index']): comment
                for comment in group.get('new_comments', [])
            }
            for dropped_index in dropped_indexes:
                duplicate_meta = duplicates_by_index.get(dropped_index, {})
                dropped_comment = dropped_lookup[dropped_index]
                deduplicated_comments.append(
                    {
                        'group_id': group['group_id'],
                        'index': dropped_index,
                        'file_path': group['file_path'],
                        'line': dropped_comment.get('line'),
                        'short_title': dropped_comment.get('short_title', ''),
                        'duplicate_of': duplicate_meta.get('duplicate_of'),
                        'reason': duplicate_meta.get('reason', 'removed by dedup agent'),
                    }
                )

            group_reports.append(
                {
                    'group_id': group['group_id'],
                    'file_path': group['file_path'],
                    'start_line': group.get('start_line'),
                    'end_line': group.get('end_line'),
                    'new_comment_indexes': sorted(valid_indexes),
                    'kept_indexes': group_keep_indexes,
                    'dropped_indexes': dropped_indexes,
                    'existing_comment_ids': [
                        comment.get('note_id')
                        for comment in group.get('existing_comments', [])
                        if comment.get('note_id') is not None
                    ],
                    'result_path': str(result_path),
                }
            )

        publishable_indexes = sorted(keep_indexes)
        deduplicated_findings = [
            finding
            for index, finding in enumerate(findings, start=1)
            if index in keep_indexes
        ]
        kept_findings = deduplicated_findings[:max_findings_total]
        published_indexes = publishable_indexes[:max_findings_total]
        truncated_indexes = publishable_indexes[max_findings_total:]
        await AsyncPathIO.write_text(paths.all_findings, json.dumps(kept_findings, indent=2))

        report = {
            'input_findings': len(findings),
            'groups': len(groups),
            'line_window': line_window,
            'parallel_reviews': concurrency,
            'existing_comments_considered': len(existing_comments),
            'remaining_after_dedup': len(deduplicated_findings),
            'published_after_limit': len(kept_findings),
            'deduplicated_comments': deduplicated_comments,
            'truncated_indexes': truncated_indexes,
            'published_indexes': published_indexes,
            'anomalies': anomalies,
            'group_reports': group_reports,
        }
        await AsyncPathIO.write_text(paths.dedup_report, json.dumps(report, indent=2))
        return {
            'kept': len(kept_findings),
            'total': len(findings),
            'deduplicated': len(deduplicated_comments),
            'groups': len(groups),
            'line_window': line_window,
            'parallel_reviews': concurrency,
            'anomalies': anomalies,
            'report_path': str(paths.dedup_report),
        }

    @classmethod
    async def _run_group_jobs(
        cls,
        jobs: list[DedupGroupJob],
        agent: AgentPort,
        model: str,
        concurrency: int,
        output: ReviewOutputPort | None = None,
    ) -> list[DedupGroupOutcome]:
        if not jobs:
            return []

        semaphore = asyncio.Semaphore(concurrency)
        progress_lock = asyncio.Lock()
        active_tasks: dict[int, TaskProgressItem] = {}
        completed_count = 0

        def _task_item(job: DedupGroupJob) -> TaskProgressItem:
            return TaskProgressItem(
                index=job.ordinal,
                total=len(jobs),
                subject=str(job.group.get('file_path', '')),
                activity=cls._group_activity(job.group),
            )

        def _task_progress(event: str, task_item: TaskProgressItem) -> TaskProgress:
            return TaskProgress(
                stage_label='Dedup group',
                event=event,
                task=task_item,
                active_tasks=tuple(active_tasks.values()),
                completed_count=completed_count,
                total_count=len(jobs),
                parallel_limit=concurrency,
            )

        async def _run_one(job: DedupGroupJob) -> DedupGroupOutcome:
            nonlocal completed_count
            existing = await cls._load_existing_outcome(job)
            if existing is not None:
                return existing
            async with semaphore:
                task_item = _task_item(job)
                if output is not None:
                    async with progress_lock:
                        active_tasks[job.ordinal] = task_item
                        output.task_progress(_task_progress('started', task_item))
                try:
                    await agent.run('deduplicate-comments-agent', str(job.input_path), model)
                    if not await AsyncPathIO.exists(job.result_path):
                        raise RuntimeError(f'dedup agent did not write {job.result_path.name}')
                    result = await AsyncPathIO.read_json(job.result_path)
                    keep_indexes, duplicates_by_index = cls._parse_group_result(job.group, result)
                    outcome = DedupGroupOutcome(
                        group=job.group,
                        keep_indexes=keep_indexes,
                        duplicates_by_index=duplicates_by_index,
                    )
                except Exception as exc:
                    outcome = DedupGroupOutcome(
                        group=job.group,
                        keep_indexes=[
                            int(comment['index'])
                            for comment in job.group.get('new_comments', [])
                        ],
                        duplicates_by_index={},
                        anomaly=f'{job.group["group_id"]}: dedup skipped ({exc})',
                    )
                    if output is not None:
                        async with progress_lock:
                            active_tasks.pop(job.ordinal, None)
                            completed_count += 1
                            output.task_progress(_task_progress('failed', task_item))
                    return outcome

                if output is not None:
                    async with progress_lock:
                        active_tasks.pop(job.ordinal, None)
                        completed_count += 1
                        output.task_progress(_task_progress('completed', task_item))
                return outcome

        return await asyncio.gather(*(_run_one(job) for job in jobs))

    @classmethod
    async def _load_existing_outcome(cls, job: DedupGroupJob) -> DedupGroupOutcome | None:
        if not await AsyncPathIO.exists(job.result_path):
            return None
        try:
            result = await AsyncPathIO.read_json(job.result_path)
            keep_indexes, duplicates_by_index = cls._parse_group_result(job.group, result)
        except Exception:
            return None
        return DedupGroupOutcome(
            group=job.group,
            keep_indexes=keep_indexes,
            duplicates_by_index=duplicates_by_index,
        )

    @classmethod
    def _group_activity(cls, group: dict) -> str:
        group_id = str(group.get('group_id', 'group'))
        start_line = group.get('start_line')
        end_line = group.get('end_line')
        if start_line is None:
            return group_id
        if start_line == end_line:
            return f'{group_id} line {start_line}'
        return f'{group_id} lines {start_line}-{end_line}'

    @classmethod
    def _parse_group_result(
        cls,
        group: dict,
        result: dict,
    ) -> tuple[list[int], dict[int, dict]]:
        valid_indexes = {
            int(comment['index'])
            for comment in group.get('new_comments', [])
        }
        keep_raw = result.get('unique_comment_indexes')
        if not isinstance(keep_raw, list) or any(not isinstance(index, int) for index in keep_raw):
            raise ValueError('dedup result is missing valid unique_comment_indexes')

        keep_indexes = sorted({index for index in keep_raw if index in valid_indexes})
        unknown_indexes = sorted(set(keep_raw).difference(valid_indexes))
        if unknown_indexes:
            raise ValueError(f'dedup result returned unknown indexes: {unknown_indexes}')

        duplicates_by_index: dict[int, dict] = {}
        duplicates = result.get('duplicates', [])
        if isinstance(duplicates, list):
            for duplicate in duplicates:
                if not isinstance(duplicate, dict):
                    continue
                index = duplicate.get('index')
                if isinstance(index, int) and index in valid_indexes:
                    duplicates_by_index[index] = duplicate

        return keep_indexes, duplicates_by_index

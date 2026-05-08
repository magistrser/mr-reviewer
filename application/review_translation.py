from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from application.dto import TaskProgress, TaskProgressItem
from application.ports import AgentPort, ReviewOutputPort
from domain.models import PreparedReviewPublication
from domain.workspace import WorkspacePaths
from runtime.async_ops import AsyncPathIO


@dataclass(frozen=True)
class TranslationJob:
    ordinal: int
    kind: str
    subject: str
    input_path: Path
    result_path: Path
    finding_index: int | None = None


@dataclass(frozen=True)
class TranslationJobOutcome:
    job: TranslationJob
    payload: dict[str, Any]


class ReviewTranslationService:
    SKIP_LANGUAGE = 'ENG'

    @classmethod
    async def translate_publication(
        cls,
        paths: WorkspacePaths,
        agent: AgentPort,
        model: str,
        publication: PreparedReviewPublication,
        target_language: str,
        parallel_reviews: int = 1,
        output: ReviewOutputPort | None = None,
    ) -> tuple[PreparedReviewPublication, dict[str, object]]:
        await AsyncPathIO.mkdir(paths.translation_inputs_dir, parents=True, exist_ok=True)
        await AsyncPathIO.mkdir(paths.translation_results_dir, parents=True, exist_ok=True)

        language = cls._normalize_language(target_language)
        if language == cls.SKIP_LANGUAGE:
            await AsyncPathIO.write_json(paths.translated_publication, publication, indent=2)
            report = {
                'status': 'skipped',
                'language': language,
                'jobs': 0,
                'finding_jobs': len(publication.get('findings', [])),
                'parallel_reviews': 0,
                'job_reports': [],
                'artifact_path': str(paths.translated_publication),
            }
            await AsyncPathIO.write_json(paths.translation_report, report, indent=2)
            return publication, {
                **report,
                'report_path': str(paths.translation_report),
            }

        jobs = await cls._build_jobs(paths, publication, language)
        concurrency = min(parallel_reviews, len(jobs))
        outcomes = await cls._run_jobs(
            jobs=jobs,
            agent=agent,
            model=model,
            concurrency=concurrency,
            output=output,
            target_language=language,
        )
        translated_publication = json.loads(json.dumps(publication))
        job_reports: list[dict[str, object]] = []

        for outcome in outcomes:
            job = outcome.job
            payload = outcome.payload
            if job.kind == 'summary_labels':
                translated_publication['summary_labels'] = payload['summary_labels']
            else:
                if job.finding_index is None:
                    raise RuntimeError(f'{job.subject}: finding translation is missing finding_index')
                translated_publication['findings'][job.finding_index]['short_title'] = payload['short_title']
                translated_publication['findings'][job.finding_index]['body'] = payload['body']
            job_reports.append(
                {
                    'ordinal': job.ordinal,
                    'kind': job.kind,
                    'subject': job.subject,
                    'input_path': str(job.input_path),
                    'result_path': str(job.result_path),
                }
            )

        await AsyncPathIO.write_json(paths.translated_publication, translated_publication, indent=2)
        report = {
            'status': 'translated',
            'language': language,
            'jobs': len(jobs),
            'finding_jobs': len(publication.get('findings', [])),
            'parallel_reviews': concurrency,
            'job_reports': job_reports,
            'artifact_path': str(paths.translated_publication),
        }
        await AsyncPathIO.write_json(paths.translation_report, report, indent=2)
        return translated_publication, {
            **report,
            'report_path': str(paths.translation_report),
        }

    @classmethod
    async def _build_jobs(
        cls,
        paths: WorkspacePaths,
        publication: PreparedReviewPublication,
        target_language: str,
    ) -> list[TranslationJob]:
        jobs: list[TranslationJob] = []
        summary_input_path = paths.translation_inputs_dir / '000-summary-labels.json'
        summary_result_path = paths.translation_results_dir / '000-summary-labels.json'
        await AsyncPathIO.write_json(
            summary_input_path,
            {
                'result_path': str(summary_result_path),
                'kind': 'summary_labels',
                'target_language': target_language,
                'summary_labels': publication.get('summary_labels', {}),
            },
            indent=2,
        )
        jobs.append(
            TranslationJob(
                ordinal=1,
                kind='summary_labels',
                subject='summary note',
                input_path=summary_input_path,
                result_path=summary_result_path,
            )
        )

        for index, finding in enumerate(publication.get('findings', []), start=1):
            ordinal = len(jobs) + 1
            key = f'{index:03d}'
            input_path = paths.translation_inputs_dir / f'{key}-finding.json'
            result_path = paths.translation_results_dir / f'{key}-finding.json'
            anchor = finding.get('anchor', {})
            subject = f'{anchor.get("new_path", "?")}:{anchor.get("new_line", "?")}'
            await AsyncPathIO.write_json(
                input_path,
                {
                    'result_path': str(result_path),
                    'kind': 'finding',
                    'target_language': target_language,
                    'finding': {
                        'file_path': anchor.get('new_path') or anchor.get('old_path') or '',
                        'line': (
                            anchor.get('new_line')
                            if anchor.get('new_line') is not None
                            else anchor.get('old_line')
                        ),
                        'short_title': finding.get('short_title', ''),
                        'body': finding.get('body', ''),
                    },
                },
                indent=2,
            )
            jobs.append(
                TranslationJob(
                    ordinal=ordinal,
                    kind='finding',
                    subject=subject,
                    input_path=input_path,
                    result_path=result_path,
                    finding_index=index - 1,
                )
            )
        return jobs

    @classmethod
    async def _run_jobs(
        cls,
        jobs: list[TranslationJob],
        agent: AgentPort,
        model: str,
        concurrency: int,
        output: ReviewOutputPort | None,
        target_language: str,
    ) -> list[TranslationJobOutcome]:
        semaphore = asyncio.Semaphore(concurrency)
        progress_lock = asyncio.Lock()
        active_tasks: dict[int, TaskProgressItem] = {}
        completed_count = 0

        def _task_item(job: TranslationJob) -> TaskProgressItem:
            return TaskProgressItem(
                index=job.ordinal,
                total=len(jobs),
                subject=job.subject,
                activity=f'{job.kind}:{target_language}',
            )

        def _task_progress(event: str, task_item: TaskProgressItem) -> TaskProgress:
            return TaskProgress(
                stage_label='Translation',
                event=event,
                task=task_item,
                active_tasks=tuple(active_tasks.values()),
                completed_count=completed_count,
                total_count=len(jobs),
                parallel_limit=concurrency,
            )

        async def _run_one(job: TranslationJob) -> TranslationJobOutcome:
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
                    await agent.run('translate-review-agent', str(job.input_path), model)
                    if not await AsyncPathIO.exists(job.result_path):
                        raise RuntimeError(f'translation agent did not write {job.result_path.name}')
                    result = await AsyncPathIO.read_json(job.result_path)
                    outcome = TranslationJobOutcome(job=job, payload=cls._parse_result(job, result))
                except Exception:
                    if output is not None:
                        async with progress_lock:
                            active_tasks.pop(job.ordinal, None)
                            output.task_progress(_task_progress('failed', task_item))
                    raise

                if output is not None:
                    async with progress_lock:
                        active_tasks.pop(job.ordinal, None)
                        completed_count += 1
                        output.task_progress(_task_progress('completed', task_item))
                return outcome

        return await asyncio.gather(*(_run_one(job) for job in jobs))

    @classmethod
    async def _load_existing_outcome(cls, job: TranslationJob) -> TranslationJobOutcome | None:
        if not await AsyncPathIO.exists(job.result_path):
            return None
        try:
            result = await AsyncPathIO.read_json(job.result_path)
            payload = cls._parse_result(job, result)
        except Exception:
            return None
        return TranslationJobOutcome(job=job, payload=payload)

    @classmethod
    def _parse_result(cls, job: TranslationJob, result: dict[str, Any]) -> dict[str, Any]:
        if job.kind == 'summary_labels':
            labels = result.get('summary_labels')
            if not isinstance(labels, dict):
                raise ValueError('summary label translation result is missing summary_labels')
            normalized: dict[str, Any] = {}
            for key, value in labels.items():
                if key in ('severity_labels', 'verdict_values'):
                    if not isinstance(value, dict):
                        raise ValueError(f'summary label translation result field {key} must be an object')
                    nested: dict[str, str] = {}
                    for nested_key, nested_value in value.items():
                        if not isinstance(nested_value, str):
                            raise ValueError(
                                f'summary label translation result field {key}.{nested_key} must be a string'
                            )
                        nested[str(nested_key)] = nested_value
                    normalized[key] = nested
                    continue
                if not isinstance(value, str):
                    raise ValueError(f'summary label translation result field {key} must be a string')
                normalized[key] = value
            return {'summary_labels': normalized}

        short_title = result.get('short_title')
        body = result.get('body')
        if not isinstance(short_title, str):
            raise ValueError('finding translation result is missing short_title')
        if not isinstance(body, str):
            raise ValueError('finding translation result is missing body')
        return {
            'short_title': short_title,
            'body': body,
        }

    @classmethod
    def _normalize_language(cls, target_language: str) -> str:
        normalized = target_language.strip()
        if normalized.upper() == cls.SKIP_LANGUAGE:
            return cls.SKIP_LANGUAGE
        return normalized

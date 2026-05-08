from __future__ import annotations

from typing import Any, Callable

from application.dto import ReviewResult, TaskProgress
from application.ports import ReviewOutputPort


class JobReviewOutput(ReviewOutputPort):
    def __init__(self, job_id: str, update_job: Callable[..., Any]) -> None:
        self._job_id = job_id
        self._update_job = update_job
        self._warnings: list[str] = []

    def step_started(self, index: int, total: int, title: str) -> None:
        self._update_job(
            self._job_id,
            progress={
                'kind': 'step',
                'index': index,
                'total': total,
                'title': title,
            },
        )

    def detail(self, label: str, value: str) -> None:
        self._update_job(
            self._job_id,
            progress={
                'kind': 'detail',
                'label': label,
                'value': value,
            },
        )

    def warning(self, message: str) -> None:
        self._warnings.append(message)
        self._update_job(self._job_id, warnings=tuple(self._warnings))

    def task_progress(self, progress: TaskProgress) -> None:
        self._update_job(
            self._job_id,
            progress={
                'kind': 'task',
                'stage_label': progress.stage_label,
                'event': progress.event,
                'completed_count': progress.completed_count,
                'total_count': progress.total_count,
                'parallel_limit': progress.parallel_limit,
                'pending_count': progress.pending_count,
                'task': {
                    'index': progress.task.index,
                    'total': progress.task.total,
                    'subject': progress.task.subject,
                    'activity': progress.task.activity,
                },
                'active_tasks': [
                    {
                        'index': item.index,
                        'total': item.total,
                        'subject': item.subject,
                        'activity': item.activity,
                    }
                    for item in progress.active_tasks
                ],
            },
        )

    def completed(self, result: ReviewResult) -> None:
        return None

    def failed(self, exc: BaseException) -> None:
        return None

    def cancelled(self) -> None:
        return None

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewRunOptions:
    model: str | None = None
    preview_mode: bool | None = False
    resume_workspace: Path | None = None


@dataclass(frozen=True)
class ReviewTask:
    ordinal: int
    pass_id: str
    file_path: str
    input_path: Path
    completed_steps: int
    total_steps: int
    focus_area: str = ''
    source_kind: str = 'file'
    findings_path: Path | None = None
    subject_key: str = ''

    @property
    def key(self) -> str:
        return f'{self.ordinal:03d}-{self.pass_id}'


@dataclass(frozen=True)
class TaskProgressItem:
    index: int
    total: int
    subject: str
    activity: str = ''

    @property
    def label(self) -> str:
        if self.activity:
            return f'{self.subject} ({self.activity})'
        return self.subject


@dataclass(frozen=True)
class TaskProgress:
    stage_label: str
    event: str
    task: TaskProgressItem
    active_tasks: tuple[TaskProgressItem, ...]
    completed_count: int
    total_count: int
    parallel_limit: int

    @property
    def pending_count(self) -> int:
        return max(self.total_count - self.completed_count - len(self.active_tasks), 0)


@dataclass(frozen=True)
class ReviewResult:
    mr_url: str
    project_path: str
    mr_iid: str
    workspace: Path
    files_changed: int
    open_discussions: int
    eligible_files: int
    review_passes: int
    cluster_reviews: int
    findings_kept: int
    invalid_findings: int
    posted: int
    anchor_errors: int
    out_of_hunk_findings: int
    verdict: str
    summary_status: str
    translation_status: str
    translation_language: str
    preview_status: str
    preview_edited: int
    preview_unpublished: int
    overall_comment_status: str
    overall_note_id: str | None
    result_saved_to: str
    quality_report_path: str
    preview_report_path: str
    model: str
    warnings: tuple[str, ...]

from __future__ import annotations

import sys
from typing import TextIO

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except ModuleNotFoundError:
    box = Console = Group = Live = Panel = Table = None
    HAS_RICH = False

from application.dto import ReviewResult, TaskProgress


class _PlainConsole:
    def __init__(self, file: TextIO) -> None:
        self._file = file

    def print(self, message: str, style: str | None = None) -> None:
        self._file.write(f'{message}\n')


class ConsoleReviewOutput:
    def __init__(
        self,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
        dynamic: bool | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._dynamic = self._detect_dynamic_output(stdout) if dynamic is None else dynamic
        if HAS_RICH:
            self._console = Console(
                file=stdout,
                force_terminal=self._dynamic,
                force_interactive=self._dynamic,
                soft_wrap=True,
                markup=False,
                highlight=False,
            )
            self._stderr_console = Console(
                file=stderr,
                stderr=True,
                force_terminal=self._detect_dynamic_output(stderr),
                soft_wrap=True,
                markup=False,
                highlight=False,
            )
        else:
            self._console = _PlainConsole(stdout)
            self._stderr_console = _PlainConsole(stderr)
        self._live: Live | None = None
        self._live_lines: list[str] | None = None

    def step_started(self, index: int, total: int, title: str) -> None:
        self._seal_live_block()
        self._print('')
        self._print(f'[{index}/{total}] {title}')

    def detail(self, label: str, value: str) -> None:
        self._seal_live_block()
        if label:
            self._print(f'  {label:<17} {value}')
            return
        self._print(value)

    def warning(self, message: str) -> None:
        self._seal_live_block()
        self._stderr_console.print(f'  Warning           {message}', style='yellow')

    def task_progress(self, progress: TaskProgress) -> None:
        status = {
            'started': 'started',
            'completed': 'finished',
            'failed': 'failed',
        }.get(progress.event, progress.event)
        task_status = (
            f'  {progress.stage_label:<17} '
            f'[{progress.task.index}/{progress.task.total}] {status} {progress.task.label}'
        )
        lines = [
            task_status,
            (
                f'  {"Progress":<17} {progress.completed_count} done, {len(progress.active_tasks)} active '
                f'of {progress.parallel_limit}, {progress.pending_count} pending'
            ),
            f'  {"In progress":<17} {", ".join(task.label for task in progress.active_tasks) or "none"}',
        ]
        if not self._dynamic:
            for line in lines:
                self._print(line)
            return
        if not HAS_RICH:
            self._live_lines = lines
            return

        renderable = self._render_progress(progress, status)
        if self._live is None:
            self._live = Live(
                renderable,
                console=self._console,
                auto_refresh=False,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=True)

    def print_summary(self, result: ReviewResult) -> None:
        self._seal_live_block()
        for line in self.summary_lines(result):
            self._print(line)

    def print_error(self, exc: BaseException) -> None:
        self._seal_live_block()
        for line in self.error_lines(exc):
            self._stderr_console.print(line)

    def completed(self, result: ReviewResult) -> None:
        self.print_summary(result)

    def failed(self, exc: BaseException) -> None:
        self.print_error(exc)

    def cancelled(self) -> None:
        self._seal_live_block()
        self._stderr_console.print('')
        self._stderr_console.print('Review cancelled by user.')

    @staticmethod
    def summary_lines(result: ReviewResult) -> list[str]:
        overall_comment = result.overall_comment_status
        if result.overall_note_id:
            overall_comment = f'{overall_comment} (note {result.overall_note_id})'

        return [
            '',
            'Review complete',
            '',
            'Overview',
            f'  {"MR":<17} {result.project_path}!{result.mr_iid}',
            f'  {"URL":<17} {result.mr_url}',
            f'  {"Model":<17} {result.model}',
            f'  {"Verdict":<17} {result.verdict}',
            f'  {"Summary":<17} {result.summary_status}',
            f'  {"Translation":<17} {result.translation_status} ({result.translation_language})',
            f'  {"Preview":<17} {result.preview_status}',
            '',
            'Scope',
            f'  {"Changed files":<17} {result.files_changed}',
            f'  {"Eligible files":<17} {result.eligible_files}',
            f'  {"Open discussions":<17} {result.open_discussions}',
            f'  {"Review passes":<17} {result.review_passes}',
            f'  {"Cluster passes":<17} {result.cluster_reviews}',
            '',
            'Results',
            f'  {"Findings kept":<17} {result.findings_kept}',
            f'  {"Invalid findings":<17} {result.invalid_findings}',
            f'  {"Preview edits":<17} {result.preview_edited}',
            f'  {"Preview hidden":<17} {result.preview_unpublished}',
            f'  {"Inline notes":<17} {result.posted} posted',
            f'  {"Anchor errors":<17} {result.anchor_errors}',
            f'  {"Out of hunk":<17} {result.out_of_hunk_findings}',
            f'  {"Overall comment":<17} {overall_comment}',
            '',
            'Artifacts',
            f'  {"Workspace":<17} {result.workspace}',
            f'  {"Preview report":<17} {result.preview_report_path}',
            f'  {"Publish JSON":<17} {result.result_saved_to}',
            f'  {"Quality report":<17} {result.quality_report_path}',
            *ConsoleReviewOutput._warning_lines(result.warnings),
        ]

    @staticmethod
    def error_lines(exc: BaseException) -> list[str]:
        return [
            '',
            'Review failed',
            f'  {"Error":<17} {exc.__class__.__name__}: {exc}',
        ]

    @staticmethod
    def _warning_lines(warnings: tuple[str, ...]) -> list[str]:
        if not warnings:
            return []
        return [
            '',
            'Warnings',
            *(f'  - {warning}' for warning in warnings),
        ]

    def _print(self, message: str) -> None:
        self._console.print(message)

    @staticmethod
    def _detect_dynamic_output(stream: TextIO) -> bool:
        isatty = getattr(stream, 'isatty', None)
        if not callable(isatty):
            return False
        try:
            return bool(isatty())
        except Exception:
            return False

    def _seal_live_block(self) -> None:
        if self._live_lines is not None:
            for line in self._live_lines:
                self._print(line)
            self._live_lines = None
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def _render_progress(self, progress: TaskProgress, status: str):
        summary = Table.grid(expand=True)
        summary.add_column(style='cyan', no_wrap=True)
        summary.add_column(ratio=1)
        summary.add_row('Event', f'[{progress.task.index}/{progress.task.total}] {status} {progress.task.label}')
        summary.add_row(
            'Progress',
            (
                f'{progress.completed_count} done, {len(progress.active_tasks)} active '
                f'of {progress.parallel_limit}, {progress.pending_count} pending'
            ),
        )

        active = Table(
            box=box.SIMPLE_HEAVY,
            expand=True,
            show_edge=False,
            pad_edge=False,
            title='In progress',
            title_style='bold',
        )
        active.add_column('#', justify='right', style='cyan', no_wrap=True)
        active.add_column('Subject', overflow='fold')
        active.add_column('Task', style='magenta', no_wrap=True)

        if progress.active_tasks:
            for task in progress.active_tasks:
                active.add_row(f'{task.index}/{task.total}', task.subject, task.activity or '-')
        else:
            active.add_row('-', 'none', '-')

        return Panel(
            Group(summary, active),
            title=progress.stage_label,
            border_style='cyan',
            padding=(0, 1),
        )

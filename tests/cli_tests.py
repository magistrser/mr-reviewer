from __future__ import annotations

import io
import unittest
from pathlib import Path

from application.dto import ReviewResult, TaskProgress, TaskProgressItem
from infrastructure.cli.main import CliApplication
from infrastructure.cli.output import ConsoleReviewOutput


class CliSummaryTests(unittest.TestCase):
    def test_summary_lines_include_scope_results_and_warnings(self) -> None:
        result = ReviewResult(
            mr_url='https://gitlab.example.com/group/project/-/merge_requests/42',
            project_path='group/project',
            mr_iid='42',
            workspace=Path('/tmp/workspace'),
            files_changed=12,
            open_discussions=3,
            eligible_files=5,
            review_passes=8,
            cluster_reviews=2,
            findings_kept=4,
            invalid_findings=1,
            posted=3,
            anchor_errors=1,
            out_of_hunk_findings=1,
            verdict='comment',
            summary_status='written',
            translation_status='translated',
            translation_language='RUS',
            preview_status='reviewed',
            preview_edited=2,
            preview_unpublished=1,
            overall_comment_status='ok',
            overall_note_id='123',
            result_saved_to='/tmp/publish.json',
            quality_report_path='/tmp/quality-report.json',
            preview_report_path='/tmp/preview-report.json',
            model='gpt-test',
            warnings=('Summary skipped: example',),
        )

        lines = ConsoleReviewOutput.summary_lines(result)
        text = '\n'.join(lines)

        self.assertIn('Review complete', text)
        self.assertIn('group/project!42', text)
        self.assertIn('Changed files', text)
        self.assertIn('Cluster passes', text)
        self.assertIn('translated (RUS)', text)
        self.assertIn('reviewed', text)
        self.assertIn('Findings kept', text)
        self.assertIn('Preview edits', text)
        self.assertIn('Invalid findings', text)
        self.assertIn('Overall comment', text)
        self.assertIn('ok (note 123)', text)
        self.assertIn('Preview report', text)
        self.assertIn('Quality report', text)
        self.assertIn('Warnings', text)
        self.assertIn('Summary skipped: example', text)

    def test_error_lines_are_user_friendly(self) -> None:
        lines = ConsoleReviewOutput.error_lines(ValueError('bad config'))
        text = '\n'.join(lines)

        self.assertIn('Review failed', text)
        self.assertIn('ValueError: bad config', text)

    def test_failed_writes_to_stderr_console(self) -> None:
        stderr = io.StringIO()
        output = ConsoleReviewOutput(stderr=stderr)

        output.failed(ValueError('bad config'))

        text = stderr.getvalue()
        self.assertIn('Review failed', text)
        self.assertIn('ValueError: bad config', text)

    def test_cancelled_writes_to_stderr_console(self) -> None:
        stderr = io.StringIO()
        output = ConsoleReviewOutput(stderr=stderr)

        output.cancelled()

        self.assertIn('Review cancelled by user.', stderr.getvalue())

    def test_task_progress_includes_active_files(self) -> None:
        stdout = io.StringIO()
        output = ConsoleReviewOutput(stdout=stdout)
        first = TaskProgressItem(index=1, total=4, subject='src/a.py', activity='correctness')
        second = TaskProgressItem(index=2, total=4, subject='src/b.py', activity='security')

        output.task_progress(
            TaskProgress(
                stage_label='Review pass',
                event='started',
                task=second,
                active_tasks=(first, second),
                completed_count=1,
                total_count=4,
                parallel_limit=2,
            )
        )

        text = stdout.getvalue()

        self.assertIn('Review pass', text)
        self.assertIn('1 done, 2 active of 2, 1 pending', text)
        self.assertIn('src/a.py (correctness), src/b.py (security)', text)

    def test_task_progress_redraws_in_place_for_tty(self) -> None:
        stdout = _FakeTty()
        output = ConsoleReviewOutput(stdout=stdout)
        first = TaskProgressItem(index=1, total=3, subject='src/a.py', activity='correctness')
        second = TaskProgressItem(index=2, total=3, subject='src/b.py', activity='security')

        output.task_progress(
            TaskProgress(
                stage_label='Review pass',
                event='started',
                task=first,
                active_tasks=(first,),
                completed_count=0,
                total_count=3,
                parallel_limit=2,
            )
        )
        self.assertTrue(output._live is not None or output._live_lines is not None)
        output.task_progress(
            TaskProgress(
                stage_label='Review pass',
                event='started',
                task=second,
                active_tasks=(first, second),
                completed_count=0,
                total_count=3,
                parallel_limit=2,
            )
        )
        self.assertTrue(output._live is not None or output._live_lines is not None)
        output.step_started(6, 8, 'Run cluster review passes')
        self.assertIsNone(output._live)
        self.assertIsNone(output._live_lines)

        text = stdout.getvalue()

        self.assertIn('Review pass', text)
        self.assertIn('In progress', text)
        self.assertIn('src/a.py', text)
        self.assertIn('src/b.py', text)
        self.assertIn('Run cluster review passes', text)

    def test_parser_accepts_preview_mode_flag(self) -> None:
        args = CliApplication.parse_args(
            [
                'https://gitlab.example.com/group/project/-/merge_requests/42',
                '--preview-mode',
            ]
        )

        self.assertTrue(args.preview_mode)

    def test_parser_accepts_bare_continue_flag(self) -> None:
        args = CliApplication.parse_args(['--continue'])

        self.assertEqual(args.continue_workspace, '')
        self.assertIsNone(args.mr_url)

    def test_parser_accepts_named_continue_workspace(self) -> None:
        args = CliApplication.parse_args(['--continue', 'project-mr-42-2026-05-08_16-40-12'])

        self.assertEqual(args.continue_workspace, 'project-mr-42-2026-05-08_16-40-12')

    def test_parser_rejects_missing_mr_url_without_continue(self) -> None:
        with self.assertRaises(SystemExit):
            CliApplication.parse_args([])

    def test_parser_rejects_mr_url_with_continue(self) -> None:
        with self.assertRaises(SystemExit):
            CliApplication.parse_args(
                [
                    'https://gitlab.example.com/group/project/-/merge_requests/42',
                    '--continue',
                ]
            )

    def test_preview_mode_requires_tty_streams(self) -> None:
        with self.assertRaisesRegex(RuntimeError, '--preview-mode requires interactive stdin and stdout TTYs.'):
            CliApplication.validate_preview_mode(
                True,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
            )


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


if __name__ == '__main__':
    unittest.main()

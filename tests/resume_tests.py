from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from application.review_progress import ReviewProgressStore
from domain.workspace import WorkspacePaths
from infrastructure.workspace.resume import WorkspaceResumeResolver
from infrastructure.workspace.setup import WorkspaceBuilder


class ReviewProgressStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_store_records_completed_and_failed_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            store = ReviewProgressStore(paths)

            await store.initialize(
                mr_url='https://gitlab.example.com/group/project/-/merge_requests/42',
                project_path='group/project',
                mr_iid='42',
                model='test-model',
                preview_mode=False,
            )
            await store.mark_stage_started('collect_state', 'Collect state')
            await store.mark_stage_completed('collect_state', 'Collect state', {'files_changed': 2})
            await store.mark_stage_failed('summary', 'Summary', RuntimeError('agent failed'))

            progress = json.loads(paths.progress.read_text())

            self.assertTrue(await store.is_stage_complete('collect_state'))
            self.assertEqual(await store.stage_data('collect_state'), {'files_changed': 2})
            self.assertEqual(progress['status'], 'failed')
            self.assertEqual(progress['current_stage'], 'summary')
            self.assertEqual(progress['stage_status']['summary']['error']['type'], 'RuntimeError')
            self.assertFalse(paths.progress.with_suffix('.json.tmp').exists())

    async def test_mark_run_completed_sets_done_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            store = ReviewProgressStore(paths)

            await store.initialize(
                mr_url='https://gitlab.example.com/group/project/-/merge_requests/42',
                project_path='group/project',
                mr_iid='42',
                model='test-model',
                preview_mode=True,
            )
            await store.mark_run_completed(result={'posted': 1}, warnings=['careful'])

            progress = json.loads(paths.progress.read_text())

            self.assertTrue(ReviewProgressStore.is_complete_payload(progress))
            self.assertEqual(progress['result'], {'posted': 1})
            self.assertEqual(progress['warnings'], ['careful'])


class WorkspaceResumeResolverTests(unittest.TestCase):
    def test_workspace_name_uses_human_readable_timestamp(self) -> None:
        name = WorkspaceBuilder.workspace_name('project', '42')

        self.assertRegex(name, r'^project-mr-42-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$')

    def test_resolves_named_workspace_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / '.pr-review-workspaces'
            workspace = workspaces / 'project-mr-42-2026-05-08_16-40-12'
            workspace.mkdir(parents=True)
            (workspace / 'progress.json').write_text(
                json.dumps(
                    {
                        'schema_version': 1,
                        'mr_url': 'https://gitlab.example.com/group/project/-/merge_requests/42',
                        'status': 'running',
                        'completed_stages': {},
                    }
                )
            )

            resolved = WorkspaceResumeResolver.resolve(Path(tmp), workspace.name)

            self.assertEqual(resolved.paths.root, workspace)
            self.assertFalse(resolved.done)
            self.assertEqual(resolved.progress['status'], 'running')

    def test_resolves_latest_resumable_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / '.pr-review-workspaces'
            older = self._workspace_with_progress(workspaces, 'older')
            newer = self._workspace_with_progress(workspaces, 'newer')
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            resolved = WorkspaceResumeResolver.resolve(Path(tmp), None)

            self.assertEqual(resolved.name, 'newer')

    def test_done_legacy_workspace_can_be_reported_but_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / '.pr-review-workspaces'
            workspace = workspaces / 'legacy-done'
            workspace.mkdir(parents=True)
            (workspace / 'publish-result.json').write_text('{}')
            (workspace / 'quality-report.json').write_text('{}')

            resolved = WorkspaceResumeResolver.resolve(Path(tmp), workspace.name)

            self.assertTrue(resolved.done)
            self.assertEqual(resolved.progress, {})

    def test_legacy_workspace_without_progress_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / '.pr-review-workspaces'
            workspace = workspaces / 'legacy-running'
            workspace.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, re.escape('legacy workspace without progress.json')):
                WorkspaceResumeResolver.resolve(Path(tmp), workspace.name)

    @staticmethod
    def _workspace_with_progress(workspaces: Path, name: str) -> Path:
        workspace = workspaces / name
        workspace.mkdir(parents=True)
        (workspace / 'progress.json').write_text(
            json.dumps(
                {
                    'schema_version': 1,
                    'mr_url': 'https://gitlab.example.com/group/project/-/merge_requests/42',
                    'status': 'running',
                    'completed_stages': {},
                }
            )
        )
        return workspace


if __name__ == '__main__':
    unittest.main()

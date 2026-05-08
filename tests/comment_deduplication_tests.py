from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from application.comment_deduplication import ReviewCommentDeduplicator
from domain.review.comment_dedup import CommentDedupPlanner
from domain.workspace import WorkspacePaths


class CommentDedupPlannerTests(unittest.TestCase):
    def test_build_groups_uses_configured_line_window(self) -> None:
        findings = [
            self._finding('src/app.py', 10, 'First'),
            self._finding('src/app.py', 14, 'Second'),
        ]

        groups = CommentDedupPlanner.build_groups(findings, [], line_window=4)

        self.assertEqual(len(groups), 1)
        self.assertEqual([comment['index'] for comment in groups[0]['new_comments']], [1, 2])
        self.assertEqual(groups[0]['line_window'], 4)

    def test_build_groups_clusters_nearby_findings_and_existing_comments(self) -> None:
        findings = [
            self._finding('src/app.py', 10, 'First'),
            self._finding('src/app.py', 12, 'Second'),
            self._finding('src/app.py', 30, 'Third'),
            self._finding('src/other.py', 5, 'Fourth'),
        ]
        groups = CommentDedupPlanner.build_groups(
            findings,
            [
                {'note_id': 100, 'author': 'alice', 'file_path': 'src/app.py', 'line': 11, 'body': 'Near first'},
                {'note_id': 101, 'author': 'bob', 'file_path': 'src/app.py', 'line': 14, 'body': 'Near second'},
                {'note_id': 102, 'author': 'carol', 'file_path': 'src/app.py', 'line': 40, 'body': 'Far away'},
            ],
        )

        self.assertEqual(len(groups), 3)
        first = groups[0]
        self.assertEqual([comment['index'] for comment in first['new_comments']], [1, 2])
        self.assertEqual([comment['note_id'] for comment in first['existing_comments']], [100, 101])
        self.assertEqual(first['start_line'], 10)
        self.assertEqual(first['end_line'], 12)

    @staticmethod
    def _finding(path: str, line: int, title: str) -> dict:
        return {
            'severity': 'Major',
            'confidence': 'High',
            'short_title': title,
            'rule_ids': ['CS-CORE-001'],
            'anchor': {
                'old_path': path,
                'new_path': path,
                'new_line': line,
                'old_line': None,
            },
            'body': title,
            'language': 'python',
            'focus_area': 'correctness',
            'evidence': f'Changed line {line}',
            'impact': title,
            'dedup_key': f'CS-CORE-001|{path}|{line}|correctness|{title.lower()}',
            'source_pass': 'correctness',
            'source_kind': 'file',
        }


class ReviewCommentDeduplicatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_deduplicate_keeps_agent_selected_indexes_and_logs_dropped_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding('src/app.py', 10, 'First'),
                        self._finding('src/app.py', 12, 'Second'),
                        self._finding('src/app.py', 40, 'Third'),
                    ]
                )
            )
            paths.existing_comments.write_text(
                json.dumps(
                    [
                        {'note_id': 500, 'author': 'alice', 'file_path': 'src/app.py', 'line': 11, 'body': 'Existing'},
                    ]
                )
            )

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=_FakeDedupAgent(),
                model='test-model',
                max_findings_total=5,
                line_window=4,
            )

            kept = json.loads(paths.all_findings.read_text())
            report = json.loads(paths.dedup_report.read_text())
            first_input = json.loads((paths.dedup_inputs_dir / '001.json').read_text())

            self.assertEqual(result['kept'], 2)
            self.assertEqual(result['deduplicated'], 1)
            self.assertEqual(result['line_window'], 4)
            self.assertEqual(result['parallel_reviews'], 1)
            self.assertEqual([item['short_title'] for item in kept], ['First', 'Third'])
            self.assertEqual(report['groups'], 2)
            self.assertEqual(report['line_window'], 4)
            self.assertEqual(report['parallel_reviews'], 1)
            self.assertEqual(report['deduplicated_comments'][0]['index'], 2)
            self.assertEqual(report['deduplicated_comments'][0]['duplicate_of'], 'new:1')
            self.assertEqual(first_input['group']['existing_comments'][0]['note_id'], 500)

    async def test_deduplicate_runs_groups_in_parallel_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding('src/app.py', 10, 'First'),
                        self._finding('src/app.py', 20, 'Second'),
                        self._finding('src/app.py', 30, 'Third'),
                        self._finding('src/app.py', 40, 'Fourth'),
                    ]
                )
            )
            agent = _TrackingDedupAgent()
            output = _RecordingOutput()

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=agent,
                model='test-model',
                max_findings_total=10,
                line_window=1,
                parallel_reviews=2,
                output=output,
            )

            kept = json.loads(paths.all_findings.read_text())
            report = json.loads(paths.dedup_report.read_text())

            self.assertEqual(result['kept'], 4)
            self.assertEqual(result['parallel_reviews'], 2)
            self.assertEqual(agent.max_in_flight, 2)
            self.assertEqual(len(kept), 4)
            self.assertEqual(report['groups'], 4)
            self.assertEqual(report['parallel_reviews'], 2)
            self.assertTrue(output.progress_updates)
            self.assertEqual(output.progress_updates[0].stage_label, 'Dedup group')
            self.assertEqual(output.progress_updates[0].event, 'started')
            self.assertTrue(any(len(update.active_tasks) == 2 for update in output.progress_updates))
            self.assertEqual(output.progress_updates[-1].event, 'completed')
            self.assertEqual(output.progress_updates[-1].completed_count, 4)
            self.assertEqual(output.progress_updates[-1].active_tasks, ())

    async def test_deduplicate_reuses_existing_group_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding('src/app.py', 10, 'First'),
                        self._finding('src/app.py', 12, 'Second'),
                    ]
                )
            )
            paths.dedup_results_dir.mkdir(parents=True)
            (paths.dedup_results_dir / '001.json').write_text(
                json.dumps(
                    {
                        'group_id': 'group_001',
                        'unique_comment_indexes': [1],
                        'duplicates': [
                            {
                                'index': 2,
                                'duplicate_of': 'new:1',
                                'reason': 'cached duplicate',
                            }
                        ],
                    }
                )
            )

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=_UnexpectedDedupAgent(),
                model='test-model',
                max_findings_total=5,
                line_window=4,
            )

            kept = json.loads(paths.all_findings.read_text())

            self.assertEqual(result['kept'], 1)
            self.assertEqual(result['deduplicated'], 1)
            self.assertEqual([item['short_title'] for item in kept], ['First'])

    @staticmethod
    def _finding(path: str, line: int, title: str) -> dict:
        return {
            'severity': 'Major',
            'confidence': 'High',
            'short_title': title,
            'rule_ids': ['CS-CORE-001'],
            'anchor': {
                'old_path': path,
                'new_path': path,
                'new_line': line,
                'old_line': None,
            },
            'body': title,
            'language': 'python',
            'focus_area': 'correctness',
            'evidence': f'Changed line {line}',
            'impact': title,
            'dedup_key': f'CS-CORE-001|{path}|{line}|correctness|{title.lower()}',
            'source_pass': 'correctness',
            'source_kind': 'file',
        }


class _FakeDedupAgent:
    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
        indexes = [comment['index'] for comment in group['new_comments']]
        if indexes == [1, 2]:
            result = {
                'group_id': group['group_id'],
                'unique_comment_indexes': [1],
                'duplicates': [
                    {
                        'index': 2,
                        'duplicate_of': 'new:1',
                        'reason': 'Same nearby defect.',
                    }
                ],
                'anomalies': [],
            }
        else:
            result = {
                'group_id': group['group_id'],
                'unique_comment_indexes': indexes,
                'duplicates': [],
                'anomalies': [],
            }
        Path(payload['result_path']).write_text(json.dumps(result, indent=2))
        return '{"status":"written"}'


class _TrackingDedupAgent:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
        indexes = [comment['index'] for comment in group['new_comments']]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            Path(payload['result_path']).write_text(
                json.dumps(
                    {
                        'group_id': group['group_id'],
                        'unique_comment_indexes': indexes,
                        'duplicates': [],
                        'anomalies': [],
                    },
                    indent=2,
                )
            )
        finally:
            self.in_flight -= 1
        return '{"status":"written"}'


class _UnexpectedDedupAgent:
    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        raise AssertionError('dedup agent should not be called when cached results are valid')


class _RecordingOutput:
    def __init__(self) -> None:
        self.progress_updates = []

    def task_progress(self, progress) -> None:
        self.progress_updates.append(progress)


if __name__ == '__main__':
    unittest.main()

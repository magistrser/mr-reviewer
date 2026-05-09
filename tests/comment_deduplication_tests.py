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

    def test_build_groups_ignores_rule_ids_and_dedup_keys_within_focus_area(self) -> None:
        findings = [
            self._finding(
                'src/auth.py',
                10,
                'Token accepted without guard',
                focus_area='security',
                rule_ids=['CS-SEC-001'],
                dedup_key='CS-SEC-001|src/auth.py|10|security|token-guard',
            ),
            self._finding(
                'src/auth.py',
                12,
                'Authorization bypass remains possible',
                focus_area='security',
                rule_ids=['CS-CORE-007'],
                dedup_key='CS-CORE-007|src/auth.py|12|security|auth-bypass-v2',
            ),
        ]

        groups = CommentDedupPlanner.build_groups(findings, [], line_window=3)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['focus_area'], 'security')
        self.assertEqual(groups[0]['focus_areas'], ['security'])
        self.assertEqual(groups[0]['source_passes'], ['security'])
        self.assertEqual([comment['index'] for comment in groups[0]['new_comments']], [1, 2])
        self.assertNotEqual(
            groups[0]['new_comments'][0]['rule_ids'],
            groups[0]['new_comments'][1]['rule_ids'],
        )
        self.assertNotEqual(
            groups[0]['new_comments'][0]['dedup_key'],
            groups[0]['new_comments'][1]['dedup_key'],
        )

    def test_build_groups_merges_nearby_findings_across_focus_areas(self) -> None:
        findings = [
            self._finding('src/app.py', 10, 'First', focus_area='correctness'),
            self._finding('src/app.py', 11, 'Second', focus_area='security'),
        ]

        groups = CommentDedupPlanner.build_groups(findings, [], line_window=3)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['focus_area'], '')
        self.assertEqual(groups[0]['focus_areas'], ['correctness', 'security'])
        self.assertEqual(groups[0]['source_passes'], ['correctness', 'security'])
        self.assertEqual([comment['index'] for comment in groups[0]['new_comments']], [1, 2])

    @staticmethod
    def _finding(
        path: str,
        line: int,
        title: str,
        *,
        focus_area: str = 'correctness',
        rule_ids: list[str] | None = None,
        dedup_key: str | None = None,
    ) -> dict:
        rule_ids = rule_ids or ['CS-CORE-001']
        return {
            'severity': 'Major',
            'confidence': 'High',
            'short_title': title,
            'rule_ids': rule_ids,
            'anchor': {
                'old_path': path,
                'new_path': path,
                'new_line': line,
                'old_line': None,
            },
            'body': title,
            'language': 'python',
            'focus_area': focus_area,
            'evidence': f'Changed line {line}',
            'impact': title,
            'dedup_key': dedup_key or f'{rule_ids[0]}|{path}|{line}|{focus_area}|{title.lower()}',
            'source_pass': focus_area,
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

    async def test_deduplicate_sends_same_focus_area_different_metadata_to_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding(
                            'src/auth.py',
                            10,
                            'Token accepted without guard',
                            focus_area='security',
                            rule_ids=['CS-SEC-001'],
                            dedup_key='CS-SEC-001|src/auth.py|10|security|token-guard',
                        ),
                        self._finding(
                            'src/auth.py',
                            12,
                            'Authorization bypass remains possible',
                            focus_area='security',
                            rule_ids=['CS-CORE-007'],
                            dedup_key='CS-CORE-007|src/auth.py|12|security|auth-bypass-v2',
                        ),
                    ]
                )
            )
            agent = _MetadataAgnosticDedupAgent()

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=agent,
                model='test-model',
                max_findings_total=5,
                line_window=3,
            )

            first_input = json.loads((paths.dedup_inputs_dir / '001.json').read_text())
            report = json.loads(paths.dedup_report.read_text())

            self.assertTrue(agent.checked_group)
            self.assertEqual(result['groups'], 1)
            self.assertEqual(result['deduplicated'], 1)
            self.assertEqual(first_input['group']['focus_area'], 'security')
            self.assertEqual(first_input['group']['focus_areas'], ['security'])
            self.assertEqual(first_input['group']['source_passes'], ['security'])
            self.assertEqual(report['group_reports'][0]['focus_area'], 'security')
            self.assertEqual(report['group_reports'][0]['focus_areas'], ['security'])
            self.assertEqual(report['group_reports'][0]['source_passes'], ['security'])

    async def test_deduplicate_merges_same_line_duplicates_across_review_lenses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding('utils/mod.rs', 21, 'Panic remains possible', focus_area='correctness'),
                        self._finding(
                            'utils/mod.rs',
                            21,
                            'Rust panic path can abort request handling',
                            focus_area='rust',
                            rule_ids=['CS-RUST-001'],
                        ),
                        self._finding(
                            'utils/mod.rs',
                            21,
                            'Async task panics instead of returning an error',
                            focus_area='async_concurrency',
                            rule_ids=['CS-ASYNC-001'],
                        ),
                    ]
                )
            )
            agent = _CrossPassDedupAgent()

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=agent,
                model='test-model',
                max_findings_total=5,
                line_window=3,
            )

            kept = json.loads(paths.all_findings.read_text())
            first_input = json.loads((paths.dedup_inputs_dir / '001.json').read_text())
            report = json.loads(paths.dedup_report.read_text())

            self.assertTrue(agent.checked_group)
            self.assertEqual(result['groups'], 1)
            self.assertEqual(result['deduplicated'], 2)
            self.assertEqual([item['short_title'] for item in kept], ['Panic remains possible'])
            self.assertEqual(
                first_input['group']['focus_areas'],
                ['async_concurrency', 'correctness', 'rust'],
            )
            self.assertEqual(
                report['group_reports'][0]['source_passes'],
                ['async_concurrency', 'correctness', 'rust'],
            )

    async def test_deduplicate_repairs_fenced_result_json(self) -> None:
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

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=_MalformedDedupAgent('fenced'),
                model='test-model',
                max_findings_total=5,
                line_window=3,
            )

            report = json.loads(paths.dedup_report.read_text())

            self.assertEqual(result['deduplicated'], 1)
            self.assertTrue(result['anomalies'])
            self.assertIn('removed markdown code fence', report['group_reports'][0]['result_repair_notes'])

    async def test_deduplicate_repairs_raw_newline_in_result_json_string(self) -> None:
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

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=_MalformedDedupAgent('raw_newline'),
                model='test-model',
                max_findings_total=5,
                line_window=3,
            )

            report = json.loads(paths.dedup_report.read_text())

            self.assertEqual(result['deduplicated'], 1)
            self.assertTrue(result['anomalies'])
            self.assertIn(
                'escaped raw control characters inside JSON strings',
                report['group_reports'][0]['result_repair_notes'],
            )

    async def test_deduplicate_retries_invalid_group_result_once(self) -> None:
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
            agent = _RetryingInvalidDedupAgent()

            result = await ReviewCommentDeduplicator.deduplicate(
                paths=paths,
                agent=agent,
                model='test-model',
                max_findings_total=5,
                line_window=3,
            )

            self.assertEqual(agent.calls, 2)
            self.assertEqual(result['deduplicated'], 1)
            self.assertTrue(any('accepted after retry' in item for item in result['anomalies']))

    @staticmethod
    def _finding(
        path: str,
        line: int,
        title: str,
        *,
        focus_area: str = 'correctness',
        rule_ids: list[str] | None = None,
        dedup_key: str | None = None,
    ) -> dict:
        rule_ids = rule_ids or ['CS-CORE-001']
        return {
            'severity': 'Major',
            'confidence': 'High',
            'short_title': title,
            'rule_ids': rule_ids,
            'anchor': {
                'old_path': path,
                'new_path': path,
                'new_line': line,
                'old_line': None,
            },
            'body': title,
            'language': 'python',
            'focus_area': focus_area,
            'evidence': f'Changed line {line}',
            'impact': title,
            'dedup_key': dedup_key or f'{rule_ids[0]}|{path}|{line}|{focus_area}|{title.lower()}',
            'source_pass': focus_area,
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


class _MetadataAgnosticDedupAgent:
    def __init__(self) -> None:
        self.checked_group = False

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
        comments = group['new_comments']
        assert group['focus_area'] == 'security'
        assert [comment['index'] for comment in comments] == [1, 2]
        assert comments[0]['rule_ids'] != comments[1]['rule_ids']
        assert comments[0]['dedup_key'] != comments[1]['dedup_key']
        self.checked_group = True
        Path(payload['result_path']).write_text(
            json.dumps(
                {
                    'group_id': group['group_id'],
                    'unique_comment_indexes': [1],
                    'duplicates': [
                        {
                            'index': 2,
                            'duplicate_of': 'new:1',
                            'reason': 'Same nearby defect despite different metadata.',
                        }
                    ],
                    'anomalies': [],
                },
                indent=2,
            )
        )
        return '{"status":"written"}'


class _CrossPassDedupAgent:
    def __init__(self) -> None:
        self.checked_group = False

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
        comments = group['new_comments']
        assert group['focus_area'] == ''
        assert group['focus_areas'] == ['async_concurrency', 'correctness', 'rust']
        assert group['source_passes'] == ['async_concurrency', 'correctness', 'rust']
        assert [comment['index'] for comment in comments] == [1, 2, 3]
        self.checked_group = True
        Path(payload['result_path']).write_text(
            json.dumps(
                {
                    'group_id': group['group_id'],
                    'unique_comment_indexes': [1],
                    'duplicates': [
                        {
                            'index': 2,
                            'duplicate_of': 'new:1',
                            'reason': 'Same utils/mod.rs:21 panic surfaced through rust lens.',
                        },
                        {
                            'index': 3,
                            'duplicate_of': 'new:1',
                            'reason': 'Same utils/mod.rs:21 panic surfaced through async lens.',
                        },
                    ],
                    'anomalies': [],
                },
                indent=2,
            )
        )
        return '{"status":"written"}'


class _MalformedDedupAgent:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
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
        result_path = Path(payload['result_path'])
        if self.mode == 'fenced':
            result_path.write_text(f'```json\n{json.dumps(result, indent=2)}\n```')
        elif self.mode == 'raw_newline':
            result_path.write_text(
                '{"group_id":"%s","unique_comment_indexes":[1],'
                '"duplicates":[{"index":2,"duplicate_of":"new:1",'
                '"reason":"Line one\nLine two"}],"anomalies":[]}'
                % group['group_id']
            )
        else:
            raise AssertionError(f'unexpected mode {self.mode}')
        return '{"status":"written"}'


class _RetryingInvalidDedupAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        assert agent_name == 'deduplicate-comments-agent'
        assert model == 'test-model'
        self.calls += 1
        payload = json.loads(Path(prompt).read_text())
        group = payload['group']
        if self.calls == 1:
            Path(payload['result_path']).write_text(
                json.dumps(
                    {
                        'group_id': group['group_id'],
                        'unique_comment_indexes': ['bad-index'],
                        'duplicates': [],
                        'anomalies': [],
                    }
                )
            )
        else:
            Path(payload['result_path']).write_text(
                json.dumps(
                    {
                        'group_id': group['group_id'],
                        'unique_comment_indexes': [1],
                        'duplicates': [
                            {
                                'index': 2,
                                'duplicate_of': 'new:1',
                                'reason': 'Valid after retry.',
                            }
                        ],
                        'anomalies': [],
                    },
                    indent=2,
                )
            )
        return '{"status":"written"}'


class _RecordingOutput:
    def __init__(self) -> None:
        self.progress_updates = []

    def task_progress(self, progress) -> None:
        self.progress_updates.append(progress)


if __name__ == '__main__':
    unittest.main()

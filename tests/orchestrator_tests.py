from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from application.dto import ReviewRunOptions, ReviewTask
from application.review_flow import (
    MergeRequestUrlParser,
    ReviewDependencies,
    ReviewMergeRequestUseCase,
    ReviewWorkspaceService,
)
from application.review_progress import ReviewProgressStore
from settings import AgentConfig, Config, GitLabConfig, ReviewConfig
from domain.review.context import ReviewContextLimits
from domain.review.standards import ReviewStandards
from domain.workspace import WorkspacePaths


class ParseMrUrlTests(unittest.TestCase):
    def test_parses_nested_project_and_iid(self) -> None:
        project_path, mr_iid = MergeRequestUrlParser.parse(
            'https://gitlab.example.com/group/sub/repo/-/merge_requests/42/diffs'
        )
        self.assertEqual(project_path, 'group/sub/repo')
        self.assertEqual(mr_iid, '42')

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            MergeRequestUrlParser.parse('https://gitlab.example.com/group/sub/repo/issues/42')


class ReviewTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resources_dir = Path('/Users/franz/review/resources/review')
        self.resources = await ReviewStandards.load_resources(resources_dir)

    async def test_collect_review_state_writes_existing_comment_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            client = _FakeGitLabStateClient(
                discussions=[
                    {
                        'notes': [
                            {
                                'id': 10,
                                'system': False,
                                'resolved': False,
                                'author': {'username': 'alice'},
                                'body': 'Open note',
                                'position': {
                                    'new_path': 'src/app.py',
                                    'new_line': 10,
                                    'old_line': None,
                                },
                            },
                            {
                                'id': 11,
                                'system': False,
                                'resolved': True,
                                'author': {'username': 'bob'},
                                'body': 'Resolved note',
                                'position': {
                                    'new_path': 'src/app.py',
                                    'new_line': 11,
                                    'old_line': None,
                                },
                            },
                        ]
                    }
                ],
                changes={'changes': []},
            )

            state = await ReviewWorkspaceService.collect_review_state(
                client,
                'group/project',
                '9',
                paths,
            )

            self.assertEqual(state['discussions_count'], 1)
            self.assertEqual(state['comments_count'], 2)
            comments = json.loads(paths.existing_comments.read_text())
            discussions = json.loads(paths.existing_discussions.read_text())
            self.assertEqual([comment['note_id'] for comment in comments], [10, 11])
            self.assertEqual([discussion['note_id'] for discussion in discussions], [10])

    async def test_next_review_task_runs_correctness_then_language_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                '    if not name:\n'
                "        return 'hi'\n"
                "    return f'hello {name}'\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 4, 'new_start': 1, 'new_count': 4}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )

            first = await ReviewWorkspaceService.next_review_task(
                paths,
                self.resources,
                max_findings_per_file=10,
            )
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first.pass_id, 'correctness')
            self.assertEqual(first.focus_area, 'correctness')
            self.assertEqual(first.file_path, 'src/main.py')
            self.assertTrue(first.input_path.exists())
            payload = json.loads(first.input_path.read_text())
            self.assertEqual(payload['review']['pass_id'], 'correctness')
            self.assertEqual(payload['review']['focus_area'], 'correctness')
            self.assertIn('analysis', payload['file'])

            core_findings = paths.findings_dir / '001-correctness.json'
            core_findings.write_text(json.dumps({'findings': []}))

            second = await ReviewWorkspaceService.next_review_task(
                paths,
                self.resources,
                max_findings_per_file=10,
            )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.pass_id, 'python')

    async def test_build_review_tasks_uses_distinct_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                '    if not name:\n'
                "        return 'hi'\n"
                "    return f'hello {name}'\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 4, 'new_start': 1, 'new_count': 4}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )

            tasks = await ReviewWorkspaceService.build_review_tasks(
                paths,
                self.resources,
                max_findings_per_file=10,
            )

            self.assertEqual([task.pass_id for task in tasks], ['correctness', 'python'])
            self.assertEqual(len({task.input_path for task in tasks}), 2)
            self.assertTrue(all(task.input_path.parent == paths.review_inputs_dir for task in tasks))
            self.assertFalse(paths.attempts.exists())

    async def test_failed_attempt_is_stubbed_and_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'pkg').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'pkg' / 'module.py').write_text('print("hello")\n')
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'pkg/module.py',
                            'new_path': 'pkg/module.py',
                            'hunks': [{'old_start': 1, 'old_count': 1, 'new_start': 1, 'new_count': 1}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            paths.attempts.write_text(json.dumps({'001-correctness': 1}))

            task = await ReviewWorkspaceService.next_review_task(
                paths,
                self.resources,
                max_findings_per_file=10,
            )
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.pass_id, 'python')

            stub = json.loads((paths.findings_dir / '001-correctness.json').read_text())
            self.assertEqual(stub['findings'], [])
            self.assertTrue(stub['anomalies'])

    async def test_next_review_task_honors_custom_context_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'import os\n'
                'import json\n'
                '\n'
                'def greet(name):\n'
                "    prefix = 'hello'\n"
                '    if not name:\n'
                "        return prefix + ' world'\n"
                '    return f"{prefix} {name}"\n'
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 8, 'new_start': 1, 'new_count': 8}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )

            limits = ReviewContextLimits(
                max_context_chars=500,
                review_goal_chars=80,
                project_profile_chars=80,
                pr_summary_chars=80,
                file_profile_chars=80,
                excerpt_chars=100,
                imports_chars=60,
                symbols_chars=120,
                review_plan_chars=60,
                standards_chars=120,
                severity_chars=60,
                template_chars=60,
            )
            task = await ReviewWorkspaceService.next_review_task(
                paths,
                self.resources,
                max_findings_per_file=10,
                context_limits=limits,
            )

            self.assertIsNotNone(task)
            assert task is not None
            payload = json.loads(task.input_path.read_text())
            context_text = Path(payload['context_path']).read_text()
            self.assertLessEqual(len(context_text), limits.scaled_value(limits.max_context_chars) + 1)
            self.assertIn('[truncated]', context_text)

    async def test_gate_changed_files_uses_repo_catalog_and_writes_cluster_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src' / 'api').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'api' / 'handler.py').write_text(
                'from src.api.dto import UserDto\n'
                '\n'
                'def build_response(payload: UserDto):\n'
                "    return {'id': payload.id}\n"
            )
            (paths.repo_dir / 'src' / 'api' / 'serializer.py').write_text(
                'from src.api.dto import UserDto\n'
                '\n'
                'class UserSerializer:\n'
                '    def serialize(self, payload: UserDto):\n'
                "        return {'id': payload.id}\n"
            )
            (paths.repo_dir / 'src' / 'api' / 'dto.py').write_text('class UserDto:\n    pass\n')
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/api/handler.py',
                            'new_path': 'src/api/handler.py',
                            'hunks': [{'old_start': 1, 'old_count': 4, 'new_start': 1, 'new_count': 4}],
                        },
                        {
                            'old_path': 'src/api/serializer.py',
                            'new_path': 'src/api/serializer.py',
                            'hunks': [{'old_start': 1, 'old_count': 5, 'new_start': 1, 'new_count': 5}],
                        },
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')

            indexing = await ReviewWorkspaceService.build_repo_catalog(
                paths,
                enabled=True,
                max_catalog_file_bytes=4096,
            )
            eligible = await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )

            self.assertEqual(indexing['indexed_files'], 3)
            self.assertEqual(eligible, 2)
            repo_catalog = json.loads(paths.repo_catalog.read_text())
            service_entry = repo_catalog['files']['src/api/serializer.py']
            self.assertIn('symbol_spans', service_entry)
            analyses = json.loads(paths.file_analysis.read_text())
            handler_analysis = next(item['analysis'] for item in analyses if item['path'] == 'src/api/handler.py')
            self.assertIn('import_targets', handler_analysis)
            self.assertIn('retrieval_hints', handler_analysis)
            clusters = json.loads(paths.cluster_plan.read_text())
            self.assertTrue(clusters)
            self.assertIn('evidence', clusters[0])
            self.assertIn('retrieval_requests', clusters[0])
            self.assertTrue(clusters[0]['retrieval_requests'])

    async def test_build_review_tasks_writes_retrieval_plan_artifacts_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'auth' / 'handler.py').write_text(
                'from src.auth.service import AuthService\n'
                '\n'
                'class AuthHandler:\n'
                '    def handle(self):\n'
                '        return AuthService()\n'
            )
            (paths.repo_dir / 'src' / 'auth' / 'service.py').write_text(
                'class AuthService:\n'
                '    def validate(self):\n'
                '        return True\n'
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/auth/handler.py',
                            'new_path': 'src/auth/handler.py',
                            'hunks': [{'old_start': 1, 'old_count': 5, 'new_start': 1, 'new_count': 5}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')

            await ReviewWorkspaceService.build_repo_catalog(
                paths,
                enabled=True,
                max_catalog_file_bytes=4096,
            )
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            tasks = await ReviewWorkspaceService.build_review_tasks(
                paths,
                self.resources,
                max_findings_per_file=10,
                indexing_enabled=True,
                max_retrieved_chars_per_task=160,
            )

            self.assertTrue(tasks)
            retrieval_plan_paths = sorted(paths.retrieval_plans_dir.glob('*.json'))
            self.assertTrue(retrieval_plan_paths)
            retrieval_plan = json.loads(retrieval_plan_paths[0].read_text())
            self.assertIn('applied_policy', retrieval_plan)
            self.assertIn('allowed_kinds', retrieval_plan['applied_policy'])
            retrieval_report = json.loads(paths.retrieval_report.read_text())
            self.assertIn('totals', retrieval_report)
            self.assertGreaterEqual(retrieval_report['totals']['planned_retrievals'], 1)

    async def test_build_review_tasks_gracefully_handles_unreadable_repo_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                "    return f'hello {name}'\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            paths.repo_catalog.write_text('{not-json')

            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            tasks = await ReviewWorkspaceService.build_review_tasks(
                paths,
                self.resources,
                max_findings_per_file=10,
                indexing_enabled=True,
            )

            self.assertEqual([task.pass_id for task in tasks], ['correctness', 'python'])

    async def test_next_cluster_review_task_builds_cross_file_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src' / 'api').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'api' / 'handler.py').write_text(
                'def build_response(user_id):\n'
                "    return {'token_id': user_id}\n"
            )
            (paths.repo_dir / 'src' / 'api' / 'serializer.py').write_text(
                'def serialize_user(payload):\n'
                "    return {'id': payload['id']}\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/api/handler.py',
                            'new_path': 'src/api/handler.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        },
                        {
                            'old_path': 'src/api/serializer.py',
                            'new_path': 'src/api/serializer.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        },
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )

            task = await ReviewWorkspaceService.next_cluster_review_task(
                paths,
                self.resources,
                max_findings_per_cluster=3,
            )

            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.pass_id, 'api_boundary')
            self.assertEqual(task.source_kind, 'cluster')
            payload = json.loads(task.input_path.read_text())
            self.assertEqual(payload['review']['pass_id'], 'api_boundary')
            self.assertEqual(payload['review']['source_kind'], 'cluster')
            self.assertEqual(len(payload['files']), 2)

    async def test_consolidate_findings_normalizes_and_drops_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            paths.findings_dir.mkdir(parents=True, exist_ok=True)
            (paths.findings_dir / '001-correctness.json').write_text(
                json.dumps(
                    {
                        'file': 'src/app.py',
                        'findings': [
                            self._finding('Major', 10),
                            {
                                'severity': 'Major',
                                'confidence': 'High',
                                'short_title': 'Missing rule IDs',
                                'anchor': {
                                    'old_path': 'src/app.py',
                                    'new_path': 'src/app.py',
                                    'new_line': 20,
                                    'old_line': None,
                                },
                                'body': 'No rule ids here.',
                            },
                        ],
                        'anomalies': [],
                    }
                )
            )

            result = await ReviewWorkspaceService.consolidate_findings(paths)

            self.assertEqual(result['count'], 1)
            self.assertEqual(result['invalid'], 1)
            self.assertTrue(result['anomalies'])
            kept = json.loads(paths.raw_findings.read_text())
            self.assertEqual(kept[0]['source_pass'], 'correctness')
            self.assertEqual(kept[0]['source_kind'], 'file')

    async def test_pass_standards_include_full_core_and_language_rules(self) -> None:
        changed_file = {
            'old_path': 'src/main.py',
            'new_path': 'src/main.py',
            'analysis': {
                'language': 'python',
                'focus_areas': ['correctness'],
            },
        }

        passes = ReviewStandards.passes_for_file(changed_file, self.resources)
        correctness = next(item for item in passes if item[0] == 'correctness')
        language_pass = next(item for item in passes if item[0] == 'python')
        standards_text = correctness[2]
        language_standards = language_pass[2]

        self.assertIn('[CS-CORE-001]', standards_text)
        self.assertIn('[CS-NAMING-001]', standards_text)
        self.assertIn('[CS-API-001]', standards_text)
        self.assertEqual(standards_text.count('[CS-CORE-006]'), 1)
        self.assertIn('## Review Scope', standards_text)
        self.assertIn('[CS-PY-001]', language_standards)
        self.assertNotIn('[CS-API-001]', language_standards)

    async def test_build_review_tasks_can_limit_to_correctness_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                '    if not name:\n'
                "        return 'hi'\n"
                "    return f'hello {name}'\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 4, 'new_start': 1, 'new_count': 4}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            resources = await ReviewStandards.load_resources(
                Path('/Users/franz/review/resources/review'),
                enabled_file_pass_ids=('correctness',),
            )

            tasks = await ReviewWorkspaceService.build_review_tasks(
                paths,
                resources,
                max_findings_per_file=10,
            )

            self.assertEqual([task.pass_id for task in tasks], ['correctness'])

    async def test_build_review_tasks_can_limit_to_language_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                '    if not name:\n'
                "        return 'hi'\n"
                "    return f'hello {name}'\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 4, 'new_start': 1, 'new_count': 4}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            resources = await ReviewStandards.load_resources(
                Path('/Users/franz/review/resources/review'),
                enabled_file_pass_ids=('python',),
            )

            tasks = await ReviewWorkspaceService.build_review_tasks(
                paths,
                resources,
                max_findings_per_file=10,
            )

            self.assertEqual([task.pass_id for task in tasks], ['python'])

    async def test_load_resources_rejects_unknown_configured_file_pass_id(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unknown pass ids'):
            await ReviewStandards.load_resources(
                Path('/Users/franz/review/resources/review'),
                enabled_file_pass_ids=('not-real',),
            )

    async def test_load_resources_rejects_unknown_configured_cluster_pass_id(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unknown pass ids'):
            await ReviewStandards.load_resources(
                Path('/Users/franz/review/resources/review'),
                enabled_cluster_pass_ids=('not-real',),
            )

    async def test_load_resources_rejects_duplicate_file_pass_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resources_dir = Path(tmp)
            (resources_dir / 'templates').mkdir(parents=True, exist_ok=True)
            (resources_dir / 'severity-levels.md').write_text('severity\n')
            (resources_dir / 'templates' / 'inline-comment.md').write_text('template\n')
            (resources_dir / 'review-plan.toml').write_text(
                '[language_standards]\n'
                '\n'
                '[[file_passes]]\n'
                'pass_id = "correctness"\n'
                'focus_area = "correctness"\n'
                'goal = "Goal"\n'
                '\n'
                '[[file_passes]]\n'
                'pass_id = "correctness"\n'
                'focus_area = "security"\n'
                'goal = "Goal"\n'
            )

            with self.assertRaisesRegex(ValueError, 'duplicate file pass_id'):
                await ReviewStandards.load_resources(resources_dir)

    async def test_load_resources_rejects_duplicate_cluster_focus_areas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resources_dir = Path(tmp)
            (resources_dir / 'templates').mkdir(parents=True, exist_ok=True)
            (resources_dir / 'severity-levels.md').write_text('severity\n')
            (resources_dir / 'templates' / 'inline-comment.md').write_text('template\n')
            (resources_dir / 'review-plan.toml').write_text(
                '[language_standards]\n'
                '\n'
                '[[cluster_passes]]\n'
                'pass_id = "security"\n'
                'focus_area = "security"\n'
                'goal = "Goal"\n'
                '\n'
                '[[cluster_passes]]\n'
                'pass_id = "api_boundary"\n'
                'focus_area = "security"\n'
                'goal = "Goal"\n'
            )

            with self.assertRaisesRegex(ValueError, 'duplicate cluster focus_area'):
                await ReviewStandards.load_resources(resources_dir)

    async def test_cluster_review_materials_can_limit_to_enabled_cluster_passes(self) -> None:
        resources = await ReviewStandards.load_resources(
            Path('/Users/franz/review/resources/review'),
            enabled_cluster_pass_ids=('api_boundary',),
        )
        cluster = {
            'focus_area': 'api_boundary',
        }
        cluster_files = [
            {
                'old_path': 'src/api/handler.py',
                'new_path': 'src/api/handler.py',
                'analysis': {
                    'language': 'python',
                },
            },
            {
                'old_path': 'src/api/serializer.py',
                'new_path': 'src/api/serializer.py',
                'analysis': {
                    'language': 'python',
                },
            },
        ]

        materials = ReviewStandards.cluster_review_materials(cluster, cluster_files, resources)

        self.assertIsNotNone(materials)
        assert materials is not None
        self.assertEqual(materials[0], 'api_boundary')
        self.assertIsNone(
            ReviewStandards.cluster_review_materials(
                {'focus_area': 'security'},
                cluster_files,
                resources,
            )
        )

    async def test_cluster_security_pass_preserves_extra_guidance(self) -> None:
        resources = await ReviewStandards.load_resources(
            Path('/Users/franz/review/resources/review'),
            enabled_cluster_pass_ids=('security',),
        )
        cluster = {
            'focus_area': 'security',
        }
        cluster_files = [
            {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                },
            },
            {
                'old_path': 'src/auth/policy.py',
                'new_path': 'src/auth/policy.py',
                'analysis': {
                    'language': 'python',
                },
            },
        ]

        materials = ReviewStandards.cluster_review_materials(cluster, cluster_files, resources)

        self.assertIsNotNone(materials)
        assert materials is not None
        self.assertEqual(materials[0], 'security')
        self.assertIn('Security boundary consistency', materials[1])

    async def test_next_cluster_review_task_skips_disabled_cluster_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._workspace(tmp)
            (paths.repo_dir / 'src' / 'api').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'api' / 'handler.py').write_text(
                'def build_response(user_id):\n'
                "    return {'token_id': user_id}\n"
            )
            (paths.repo_dir / 'src' / 'api' / 'serializer.py').write_text(
                'def serialize_user(payload):\n'
                "    return {'id': payload['id']}\n"
            )
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/api/handler.py',
                            'new_path': 'src/api/handler.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        },
                        {
                            'old_path': 'src/api/serializer.py',
                            'new_path': 'src/api/serializer.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        },
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=3,
            )
            resources = await ReviewStandards.load_resources(
                Path('/Users/franz/review/resources/review'),
                enabled_cluster_pass_ids=(),
            )

            task = await ReviewWorkspaceService.next_cluster_review_task(
                paths,
                resources,
                max_findings_per_cluster=3,
            )

            self.assertIsNone(task)

    async def test_compact_standards_only_normalizes_whitespace(self) -> None:
        compacted = ReviewStandards.compact_standards('\n\nalpha\n\n\nbeta\n')

        self.assertEqual(compacted, 'alpha\n\nbeta')

    @staticmethod
    def _workspace(tmp: str) -> WorkspacePaths:
        paths = WorkspacePaths(root=Path(tmp))
        paths.repo_dir.mkdir(parents=True, exist_ok=True)
        paths.meta.write_text(
            json.dumps(
                {
                    'project_path': 'group/project',
                    'mr_iid': '9',
                    'head_sha': 'head',
                    'base_sha': 'base',
                    'start_sha': 'start',
                }
            )
        )
        return paths

    @staticmethod
    def _finding(severity: str, line: int) -> dict:
        return {
            'severity': severity,
            'confidence': 'High',
            'short_title': severity,
            'rule_ids': ['CS-CORE-001'],
            'anchor': {
                'old_path': 'src/app.py',
                'new_path': 'src/app.py',
                'new_line': line,
                'old_line': None,
            },
            'body': severity,
            'language': 'python',
            'focus_area': 'correctness',
            'evidence': f'Changed line {line}',
            'impact': severity,
            'dedup_key': f'CS-CORE-001|src/app.py|{line}|correctness|{severity.lower()}',
            'source_pass': 'correctness',
            'source_kind': 'file',
        }


class FinalizeFindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_findings_sorts_and_caps_consolidated_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            paths.raw_findings.write_text(
                json.dumps(
                    [
                        self._finding('Suggestion', 30),
                        self._finding('Critical', 10),
                        self._finding('Major', 20),
                    ]
                )
            )

            result = await ReviewWorkspaceService.finalize_findings(paths, max_findings_total=2)

            self.assertEqual(result['kept'], 2)
            self.assertEqual(result['total'], 3)
            kept = json.loads(paths.all_findings.read_text())
            self.assertEqual([item['severity'] for item in kept], ['Critical', 'Major'])

    @staticmethod
    def _finding(severity: str, line: int) -> dict:
        return {
            'severity': severity,
            'confidence': 'High',
            'short_title': severity,
            'rule_ids': ['CS-CORE-001'],
            'anchor': {
                'old_path': 'src/app.py',
                'new_path': 'src/app.py',
                'new_line': line,
                'old_line': None,
            },
            'body': severity,
            'language': 'python',
            'focus_area': 'correctness',
            'evidence': f'Changed line {line}',
            'impact': severity,
            'dedup_key': f'CS-CORE-001|src/app.py|{line}|correctness|{severity.lower()}',
            'source_pass': 'correctness',
            'source_kind': 'file',
        }


class ParallelReviewExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_agent_tasks_respects_parallel_review_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks: list[ReviewTask] = []
            for index in range(3):
                input_path = root / f'input-{index}.json'
                input_path.write_text('{}')
                tasks.append(
                    ReviewTask(
                        ordinal=index + 1,
                        pass_id='correctness',
                        file_path=f'src/file_{index}.py',
                        input_path=input_path,
                        completed_steps=index,
                        total_steps=3,
                        findings_path=root / f'finding-{index}.json',
                        subject_key=f'src/file_{index}.py',
                    )
                )

            agent = _TrackingAgent()
            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=root,
                    agents_dir=root,
                    parallel_reviews=2,
                    parallel_cluster_reviews=1,
                ),
            )
            output = _RecordingReviewOutput()
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=object(),
                    agent=agent,
                    output=output,
                ),
            )

            count = await use_case._run_agent_tasks(
                stage_label='Review pass',
                agent_name='review-agent',
                tasks=tasks,
                model='test-model',
                attempts_path=root / '.attempts.json',
                concurrency=config.review.parallel_reviews,
            )

            self.assertEqual(count, 3)
            self.assertEqual(agent.max_in_flight, 2)
            self.assertTrue(all(task.findings_path is not None and task.findings_path.exists() for task in tasks))
            self.assertTrue(output.progress_updates)
            self.assertEqual(output.progress_updates[0].event, 'started')
            self.assertEqual(len(output.progress_updates[0].active_tasks), 1)
            self.assertTrue(any(len(update.active_tasks) == 2 for update in output.progress_updates))
            self.assertEqual(output.progress_updates[-1].event, 'completed')
            self.assertEqual(output.progress_updates[-1].completed_count, 3)
            self.assertEqual(output.progress_updates[-1].active_tasks, ())

    async def test_run_agent_tasks_can_use_independent_cluster_parallel_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks: list[ReviewTask] = []
            for index in range(4):
                input_path = root / f'cluster-input-{index}.json'
                input_path.write_text('{}')
                tasks.append(
                    ReviewTask(
                        ordinal=index + 1,
                        pass_id='cluster_api_boundary',
                        file_path=f'Cluster {index}',
                        input_path=input_path,
                        completed_steps=index,
                        total_steps=4,
                        findings_path=root / f'cluster-finding-{index}.json',
                        subject_key=f'cluster-{index}',
                        source_kind='cluster',
                    )
                )

            agent = _TrackingAgent()
            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=root,
                    agents_dir=root,
                    parallel_reviews=4,
                    parallel_cluster_reviews=2,
                ),
            )
            output = _RecordingReviewOutput()
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=object(),
                    agent=agent,
                    output=output,
                ),
            )

            count = await use_case._run_agent_tasks(
                stage_label='Cluster pass',
                agent_name='cluster-review-agent',
                tasks=tasks,
                model='test-model',
                attempts_path=root / '.cluster-attempts.json',
                concurrency=config.review.parallel_cluster_reviews,
            )

            self.assertEqual(count, 4)
            self.assertEqual(agent.max_in_flight, 2)
            self.assertTrue(all(task.findings_path is not None and task.findings_path.exists() for task in tasks))
            self.assertTrue(output.progress_updates)
            self.assertEqual(output.progress_updates[-1].parallel_limit, 2)


class UseCaseDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_writes_quality_report_with_indexing_and_retrieval_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / 'workspace'

            async def workspace_provisioner(**kwargs) -> WorkspacePaths:
                paths = WorkspacePaths(root=workspace_root)
                (paths.repo_dir / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
                (paths.repo_dir / 'src' / 'auth' / 'handler.py').write_text(
                    'from src.auth.service import AuthService\n'
                    '\n'
                    'class AuthHandler:\n'
                    '    def handle(self):\n'
                    '        return AuthService()\n'
                )
                (paths.repo_dir / 'src' / 'auth' / 'service.py').write_text(
                    'class AuthService:\n'
                    '    def validate(self):\n'
                    '        return True\n'
                )
                paths.meta.parent.mkdir(parents=True, exist_ok=True)
                paths.meta.write_text(
                    json.dumps(
                        {
                            'project_path': 'group/project',
                            'mr_iid': '7',
                            'head_sha': 'head',
                            'base_sha': 'base',
                            'start_sha': 'start',
                        }
                    )
                )
                return paths

            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                    max_retrieved_chars_per_task=160,
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FakeGitLabExecuteClient(),
                    agent=_TrackingAgent(),
                    output=_RecordingReviewOutput(),
                    workspace_provisioner=workspace_provisioner,
                ),
            )

            async def fake_translate_publication(
                paths,
                agent,
                model,
                publication,
                target_language,
                parallel_reviews,
                output,
            ) -> tuple[dict[str, object], dict[str, object]]:
                await asyncio.sleep(0)
                paths.translated_publication.write_text(json.dumps(publication))
                paths.translation_report.write_text(json.dumps({'status': 'skipped'}))
                return publication, {
                    'status': 'skipped',
                    'language': 'ENG',
                    'jobs': 0,
                    'parallel_reviews': 0,
                    'artifact_path': str(paths.translated_publication),
                    'report_path': str(paths.translation_report),
                }

            async def fake_publish_review(**kwargs) -> dict[str, object]:
                result_path = kwargs['result_path']
                result_path.write_text(json.dumps({'ok': True}))
                return {
                    'posted': 0,
                    'anchor_errors': 0,
                    'out_of_hunk_count': 0,
                    'verdict': 'comment',
                    'overall_comment_status': 'ok',
                    'overall_note_id': '1',
                    'overall_comment_error': None,
                    'result_saved_to': str(result_path),
                }

            with patch(
                'application.review_flow.ReviewTranslationService.translate_publication',
                side_effect=fake_translate_publication,
            ):
                with patch(
                    'application.review_flow.ReviewPublisher.publish_prepared_review',
                    side_effect=fake_publish_review,
                ):
                    result = await use_case.execute(
                        'https://gitlab.example.com/group/project/-/merge_requests/7',
                        model='test-model',
                    )

            quality_report = json.loads(Path(result.quality_report_path).read_text())
            progress = json.loads((workspace_root / 'progress.json').read_text())
            self.assertIn('indexing', quality_report)
            self.assertIn('retrieval', quality_report)
            self.assertIn('translation', quality_report)
            self.assertIn('preview', quality_report)
            self.assertEqual(progress['status'], 'completed')
            self.assertIn('publish', progress['completed_stages'])
            self.assertTrue((workspace_root / 'repo-catalog.json').exists())
            self.assertTrue((workspace_root / 'retrieval-report.json').exists())
            self.assertTrue(list((workspace_root / 'retrieval-plans').glob('*.json')))
            self.assertEqual(result.translation_status, 'skipped')
            self.assertEqual(result.translation_language, 'ENG')
            self.assertEqual(result.preview_status, 'disabled')

    async def test_execute_resumes_completed_setup_and_remaining_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = WorkspacePaths(root=root / 'workspace')
            (paths.repo_dir / 'src').mkdir(parents=True, exist_ok=True)
            (paths.repo_dir / 'src' / 'main.py').write_text(
                'def greet(name):\n'
                "    return f'hello {name}'\n"
            )
            paths.changed_files.parent.mkdir(parents=True, exist_ok=True)
            paths.changed_files.write_text(
                json.dumps(
                    [
                        {
                            'old_path': 'src/main.py',
                            'new_path': 'src/main.py',
                            'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                        }
                    ]
                )
            )
            paths.existing_discussions.write_text('[]')
            paths.existing_comments.write_text('[]')
            indexing = await ReviewWorkspaceService.build_repo_catalog(
                paths,
                enabled=True,
                max_catalog_file_bytes=4096,
            )
            eligible = await ReviewWorkspaceService.gate_changed_files(
                paths,
                max_eligible=10,
                large_file_bytes=65536,
                max_cluster_reviews=0,
            )
            cluster_plan = json.loads(paths.cluster_plan.read_text())
            paths.findings_dir.mkdir(parents=True)
            (paths.findings_dir / '001-correctness.json').write_text(json.dumps({'findings': []}))

            progress = ReviewProgressStore(paths)
            mr_url = 'https://gitlab.example.com/group/project/-/merge_requests/7'
            mr = {
                'web_url': mr_url,
                'http_url_to_repo': 'https://gitlab.example.com/group/project.git',
                'target_branch': 'main',
                'diff_refs': {'base_sha': 'base', 'start_sha': 'start', 'head_sha': 'head'},
            }
            await progress.initialize(
                mr_url=mr_url,
                project_path='group/project',
                mr_iid='7',
                model='test-model',
                preview_mode=False,
            )
            await progress.mark_stage_completed(
                'resolve_workspace',
                'Resolve merge request and prepare workspace',
                {
                    'mr_url': mr_url,
                    'project_path': 'group/project',
                    'mr_iid': '7',
                    'workspace': str(paths.root),
                    'mr': mr,
                    'diff_refs': mr['diff_refs'],
                },
            )
            await progress.mark_stage_completed(
                'collect_state',
                'Collect changed files and existing discussions',
                {'discussions_count': 0, 'comments_count': 0, 'files_changed': 1},
            )
            await progress.mark_stage_completed('summary', 'Generate merge request summary', {'summary_status': 'written'})
            await progress.mark_stage_completed(
                'analyze_files',
                'Select and analyze reviewable files',
                {
                    'indexing_summary': indexing,
                    'eligible_count': eligible,
                    'cluster_plan': cluster_plan,
                },
            )

            agent = _CountingAgent()
            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                    enabled_cluster_pass_ids=(),
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FailingGitLabClient(),
                    agent=agent,
                    output=_RecordingReviewOutput(),
                ),
            )

            async def fake_publish_review(**kwargs) -> dict[str, object]:
                result_path = kwargs['result_path']
                result_path.write_text(json.dumps({'inline_posts': [], 'overall_comment': {'status': 'ok'}}))
                return {
                    'posted': 0,
                    'anchor_errors': 0,
                    'out_of_hunk_count': 0,
                    'verdict': 'approve',
                    'overall_comment_status': 'ok',
                    'overall_note_id': None,
                    'overall_comment_error': None,
                    'result_saved_to': str(result_path),
                }

            with patch(
                'application.review_flow.ReviewPublisher.publish_prepared_review',
                side_effect=fake_publish_review,
            ):
                result = await use_case.execute(
                    mr_url,
                    options=ReviewRunOptions(preview_mode=None, resume_workspace=paths.root),
                )

            self.assertEqual(result.review_passes, 2)
            self.assertEqual(agent.agent_names.count('review-agent'), 1)
            self.assertEqual(agent.agent_names.count('summarize-pr-agent'), 0)
            self.assertEqual(json.loads(paths.progress.read_text())['status'], 'completed')

    async def test_execute_does_not_republish_when_publish_result_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = WorkspacePaths(root=root / 'workspace')
            paths.root.mkdir(parents=True)
            mr_url = 'https://gitlab.example.com/group/project/-/merge_requests/7'
            publication = {
                'findings': [{'short_title': 'Cached', 'anchor': {'new_path': 'src/app.py', 'new_line': 10}}],
                'out_of_hunk_findings': [],
                'counts': {'Major': 1},
                'verdict': 'request changes',
                'summary_labels': {},
            }
            paths.preview_publication.write_text(json.dumps(publication))
            paths.publish_result.write_text(
                json.dumps(
                    {
                        'inline_posts': [{'status': 'ok', 'note_id': '8'}],
                        'overall_comment': {'status': 'ok', 'note_id': '9', 'error': None},
                    }
                )
            )

            progress = ReviewProgressStore(paths)
            await progress.initialize(
                mr_url=mr_url,
                project_path='group/project',
                mr_iid='7',
                model='test-model',
                preview_mode=False,
            )
            await progress.mark_stage_completed(
                'resolve_workspace',
                'Resolve merge request and prepare workspace',
                {
                    'mr_url': mr_url,
                    'project_path': 'group/project',
                    'mr_iid': '7',
                    'workspace': str(paths.root),
                    'mr': {'web_url': mr_url, 'diff_refs': {'base_sha': 'base', 'start_sha': 'start', 'head_sha': 'head'}},
                    'diff_refs': {'base_sha': 'base', 'start_sha': 'start', 'head_sha': 'head'},
                },
            )
            await progress.mark_stage_completed(
                'collect_state',
                'Collect changed files and existing discussions',
                {'discussions_count': 0, 'comments_count': 0, 'files_changed': 1},
            )
            await progress.mark_stage_completed('summary', 'Generate merge request summary', {'summary_status': 'written'})
            await progress.mark_stage_completed(
                'analyze_files',
                'Select and analyze reviewable files',
                {'indexing_summary': {'indexed_files': 0}, 'eligible_count': 1, 'cluster_plan': []},
            )
            await progress.mark_stage_completed('review_passes', 'Run review passes', {'review_passes': 1})
            await progress.mark_stage_completed('cluster_passes', 'Run cluster review passes', {'cluster_reviews': 0})
            await progress.mark_stage_completed(
                'finalize_findings',
                'Validate, consolidate, and deduplicate findings',
                {
                    'consolidation': {'count': 1, 'invalid': 0, 'anomalies': []},
                    'final_findings': {'kept': 1, 'groups': 0, 'line_window': 3, 'parallel_reviews': 0, 'deduplicated': 0},
                },
            )
            await progress.mark_stage_completed(
                'translation',
                'Translate publishable review output',
                {
                    'translated_publication': publication,
                    'translation_summary': {'status': 'skipped', 'language': 'ENG', 'jobs': 0, 'parallel_reviews': 0},
                },
            )
            await progress.mark_stage_completed(
                'preview',
                'Preview translated review findings',
                {
                    'preview_publication': publication,
                    'preview_summary': {
                        'status': 'disabled',
                        'items': 1,
                        'edited': 0,
                        'unpublished': 0,
                        'report_path': str(paths.preview_report),
                    },
                },
            )

            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FailingGitLabClient(),
                    agent=_CountingAgent(),
                    output=_RecordingReviewOutput(),
                ),
            )

            async def unexpected_publish(**kwargs) -> dict[str, object]:
                raise AssertionError('publish should not be called when publish-result.json exists')

            with patch(
                'application.review_flow.ReviewPublisher.publish_prepared_review',
                side_effect=unexpected_publish,
            ):
                result = await use_case.execute(
                    mr_url,
                    options=ReviewRunOptions(preview_mode=None, resume_workspace=paths.root),
                )

            self.assertEqual(result.posted, 1)
            self.assertEqual(result.overall_note_id, '9')
            self.assertEqual(result.result_saved_to, str(paths.publish_result))

    async def test_execute_passes_translated_publication_to_publish_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / 'workspace'

            async def workspace_provisioner(**kwargs) -> WorkspacePaths:
                paths = WorkspacePaths(root=workspace_root)
                (paths.repo_dir / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
                (paths.repo_dir / 'src' / 'auth' / 'handler.py').write_text(
                    'from src.auth.service import AuthService\n'
                    '\n'
                    'class AuthHandler:\n'
                    '    def handle(self):\n'
                    '        return AuthService()\n'
                )
                (paths.repo_dir / 'src' / 'auth' / 'service.py').write_text(
                    'class AuthService:\n'
                    '    def validate(self):\n'
                    '        return True\n'
                )
                paths.meta.parent.mkdir(parents=True, exist_ok=True)
                paths.meta.write_text(
                    json.dumps(
                        {
                            'project_path': 'group/project',
                            'mr_iid': '7',
                            'head_sha': 'head',
                            'base_sha': 'base',
                            'start_sha': 'start',
                        }
                    )
                )
                return paths

            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                    translation_language='RUS',
                    parallel_translation_reviews=2,
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FakeGitLabExecuteClient(),
                    agent=_TrackingAgent(),
                    output=_RecordingReviewOutput(),
                    workspace_provisioner=workspace_provisioner,
                ),
            )
            publish_calls: list[dict[str, object]] = []

            async def fake_translate_publication(
                paths,
                agent,
                model,
                publication,
                target_language,
                parallel_reviews,
                output,
            ) -> tuple[dict[str, object], dict[str, object]]:
                translated = json.loads(json.dumps(publication))
                translated['summary_labels']['title'] = 'RUS summary'
                return translated, {
                    'status': 'translated',
                    'language': target_language,
                    'jobs': 1,
                    'parallel_reviews': parallel_reviews,
                    'artifact_path': str(paths.translated_publication),
                    'report_path': str(paths.translation_report),
                }

            async def fake_publish_review(**kwargs) -> dict[str, object]:
                publish_calls.append(kwargs['publication'])
                result_path = kwargs['result_path']
                result_path.write_text(json.dumps({'ok': True}))
                return {
                    'posted': 0,
                    'anchor_errors': 0,
                    'out_of_hunk_count': 0,
                    'verdict': 'approve',
                    'overall_comment_status': 'ok',
                    'overall_note_id': '1',
                    'overall_comment_error': None,
                    'result_saved_to': str(result_path),
                }

            with patch(
                'application.review_flow.ReviewTranslationService.translate_publication',
                side_effect=fake_translate_publication,
            ):
                with patch(
                    'application.review_flow.ReviewPublisher.publish_prepared_review',
                    side_effect=fake_publish_review,
                ):
                    result = await use_case.execute(
                        'https://gitlab.example.com/group/project/-/merge_requests/7',
                        options=ReviewRunOptions(model='test-model', preview_mode=False),
                    )

            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(publish_calls[0]['summary_labels']['title'], 'RUS summary')
            self.assertEqual(result.translation_status, 'translated')
            self.assertEqual(result.translation_language, 'RUS')
            self.assertEqual(result.preview_status, 'disabled')

    async def test_execute_passes_previewed_publication_to_publish_when_preview_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / 'workspace'

            async def workspace_provisioner(**kwargs) -> WorkspacePaths:
                paths = WorkspacePaths(root=workspace_root)
                (paths.repo_dir / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
                (paths.repo_dir / 'src' / 'auth' / 'handler.py').write_text(
                    'from src.auth.service import AuthService\n'
                    '\n'
                    'class AuthHandler:\n'
                    '    def handle(self):\n'
                    '        return AuthService()\n'
                )
                (paths.repo_dir / 'src' / 'auth' / 'service.py').write_text(
                    'class AuthService:\n'
                    '    def validate(self):\n'
                    '        return True\n'
                )
                paths.meta.parent.mkdir(parents=True, exist_ok=True)
                paths.meta.write_text(
                    json.dumps(
                        {
                            'project_path': 'group/project',
                            'mr_iid': '7',
                            'head_sha': 'head',
                            'base_sha': 'base',
                            'start_sha': 'start',
                        }
                    )
                )
                return paths

            previewer = _EditingPreviewer()
            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                    translation_language='RUS',
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FakeGitLabExecuteClient(),
                    agent=_TrackingAgent(),
                    output=_RecordingReviewOutput(),
                    previewer=previewer,
                    workspace_provisioner=workspace_provisioner,
                ),
            )
            publish_calls: list[dict[str, object]] = []

            async def fake_translate_publication(
                paths,
                agent,
                model,
                publication,
                target_language,
                parallel_reviews,
                output,
            ) -> tuple[dict[str, object], dict[str, object]]:
                translated = json.loads(json.dumps(publication))
                translated['findings'] = [
                    {
                        'severity': 'Major',
                        'short_title': 'Original preview title',
                        'body': 'Original preview body',
                        'anchor': {
                            'old_path': 'src/auth/handler.py',
                            'new_path': 'src/auth/handler.py',
                            'new_line': 4,
                            'old_line': None,
                        },
                    }
                ]
                translated['counts'] = {'Major': 1}
                translated['verdict'] = 'request changes'
                return translated, {
                    'status': 'translated',
                    'language': target_language,
                    'jobs': 1,
                    'parallel_reviews': parallel_reviews,
                    'artifact_path': str(paths.translated_publication),
                    'report_path': str(paths.translation_report),
                }

            async def fake_publish_review(**kwargs) -> dict[str, object]:
                publish_calls.append(kwargs['publication'])
                result_path = kwargs['result_path']
                result_path.write_text(json.dumps({'ok': True}))
                return {
                    'posted': 0,
                    'anchor_errors': 0,
                    'out_of_hunk_count': 0,
                    'verdict': kwargs['publication']['verdict'],
                    'overall_comment_status': 'ok',
                    'overall_note_id': '1',
                    'overall_comment_error': None,
                    'result_saved_to': str(result_path),
                }

            with patch(
                'application.review_flow.ReviewTranslationService.translate_publication',
                side_effect=fake_translate_publication,
            ):
                with patch(
                    'application.review_flow.ReviewPublisher.publish_prepared_review',
                    side_effect=fake_publish_review,
                ):
                    result = await use_case.execute(
                        'https://gitlab.example.com/group/project/-/merge_requests/7',
                        options=ReviewRunOptions(model='test-model', preview_mode=True),
                    )

            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(publish_calls[0]['findings'][0]['short_title'], 'Preview edited title')
            self.assertEqual(publish_calls[0]['findings'][0]['body'], 'Preview edited body')
            self.assertEqual(result.preview_status, 'reviewed')
            self.assertEqual(result.preview_edited, 1)

    async def test_execute_stops_before_publish_when_translation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / 'workspace'

            async def workspace_provisioner(**kwargs) -> WorkspacePaths:
                paths = WorkspacePaths(root=workspace_root)
                (paths.repo_dir / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
                (paths.repo_dir / 'src' / 'auth' / 'handler.py').write_text(
                    'from src.auth.service import AuthService\n'
                    '\n'
                    'class AuthHandler:\n'
                    '    def handle(self):\n'
                    '        return AuthService()\n'
                )
                (paths.repo_dir / 'src' / 'auth' / 'service.py').write_text(
                    'class AuthService:\n'
                    '    def validate(self):\n'
                    '        return True\n'
                )
                paths.meta.parent.mkdir(parents=True, exist_ok=True)
                paths.meta.write_text(
                    json.dumps(
                        {
                            'project_path': 'group/project',
                            'mr_iid': '7',
                            'head_sha': 'head',
                            'base_sha': 'base',
                            'start_sha': 'start',
                        }
                    )
                )
                return paths

            config = Config(
                gitlab=GitLabConfig(token='token', api_url='https://gitlab.example.com/api/v4'),
                agent=AgentConfig(base_url='http://localhost:1234/v1', api_key='secret', timeout_seconds=30),
                review=ReviewConfig(
                    review_root=root,
                    resources_dir=Path('/Users/franz/review/resources/review'),
                    agents_dir=Path('/Users/franz/review/resources/agents'),
                    translation_language='DE',
                ),
            )
            use_case = ReviewMergeRequestUseCase(
                config,
                ReviewDependencies(
                    gitlab=_FakeGitLabExecuteClient(),
                    agent=_TrackingAgent(),
                    output=_RecordingReviewOutput(),
                    workspace_provisioner=workspace_provisioner,
                ),
            )
            publish_called = False

            async def fake_translate_publication(*args, **kwargs):
                raise RuntimeError('translation failed')

            async def fake_publish_review(**kwargs) -> dict[str, object]:
                nonlocal publish_called
                publish_called = True
                return {}

            with patch(
                'application.review_flow.ReviewTranslationService.translate_publication',
                side_effect=fake_translate_publication,
            ):
                with patch(
                    'application.review_flow.ReviewPublisher.publish_prepared_review',
                    side_effect=fake_publish_review,
                ):
                    with self.assertRaisesRegex(RuntimeError, 'translation failed'):
                        await use_case.execute(
                            'https://gitlab.example.com/group/project/-/merge_requests/7',
                            model='test-model',
                        )

            self.assertFalse(publish_called)


class _FakeGitLabStateClient:
    def __init__(self, discussions: list[dict], changes: dict) -> None:
        self._discussions = discussions
        self._changes = changes

    def proj_url(self, project_id: str | int, *segments: str) -> str:
        return f'https://gitlab.example.com/{project_id}/' + '/'.join(segments)

    async def get_paged(self, url: str) -> list[dict]:
        return self._discussions

    async def get_one(self, url: str) -> dict:
        return self._changes


class _FakeGitLabExecuteClient:
    def proj_url(self, project_id: str | int, *segments: str) -> str:
        return f'https://gitlab.example.com/{project_id}/' + '/'.join(segments)

    async def get_paged(self, url: str) -> list[dict]:
        return []

    async def get_one(self, url: str) -> dict:
        if url.endswith('/changes'):
            return {
                'changes': [
                    {
                        'old_path': 'src/auth/handler.py',
                        'new_path': 'src/auth/handler.py',
                        'new_file': False,
                        'deleted_file': False,
                        'renamed_file': False,
                        'diff': (
                            '@@ -0,0 +1,5 @@\n'
                            '+from src.auth.service import AuthService\n'
                            '+\n'
                            '+class AuthHandler:\n'
                            '+    def handle(self):\n'
                            '+        return AuthService()\n'
                        ),
                    }
                ]
            }
        return {
            'id': 7,
            'iid': 7,
            'title': 'Auth change',
            'source_branch': 'feature',
            'target_branch': 'main',
            'diff_refs': {
                'base_sha': 'base',
                'start_sha': 'start',
                'head_sha': 'head',
            },
            'web_url': 'https://gitlab.example.com/group/project/-/merge_requests/7',
            'http_url_to_repo': 'https://gitlab.example.com/group/project.git',
        }


class _FailingGitLabClient:
    def proj_url(self, project_id: str | int, *segments: str) -> str:
        return f'https://gitlab.example.com/{project_id}/' + '/'.join(segments)

    async def get_paged(self, url: str) -> list[dict]:
        raise AssertionError('GitLab should not be called for completed resume stages')

    async def get_one(self, url: str) -> dict:
        raise AssertionError('GitLab should not be called for completed resume stages')

    async def post(self, url: str, payload: dict) -> dict:
        raise AssertionError('GitLab should not be called when publish is patched')


class _TrackingAgent:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.02)
        self.in_flight -= 1
        return ''


class _CountingAgent(_TrackingAgent):
    def __init__(self) -> None:
        super().__init__()
        self.agent_names: list[str] = []

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        self.agent_names.append(agent_name)
        return await super().run(agent_name, prompt, model)


class _RecordingReviewOutput:
    def __init__(self) -> None:
        self.progress_updates = []

    def step_started(self, index: int, total: int, title: str) -> None:
        return None

    def detail(self, label: str, value: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def task_progress(self, progress) -> None:
        self.progress_updates.append(progress)

    def completed(self, result) -> None:
        return None

    def failed(self, exc: BaseException) -> None:
        return None

    def cancelled(self) -> None:
        return None


class _EditingPreviewer:
    async def preview(self, session):
        from dataclasses import replace

        updated = replace(
            session.items[0],
            short_title='Preview edited title',
            body='Preview edited body',
        )
        from domain.review.preview import PreviewSessionResult

        return PreviewSessionResult(items=(updated, *session.items[1:]))


if __name__ == '__main__':
    unittest.main()

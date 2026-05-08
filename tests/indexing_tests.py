from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from settings import RetrievalPolicyConfig
from domain.review.context import ReviewContextBuilder, ReviewContextLimits
from domain.review.indexing import RepoCatalogBuilder, ReviewRetrievalPlanner
from domain.review.standards import ReviewPlan, Resources


class RepoCatalogBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_indexes_tracked_source_files_and_skips_large_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'handler.py').write_text(
                'from src.auth.service import AuthService\n'
                '\n'
                'class AuthHandler:\n'
                '    async def handle(self):\n'
                '        return AuthService()\n'
            )
            (repo / 'src' / 'auth' / 'service.py').write_text(
                'class AuthService:\n'
                '    pass\n'
            )
            (repo / 'src' / 'ui').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'ui' / 'serializer.ts').write_text(
                "import { UserDto } from './dto'\n"
                'export function serializeUser(payload: UserDto) {\n'
                '  return payload\n'
                '}\n'
            )
            (repo / 'src' / 'ui' / 'dto.ts').write_text('export type UserDto = { id: string }\n')
            (repo / 'src' / 'auth' / 'large.py').write_text('x' * 300)
            (repo / 'src' / 'auth' / 'binary.py').write_bytes(b'\x00\x01\x02')
            (repo / 'README.md').write_text('ignored\n')
            _init_git_repo(repo)

            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)

            self.assertEqual(catalog['indexed_files'], 4)
            self.assertEqual(catalog['skipped_large_files'], 1)
            self.assertEqual(catalog['skipped_binary_files'], 1)
            self.assertIn('src/auth/handler.py', catalog['files'])
            self.assertIn('src/ui/serializer.ts', catalog['files'])
            self.assertNotIn('README.md', catalog['files'])
            handler = catalog['files']['src/auth/handler.py']
            serializer = catalog['files']['src/ui/serializer.ts']
            self.assertIn('src.auth.service', handler['import_targets'])
            self.assertIn('AuthHandler', handler['symbol_names'])
            self.assertEqual(
                handler['symbol_spans'][0],
                {
                    'name': 'AuthHandler',
                    'line_start': 3,
                    'line_end': 3,
                },
            )
            self.assertEqual(handler['symbol_spans'][1]['name'], 'handle')
            self.assertIn('./dto', serializer['import_targets'])
            self.assertIn('serializeUser', serializer['symbol_names'])
            self.assertEqual(serializer['symbol_spans'][0]['name'], 'serializeUser')
            self.assertIn('AuthHandler', catalog['lookups']['symbols'])

    async def test_build_keeps_shallow_metadata_for_mapped_languages_without_extractors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'pkg').mkdir(parents=True, exist_ok=True)
            (repo / 'pkg' / 'service.go').write_text(
                'package pkg\n'
                '\n'
                'func HandleRequest() {}\n'
            )
            _init_git_repo(repo)

            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)

            entry = catalog['files']['pkg/service.go']
            self.assertEqual(entry['language'], 'go')
            self.assertEqual(entry['import_lines'], [])
            self.assertEqual(entry['symbol_names'], [])
            self.assertEqual(entry['symbol_spans'], [])
            self.assertIn('service', entry['path_tokens'])
            self.assertIn('service', entry['role_hints'])


class ReviewRetrievalPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_retrieval_policy_skips_correctness_and_allows_single_security_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'service.py').write_text(
                'class AuthService:\n'
                '    def validate(self):\n'
                '        return True\n'
            )
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            changed_file = {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                    'directory': 'src/auth',
                    'path_tokens': ['src', 'auth', 'handler'],
                    'role_hints': ['service'],
                    'retrieval_hints': [
                        {
                            'kind': 'import_owner',
                            'query': 'src.auth.service',
                            'reason': 'Find the owner for src.auth.service.',
                            'source_path': 'src/auth/handler.py',
                        }
                    ],
                },
            }

            correctness_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='correctness',
                focus_area='correctness',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='correctness',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                ),
            )
            security_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='security',
                focus_area='security',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='security',
                    max_artifacts=3,
                    max_chars=40,
                    max_lines_per_artifact=40,
                ),
            )

            self.assertEqual(correctness_plan['stats']['executed_retrievals'], 0)
            self.assertEqual(correctness_plan['stats']['skipped_by_policy'], 1)
            self.assertEqual(security_plan['stats']['executed_retrievals'], 1)
            artifact = security_plan['artifacts'][0]
            self.assertEqual(artifact['path'], 'src/auth/service.py')
            self.assertEqual(artifact['unit'], 'file')
            self.assertIn('AuthService', artifact['snippet'])
            self.assertIn('[truncated]', artifact['snippet'])
            self.assertEqual(security_plan['applied_policy']['max_artifacts'], 1)

    async def test_symbol_retrieval_returns_symbol_scoped_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'service.py').write_text(
                'class AuthService:\n'
                '    def validate(self):\n'
                '        return True\n'
            )
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            changed_file = {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                    'directory': 'src/auth',
                    'path_tokens': ['src', 'auth', 'handler'],
                    'role_hints': ['service'],
                    'retrieval_hints': [
                        {
                            'kind': 'symbol_definition',
                            'query': 'AuthService',
                            'reason': 'Inspect the AuthService definition.',
                            'source_path': 'src/auth/handler.py',
                        }
                    ],
                },
            }

            plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='security',
                focus_area='security',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='security',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(
                        allowed_kinds=('symbol_definition',),
                    ),
                ),
            )

            self.assertEqual(plan['stats']['executed_retrievals'], 1)
            artifact = plan['artifacts'][0]
            self.assertEqual(artifact['unit'], 'symbol')
            self.assertEqual(artifact['symbol_name'], 'AuthService')
            self.assertEqual(artifact['path'], 'src/auth/service.py')
            self.assertIn('class AuthService:', artifact['snippet'])

    async def test_symbol_retrieval_falls_back_to_file_snippet_without_exact_symbol_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'service.py').write_text(
                'class AuthService:\n'
                '    def validate(self):\n'
                '        return True\n'
            )
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            changed_file = {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                    'directory': 'src/auth',
                    'path_tokens': ['src', 'auth', 'handler'],
                    'role_hints': ['service'],
                    'retrieval_hints': [
                        {
                            'kind': 'service_partner',
                            'query': 'service',
                            'reason': 'Inspect the neighboring service context.',
                            'source_path': 'src/auth/handler.py',
                        }
                    ],
                },
            }

            plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='correctness',
                focus_area='correctness',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='correctness',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(
                        enabled=True,
                        allowed_kinds=('service_partner',),
                        hint_selection='exactly_one',
                    ),
                ),
            )

            self.assertEqual(plan['stats']['executed_retrievals'], 1)
            artifact = plan['artifacts'][0]
            self.assertEqual(artifact['unit'], 'file')
            self.assertNotIn('symbol_name', artifact)
            self.assertIn('class AuthService:', artifact['snippet'])

    async def test_file_policy_override_can_disable_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'service.py').write_text('class AuthService:\n    pass\n')
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            changed_file = {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                    'directory': 'src/auth',
                    'path_tokens': ['src', 'auth', 'handler'],
                    'role_hints': ['service'],
                    'retrieval_hints': [
                        {
                            'kind': 'import_owner',
                            'query': 'src.auth.service',
                            'reason': 'Find the owner for src.auth.service.',
                            'source_path': 'src/auth/handler.py',
                        }
                    ],
                },
            }

            plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='security',
                focus_area='security',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='security',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(enabled=False),
                ),
            )

            self.assertEqual(plan['stats']['executed_retrievals'], 0)
            self.assertEqual(plan['stats']['skipped_by_policy'], 1)
            self.assertFalse(plan['applied_policy']['enabled'])

    async def test_file_policy_override_can_relax_unique_top_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'auth').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'auth' / 'service.py').write_text('class AuthService:\n    pass\n')
            (repo / 'src' / 'auth' / 'manager.py').write_text('class AuthManager:\n    pass\n')
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            changed_file = {
                'old_path': 'src/auth/handler.py',
                'new_path': 'src/auth/handler.py',
                'analysis': {
                    'language': 'python',
                    'directory': 'src/auth',
                    'path_tokens': ['src', 'auth', 'handler'],
                    'role_hints': ['service'],
                    'retrieval_hints': [
                        {
                            'kind': 'service_partner',
                            'query': 'service',
                            'reason': 'Inspect the neighboring service context.',
                            'source_path': 'src/auth/handler.py',
                        }
                    ],
                },
            }

            strict_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='correctness',
                focus_area='correctness',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='correctness',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(
                        enabled=True,
                        allowed_kinds=('service_partner',),
                        hint_selection='exactly_one',
                        require_unique_top=True,
                    ),
                ),
            )
            relaxed_plan = await ReviewRetrievalPlanner.plan_file_task(
                changed_file=changed_file,
                pass_id='correctness',
                focus_area='correctness',
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_file_policy(
                    pass_id='correctness',
                    max_artifacts=3,
                    max_chars=500,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(
                        enabled=True,
                        allowed_kinds=('service_partner',),
                        hint_selection='exactly_one',
                        require_unique_top=False,
                    ),
                ),
            )

            self.assertEqual(strict_plan['stats']['executed_retrievals'], 0)
            self.assertEqual(strict_plan['stats']['empty_retrievals'], 1)
            self.assertEqual(relaxed_plan['stats']['executed_retrievals'], 1)

    async def test_cluster_retrieval_respects_artifact_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'api').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'api' / 'dto.py').write_text('class UserDto:\n    pass\n')
            (repo / 'src' / 'api' / 'serializer.py').write_text('class UserSerializer:\n    pass\n')
            (repo / 'src' / 'api' / 'service.py').write_text('class ApiService:\n    pass\n')
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            cluster = {
                'cluster_id': 'cluster_01',
                'title': 'Api Boundary changes in src/api',
                'focus_area': 'api_boundary',
                'files': ['src/api/handler.py', 'src/api/serializer_changed.py'],
                'retrieval_requests': [
                    {
                        'kind': 'schema_partner',
                        'query': 'UserDto',
                        'reason': 'Inspect related schema context for UserDto.',
                    },
                    {
                        'kind': 'service_partner',
                        'query': 'service',
                        'reason': 'Inspect neighboring service context.',
                    },
                ],
            }
            cluster_files = [
                {
                    'new_path': 'src/api/handler.py',
                    'analysis': {
                        'language': 'python',
                        'directory': 'src/api',
                        'path_tokens': ['src', 'api', 'handler'],
                        'role_hints': ['handler'],
                    },
                },
                {
                    'new_path': 'src/api/serializer_changed.py',
                    'analysis': {
                        'language': 'python',
                        'directory': 'src/api',
                        'path_tokens': ['src', 'api', 'serializer'],
                        'role_hints': ['serializer'],
                    },
                },
            ]

            plan = await ReviewRetrievalPlanner.plan_cluster_task(
                cluster=cluster,
                cluster_files=cluster_files,
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_cluster_policy(
                    focus_area='api_boundary',
                    max_artifacts=1,
                    max_chars=400,
                    max_lines_per_artifact=40,
                ),
            )

            self.assertEqual(plan['stats']['planned_retrievals'], 2)
            self.assertEqual(plan['stats']['executed_retrievals'], 1)

    async def test_cluster_policy_override_filters_request_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'src' / 'api').mkdir(parents=True, exist_ok=True)
            (repo / 'src' / 'api' / 'dto.py').write_text('class UserDto:\n    pass\n')
            (repo / 'src' / 'api' / 'service.py').write_text('class ApiService:\n    pass\n')
            _init_git_repo(repo)
            catalog = await RepoCatalogBuilder.build(repo, max_file_bytes=200)
            cluster = {
                'cluster_id': 'cluster_01',
                'title': 'Api Boundary changes in src/api',
                'focus_area': 'api_boundary',
                'files': ['src/api/handler.py', 'src/api/serializer_changed.py'],
                'retrieval_requests': [
                    {
                        'kind': 'schema_partner',
                        'query': 'UserDto',
                        'reason': 'Inspect related schema context for UserDto.',
                    },
                    {
                        'kind': 'service_partner',
                        'query': 'service',
                        'reason': 'Inspect neighboring service context.',
                    },
                ],
            }
            cluster_files = [
                {
                    'new_path': 'src/api/handler.py',
                    'analysis': {
                        'language': 'python',
                        'directory': 'src/api',
                        'path_tokens': ['src', 'api', 'handler'],
                        'role_hints': ['handler'],
                    },
                },
                {
                    'new_path': 'src/api/serializer_changed.py',
                    'analysis': {
                        'language': 'python',
                        'directory': 'src/api',
                        'path_tokens': ['src', 'api', 'serializer'],
                        'role_hints': ['serializer'],
                    },
                },
            ]

            plan = await ReviewRetrievalPlanner.plan_cluster_task(
                cluster=cluster,
                cluster_files=cluster_files,
                catalog=catalog,
                repo_dir=repo,
                policy=ReviewRetrievalPlanner.resolve_cluster_policy(
                    focus_area='api_boundary',
                    max_artifacts=3,
                    max_chars=400,
                    max_lines_per_artifact=40,
                    policy_override=RetrievalPolicyConfig(
                        allowed_kinds=('schema_partner',),
                        max_artifacts=1,
                    ),
                ),
            )

            self.assertEqual(plan['stats']['planned_retrievals'], 1)
            self.assertEqual(plan['stats']['executed_retrievals'], 1)
            self.assertEqual(plan['applied_policy']['allowed_kinds'], ['schema_partner'])


class ReviewContextBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_file_context_adds_related_repo_section_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'src' / 'main.py'
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                'def greet(name):\n'
                '    return name\n'
            )
            excerpts_path = root / 'excerpt.txt'
            excerpts_path.write_text('1: def greet(name):\n2:     return name\n')
            context_path = root / 'context.md'
            resources = Resources(
                severity='severity',
                template='template',
                standard_texts={},
                review_plan=ReviewPlan(language_standards={}, file_passes=(), cluster_passes=()),
                project_profile='profile',
            )
            changed_file = {
                'old_path': 'src/main.py',
                'new_path': 'src/main.py',
                'hunks': [{'old_start': 1, 'old_count': 2, 'new_start': 1, 'new_count': 2}],
                'analysis': {
                    'language': 'python',
                    'focus_areas': ['correctness'],
                },
            }

            await ReviewContextBuilder.write_file_context(
                changed_file,
                root,
                excerpts_path,
                context_path,
                'correctness',
                'correctness',
                'standards',
                'review plan',
                resources,
                limits=ReviewContextLimits(max_context_chars=800),
                retrieved_artifacts=[
                    {
                        'path': 'src/support.py',
                        'reason': 'Related helper.',
                        'snippet': '1: def helper():\n2:     return True',
                    }
                ],
            )
            context_text = context_path.read_text()

            self.assertIn('Related Repo Context', context_text)
            self.assertIn('src/support.py', context_text)
            self.assertIn('Related helper.', context_text)

            await ReviewContextBuilder.write_file_context(
                changed_file,
                root,
                excerpts_path,
                context_path,
                'correctness',
                'correctness',
                'standards',
                'review plan',
                resources,
                limits=ReviewContextLimits(max_context_chars=800),
            )
            context_without_retrieval = context_path.read_text()
            self.assertNotIn('Related Repo Context', context_without_retrieval)


def _init_git_repo(repo: Path) -> None:
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()

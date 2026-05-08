from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yaml import safe_dump

from settings import ConfigLoader


class ConfigLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_reads_review_context_limits_and_scale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'model': {
                            'generationConfig': {
                                'timeout': 120000,
                            }
                        },
                        'review': {
                            'parallelReviews': 3,
                            'parallelClusterReviews': 2,
                            'dedup': {
                                'lineWindow': 5,
                                'parallelReviews': 4,
                            },
                            'translation': {
                                'language': 'RUS',
                                'parallelReviews': 3,
                            },
                            'indexing': {
                                'enabled': False,
                                'maxCatalogFileBytes': 2048,
                                'maxRetrievedArtifactsPerTask': 2,
                                'maxRetrievedCharsPerTask': 1800,
                                'maxRetrievedLinesPerArtifact': 40,
                                'filePolicies': {
                                    'security': {
                                        'enabled': True,
                                        'allowedKinds': [
                                            'import_owner',
                                            'symbol_definition',
                                        ],
                                        'hintSelection': 'first_n',
                                        'minScore': 90,
                                        'requireUniqueTop': False,
                                        'maxArtifacts': 2,
                                        'maxChars': 900,
                                        'maxLinesPerArtifact': 12,
                                    }
                                },
                                'clusterPolicies': {
                                    'api_boundary': {
                                        'enabled': True,
                                        'allowedKinds': [
                                            'schema_partner',
                                            'import_owner',
                                        ],
                                        'hintSelection': 'first_n',
                                        'maxArtifacts': 1,
                                    }
                                },
                            },
                            'context': {
                                'scale': 1.25,
                                'maxContextChars': 20000,
                                'maxClusterContextChars': 22000,
                                'maxSymbolBlocks': 3,
                                'maxSymbolLines': 50,
                                'clusterExcerptRadius': 6,
                                'clusterMaxSegments': 4,
                                'sectionChars': {
                                    'reviewGoal': 480,
                                    'projectProfile': 1500,
                                    'reviewPlan': 1800,
                                    'standards': 9000,
                                },
                            }
                        },
                    }
                )
            )

            config = await ConfigLoader.load(
                env_path=env_path,
                settings_path=settings_path,
                review_root=root,
                resources_dir=root / 'resources',
                agents_dir=root / 'agents',
            )

            limits = config.review.context_limits
            self.assertEqual(config.review.parallel_reviews, 3)
            self.assertEqual(config.review.parallel_cluster_reviews, 2)
            self.assertEqual(config.review.http_timeout_seconds, 60)
            self.assertEqual(config.review.parallel_dedup_reviews, 4)
            self.assertEqual(config.review.translation_language, 'RUS')
            self.assertEqual(config.review.parallel_translation_reviews, 3)
            self.assertEqual(config.review.dedup_line_window, 5)
            self.assertFalse(config.review.indexing_enabled)
            self.assertEqual(config.review.max_catalog_file_bytes, 2048)
            self.assertEqual(config.review.max_retrieved_artifacts_per_task, 2)
            self.assertEqual(config.review.max_retrieved_chars_per_task, 1800)
            self.assertEqual(config.review.max_retrieved_lines_per_artifact, 40)
            self.assertEqual(limits.scale, 1.25)
            self.assertEqual(limits.max_context_chars, 20000)
            self.assertEqual(limits.max_cluster_context_chars, 22000)
            self.assertEqual(limits.max_symbol_blocks, 3)
            self.assertEqual(limits.max_symbol_lines, 50)
            self.assertEqual(limits.cluster_excerpt_radius, 6)
            self.assertEqual(limits.cluster_max_segments, 4)
            self.assertEqual(limits.scaled_value(limits.max_context_chars), 25000)
            self.assertEqual(limits.section_chars()['review_goal'], 600)
            self.assertEqual(limits.section_chars()['review_plan'], 2250)
            self.assertEqual(limits.section_chars()['standards'], 11250)
            self.assertIsNone(config.review.enabled_file_pass_ids)
            self.assertIsNone(config.review.enabled_cluster_pass_ids)
            security_policy = config.review.file_retrieval_policies['security']
            self.assertTrue(security_policy.enabled)
            self.assertEqual(
                security_policy.allowed_kinds,
                ('import_owner', 'symbol_definition'),
            )
            self.assertEqual(security_policy.hint_selection, 'first_n')
            self.assertEqual(security_policy.min_score, 90)
            self.assertFalse(security_policy.require_unique_top)
            self.assertEqual(security_policy.max_artifacts, 2)
            self.assertEqual(security_policy.max_chars, 900)
            self.assertEqual(security_policy.max_lines_per_artifact, 12)
            api_boundary_policy = config.review.cluster_retrieval_policies['api_boundary']
            self.assertTrue(api_boundary_policy.enabled)
            self.assertEqual(
                api_boundary_policy.allowed_kinds,
                ('schema_partner', 'import_owner'),
            )
            self.assertEqual(api_boundary_policy.hint_selection, 'first_n')
            self.assertEqual(api_boundary_policy.max_artifacts, 1)

    async def test_load_accepts_snake_case_context_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'parallel_reviews': 2,
                            'dedup': {
                                'line_window': 2,
                                'parallel_reviews': 3,
                            },
                            'translation': {
                                'language': 'de',
                                'parallel_reviews': 2,
                            },
                            'indexing': {
                                'file_policies': {
                                    'correctness': {
                                        'enabled': True,
                                        'allowed_kinds': ['symbol_definition'],
                                        'hint_selection': 'exactly_one',
                                        'max_lines_per_artifact': 16,
                                    }
                                },
                                'cluster_policies': {
                                    'architecture': {
                                        'enabled': False,
                                        'require_unique_top': True,
                                    }
                                },
                            },
                            'context': {
                                'scale': 0.5,
                                'max_context_chars': 16000,
                                'section_chars': {
                                    'file_profile': 1000,
                                    'imports': 600,
                                },
                            }
                        },
                    }
                )
            )

            config = await ConfigLoader.load(
                env_path=env_path,
                settings_path=settings_path,
                review_root=root,
                resources_dir=root / 'resources',
                agents_dir=root / 'agents',
            )

            limits = config.review.context_limits
            self.assertEqual(config.review.parallel_reviews, 2)
            self.assertEqual(config.review.parallel_cluster_reviews, 2)
            self.assertEqual(config.review.parallel_dedup_reviews, 3)
            self.assertEqual(config.review.translation_language, 'de')
            self.assertEqual(config.review.parallel_translation_reviews, 2)
            self.assertEqual(config.review.dedup_line_window, 2)
            self.assertTrue(config.review.indexing_enabled)
            self.assertEqual(config.review.max_catalog_file_bytes, 131072)
            self.assertEqual(limits.scale, 0.5)
            self.assertEqual(limits.max_context_chars, 16000)
            self.assertEqual(limits.section_chars()['file_profile'], 500)
            self.assertEqual(limits.section_chars()['imports'], 300)
            self.assertEqual(
                config.review.file_retrieval_policies['correctness'].allowed_kinds,
                ('symbol_definition',),
            )
            self.assertEqual(
                config.review.file_retrieval_policies['correctness'].max_lines_per_artifact,
                16,
            )
            self.assertFalse(config.review.cluster_retrieval_policies['architecture'].enabled)
            self.assertTrue(config.review.cluster_retrieval_policies['architecture'].require_unique_top)

    async def test_load_defaults_cluster_parallelism_to_review_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'parallelReviews': 5,
                        },
                    }
                )
            )

            config = await ConfigLoader.load(
                env_path=env_path,
                settings_path=settings_path,
                review_root=root,
                resources_dir=root / 'resources',
                agents_dir=root / 'agents',
            )

            self.assertEqual(config.review.parallel_reviews, 5)
            self.assertEqual(config.review.parallel_cluster_reviews, 5)
            self.assertEqual(config.review.parallel_dedup_reviews, 1)
            self.assertEqual(config.review.translation_language, 'ENG')
            self.assertEqual(config.review.parallel_translation_reviews, 1)

    async def test_load_reads_review_pass_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'passes': {
                                'file': {
                                    'enabled': [
                                        'correctness',
                                        'python',
                                    ],
                                },
                                'cluster_passes': {
                                    'enabled_passes': [
                                        'security',
                                        'api_boundary',
                                    ],
                                },
                            }
                        },
                    }
                )
            )

            config = await ConfigLoader.load(
                env_path=env_path,
                settings_path=settings_path,
                review_root=root,
                resources_dir=root / 'resources',
                agents_dir=root / 'agents',
            )

            self.assertEqual(
                config.review.enabled_file_pass_ids,
                ('correctness', 'python'),
            )
            self.assertEqual(
                config.review.enabled_cluster_pass_ids,
                ('security', 'api_boundary'),
            )

    async def test_load_rejects_empty_translation_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'translation': {
                                'language': '   ',
                            }
                        },
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, 'review.translation.language must not be empty'):
                await ConfigLoader.load(
                    env_path=env_path,
                    settings_path=settings_path,
                    review_root=root,
                    resources_dir=root / 'resources',
                    agents_dir=root / 'agents',
                )

    async def test_load_rejects_non_string_translation_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'translation': {
                                'language': 7,
                            }
                        },
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, 'review.translation.language must be a string'):
                await ConfigLoader.load(
                    env_path=env_path,
                    settings_path=settings_path,
                    review_root=root,
                    resources_dir=root / 'resources',
                    agents_dir=root / 'agents',
                )

    async def test_load_rejects_non_positive_translation_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.yml'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text(
                safe_dump(
                    {
                        'security': {
                            'auth': {
                                'baseUrl': 'http://localhost:1234/v1',
                                'apiKey': 'secret',
                            }
                        },
                        'review': {
                            'translation': {
                                'parallelReviews': 0,
                            }
                        },
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, 'parallelReviews/parallel_reviews must be >= 1'):
                await ConfigLoader.load(
                    env_path=env_path,
                    settings_path=settings_path,
                    review_root=root,
                    resources_dir=root / 'resources',
                    agents_dir=root / 'agents',
                )

    async def test_load_rejects_json_settings_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / '.env'
            settings_path = root / 'settings.json'
            env_path.write_text(
                'GITLAB_TOKEN=test-token\n'
                'GITLAB_API_URL=https://gitlab.example.com/api/v4\n'
            )
            settings_path.write_text('{"review": {}}')

            with self.assertRaisesRegex(ValueError, 'must be a YAML settings file'):
                await ConfigLoader.load(
                    env_path=env_path,
                    settings_path=settings_path,
                    review_root=root,
                    resources_dir=root / 'resources',
                    agents_dir=root / 'agents',
                )


if __name__ == '__main__':
    unittest.main()

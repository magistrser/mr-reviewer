from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from yaml import safe_load

from domain.review.context import ReviewContextLimits
from runtime.async_ops import AsyncPathIO


class ApiSettings(BaseModel):
    review_root: Path = Path('.')
    env_path: Path | None = None
    settings_path: Path | None = None
    resources_dir: Path | None = None
    agents_dir: Path | None = None
    max_concurrent_jobs: int = Field(default=1, ge=1)


class Settings(BaseModel):
    api: ApiSettings = Field(default_factory=ApiSettings)


def get_settings() -> Settings:
    root_dir = Path(__file__).parent
    environment = os.environ.get('ENVIRONMENT')

    match environment:
        case 'DEVELOPMENT' | 'TEST' | None:
            settings_path = root_dir / 'settings.dev.yml'
        case 'PRODUCTION':
            settings_path = root_dir / 'settings.yml'
        case invalid:
            raise ValueError(f'Failed to initialize settings. Invalid ENVIRONMENT variable: {invalid}')

    if not settings_path.exists():
        return Settings()

    with settings_path.open('r') as settings_file:
        raw_settings = safe_load(settings_file) or {}
    return Settings.model_validate(raw_settings)


settings = get_settings()


@dataclass(frozen=True)
class GitLabConfig:
    token: str
    api_url: str


@dataclass(frozen=True)
class AgentConfig:
    base_url: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class RetrievalPolicyConfig:
    enabled: bool | None = None
    allowed_kinds: tuple[str, ...] | None = None
    hint_selection: str | None = None
    min_score: int | None = None
    require_unique_top: bool | None = None
    max_artifacts: int | None = None
    max_chars: int | None = None
    max_lines_per_artifact: int | None = None


@dataclass(frozen=True)
class ReviewConfig:
    review_root: Path
    resources_dir: Path
    agents_dir: Path
    parallel_reviews: int = 1
    parallel_cluster_reviews: int = 1
    parallel_dedup_reviews: int = 1
    translation_language: str = 'ENG'
    parallel_translation_reviews: int = 1
    max_eligible_files: int = 50
    max_findings_total: int = 100
    max_findings_per_file: int = 10
    max_findings_per_cluster: int = 5
    max_cluster_reviews: int = 6
    large_file_bytes: int = 65536
    http_retries: int = 2
    http_backoff_seconds: float = 0.5
    http_timeout_seconds: int = 60
    dedup_line_window: int = 3
    enabled_file_pass_ids: tuple[str, ...] | None = None
    enabled_cluster_pass_ids: tuple[str, ...] | None = None
    context_limits: ReviewContextLimits = field(default_factory=ReviewContextLimits)
    indexing_enabled: bool = True
    max_catalog_file_bytes: int = 131072
    max_retrieved_artifacts_per_task: int = 3
    max_retrieved_chars_per_task: int = 2500
    max_retrieved_lines_per_artifact: int = 80
    file_retrieval_policies: dict[str, RetrievalPolicyConfig] = field(default_factory=dict)
    cluster_retrieval_policies: dict[str, RetrievalPolicyConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    gitlab: GitLabConfig
    agent: AgentConfig
    review: ReviewConfig


class ConfigLoader:
    VALID_HINT_SELECTIONS = frozenset({'exactly_one', 'first_n'})

    @staticmethod
    def _require_mapping(value: Any, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f'{label} must be a YAML object')
        return value

    @staticmethod
    def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in mapping:
                return mapping[key]
        return None

    @classmethod
    def _int_setting(cls, mapping: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
        value = cls._first_present(mapping, *keys)
        return default if value is None else int(value)

    @classmethod
    def _float_setting(cls, mapping: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
        value = cls._first_present(mapping, *keys)
        return default if value is None else float(value)

    @classmethod
    def _positive_int_setting(cls, mapping: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
        value = cls._int_setting(mapping, keys, default)
        if value < 1:
            joined = '/'.join(keys)
            raise ValueError(f'{joined} must be >= 1')
        return value

    @classmethod
    def _string_tuple_setting(
        cls,
        mapping: dict[str, Any],
        keys: tuple[str, ...],
        default: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        value = cls._first_present(mapping, *keys)
        if value is None:
            return default
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            joined = '/'.join(keys)
            raise ValueError(f'{joined} must be a YAML array of strings')
        return tuple(value)

    @classmethod
    def _bool_setting(cls, mapping: dict[str, Any], keys: tuple[str, ...], default: bool) -> bool:
        value = cls._first_present(mapping, *keys)
        return default if value is None else bool(value)

    @classmethod
    def _optional_bool_setting(cls, mapping: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
        value = cls._first_present(mapping, *keys)
        return None if value is None else bool(value)

    @classmethod
    def _translation_language_setting(cls, mapping: dict[str, Any], default: str) -> str:
        value = cls._first_present(mapping, 'language')
        if value is None:
            return default
        if not isinstance(value, str):
            raise ValueError('review.translation.language must be a string')
        normalized = value.strip()
        if not normalized:
            raise ValueError('review.translation.language must not be empty')
        if normalized.upper() == 'ENG':
            return 'ENG'
        return normalized

    @classmethod
    def _optional_positive_int_setting(
        cls,
        mapping: dict[str, Any],
        keys: tuple[str, ...],
    ) -> int | None:
        value = cls._first_present(mapping, *keys)
        if value is None:
            return None
        parsed = int(value)
        if parsed < 1:
            joined = '/'.join(keys)
            raise ValueError(f'{joined} must be >= 1')
        return parsed

    @classmethod
    def _optional_string_tuple_setting(
        cls,
        mapping: dict[str, Any],
        keys: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        value = cls._first_present(mapping, *keys)
        if value is None:
            return None
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            joined = '/'.join(keys)
            raise ValueError(f'{joined} must be a YAML array of strings')
        return tuple(value)

    @classmethod
    def _hint_selection_setting(
        cls,
        mapping: dict[str, Any],
        keys: tuple[str, ...],
    ) -> str | None:
        value = cls._first_present(mapping, *keys)
        if value is None:
            return None
        if not isinstance(value, str):
            joined = '/'.join(keys)
            raise ValueError(f'{joined} must be a string')
        normalized = value.strip()
        if normalized not in cls.VALID_HINT_SELECTIONS:
            joined = '/'.join(keys)
            valid = ', '.join(sorted(cls.VALID_HINT_SELECTIONS))
            raise ValueError(f'{joined} must be one of: {valid}')
        return normalized

    @classmethod
    def _load_retrieval_policies(
        cls,
        mapping: dict[str, Any],
        label: str,
    ) -> dict[str, RetrievalPolicyConfig]:
        policies: dict[str, RetrievalPolicyConfig] = {}
        for name, raw_value in mapping.items():
            if not isinstance(name, str):
                raise ValueError(f'{label} keys must be strings')
            policy_mapping = cls._require_mapping(raw_value, f'{label}.{name}')
            policies[name] = RetrievalPolicyConfig(
                enabled=cls._optional_bool_setting(policy_mapping, ('enabled',)),
                allowed_kinds=cls._optional_string_tuple_setting(
                    policy_mapping,
                    ('allowedKinds', 'allowed_kinds'),
                ),
                hint_selection=cls._hint_selection_setting(
                    policy_mapping,
                    ('hintSelection', 'hint_selection'),
                ),
                min_score=cls._optional_positive_int_setting(
                    policy_mapping,
                    ('minScore', 'min_score'),
                ),
                require_unique_top=cls._optional_bool_setting(
                    policy_mapping,
                    ('requireUniqueTop', 'require_unique_top'),
                ),
                max_artifacts=cls._optional_positive_int_setting(
                    policy_mapping,
                    ('maxArtifacts', 'max_artifacts'),
                ),
                max_chars=cls._optional_positive_int_setting(
                    policy_mapping,
                    ('maxChars', 'max_chars'),
                ),
                max_lines_per_artifact=cls._optional_positive_int_setting(
                    policy_mapping,
                    ('maxLinesPerArtifact', 'max_lines_per_artifact'),
                ),
            )
        return policies

    @classmethod
    def _load_context_limits(cls, settings: dict[str, Any]) -> ReviewContextLimits:
        defaults = ReviewContextLimits()
        review_settings = cls._require_mapping(settings.get('review'), 'review')
        context_settings = cls._require_mapping(review_settings.get('context'), 'review.context')
        section_settings = cls._require_mapping(
            cls._first_present(context_settings, 'sectionChars', 'section_chars'),
            'review.context.sectionChars',
        )

        return ReviewContextLimits(
            scale=cls._float_setting(context_settings, ('scale',), defaults.scale),
            max_context_chars=cls._int_setting(
                context_settings,
                ('maxContextChars', 'max_context_chars'),
                defaults.max_context_chars,
            ),
            max_cluster_context_chars=cls._int_setting(
                context_settings,
                ('maxClusterContextChars', 'max_cluster_context_chars'),
                defaults.max_cluster_context_chars,
            ),
            review_goal_chars=cls._int_setting(
                section_settings,
                ('reviewGoal', 'review_goal'),
                defaults.review_goal_chars,
            ),
            project_profile_chars=cls._int_setting(
                section_settings,
                ('projectProfile', 'project_profile'),
                defaults.project_profile_chars,
            ),
            pr_summary_chars=cls._int_setting(
                section_settings,
                ('prSummary', 'pr_summary'),
                defaults.pr_summary_chars,
            ),
            file_profile_chars=cls._int_setting(
                section_settings,
                ('fileProfile', 'file_profile'),
                defaults.file_profile_chars,
            ),
            excerpt_chars=cls._int_setting(
                section_settings,
                ('excerpt', 'excerptChars', 'excerpt_chars'),
                defaults.excerpt_chars,
            ),
            imports_chars=cls._int_setting(
                section_settings,
                ('imports', 'importsChars', 'imports_chars'),
                defaults.imports_chars,
            ),
            symbols_chars=cls._int_setting(
                section_settings,
                ('symbols', 'symbolsChars', 'symbols_chars'),
                defaults.symbols_chars,
            ),
            review_plan_chars=cls._int_setting(
                section_settings,
                ('reviewPlan', 'review_plan'),
                defaults.review_plan_chars,
            ),
            standards_chars=cls._int_setting(
                section_settings,
                ('standards', 'standardsChars', 'standards_chars'),
                defaults.standards_chars,
            ),
            severity_chars=cls._int_setting(
                section_settings,
                ('severity', 'severityChars', 'severity_chars'),
                defaults.severity_chars,
            ),
            template_chars=cls._int_setting(
                section_settings,
                ('template', 'templateChars', 'template_chars'),
                defaults.template_chars,
            ),
            cluster_intro_chars=cls._int_setting(
                section_settings,
                ('clusterIntro', 'cluster_intro'),
                defaults.cluster_intro_chars,
            ),
            cluster_file_chars=cls._int_setting(
                section_settings,
                ('clusterFile', 'cluster_file'),
                defaults.cluster_file_chars,
            ),
            max_symbol_blocks=cls._int_setting(
                context_settings,
                ('maxSymbolBlocks', 'max_symbol_blocks'),
                defaults.max_symbol_blocks,
            ),
            max_symbol_lines=cls._int_setting(
                context_settings,
                ('maxSymbolLines', 'max_symbol_lines'),
                defaults.max_symbol_lines,
            ),
            cluster_excerpt_radius=cls._int_setting(
                context_settings,
                ('clusterExcerptRadius', 'cluster_excerpt_radius'),
                defaults.cluster_excerpt_radius,
            ),
            cluster_max_segments=cls._int_setting(
                context_settings,
                ('clusterMaxSegments', 'cluster_max_segments'),
                defaults.cluster_max_segments,
            ),
        )

    @staticmethod
    async def read_env_file(path: Path) -> dict[str, str]:
        if not await AsyncPathIO.exists(path):
            return {}
        result: dict[str, str] = {}
        for line in (await AsyncPathIO.read_text(path)).splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result

    @staticmethod
    async def read_settings_file(path: Path) -> dict[str, Any]:
        if path.suffix.lower() not in ('.yml', '.yaml'):
            raise ValueError(f'{path} must be a YAML settings file')
        text = await AsyncPathIO.read_text(path)
        raw_settings = safe_load(text) or {}
        if not isinstance(raw_settings, dict):
            raise ValueError(f'{path} must contain a settings object')
        return raw_settings

    @classmethod
    def _auth_value(cls, auth: dict[str, Any], env: dict[str, str], *names: str) -> str | None:
        for name in names:
            if value := os.environ.get(name):
                return value
            if value := env.get(name):
                return value
        for name in names:
            if name in auth:
                return str(auth[name])
        return None

    @classmethod
    async def load(
        cls,
        env_path: Path,
        settings_path: Path,
        review_root: Path,
        resources_dir: Path,
        agents_dir: Path,
    ) -> Config:
        file_env = await cls.read_env_file(env_path)
        token = os.environ.get('GITLAB_TOKEN') or file_env.get('GITLAB_TOKEN')
        api_url = os.environ.get('GITLAB_API_URL') or file_env.get('GITLAB_API_URL')
        if not token:
            raise ValueError(f'GITLAB_TOKEN not set. Provide via env or {env_path}')
        if not api_url:
            raise ValueError(f'GITLAB_API_URL not set. Provide via env or {env_path}')

        settings = await cls.read_settings_file(settings_path)
        auth = settings['security']['auth']
        agent_api_key = cls._auth_value(auth, file_env, 'OPENAI_API_KEY', 'AGENT_API_KEY', 'apiKey', 'api_key')
        if not agent_api_key:
            raise ValueError('Agent API key not set. Provide OPENAI_API_KEY or AGENT_API_KEY.')
        base_url = cls._auth_value(auth, file_env, 'OPENAI_BASE_URL', 'AGENT_BASE_URL', 'baseUrl', 'base_url')
        if not isinstance(base_url, str) or not base_url:
            raise ValueError('security.auth.baseUrl must be set')
        timeout_ms: int = settings.get('model', {}).get('generationConfig', {}).get('timeout', 300000)
        context_limits = cls._load_context_limits(settings)
        review_settings = cls._require_mapping(settings.get('review'), 'review')
        dedup_settings = cls._require_mapping(review_settings.get('dedup'), 'review.dedup')
        translation_settings = cls._require_mapping(review_settings.get('translation'), 'review.translation')
        indexing_settings = cls._require_mapping(review_settings.get('indexing'), 'review.indexing')
        file_policy_settings = cls._require_mapping(
            cls._first_present(indexing_settings, 'filePolicies', 'file_policies'),
            'review.indexing.filePolicies',
        )
        cluster_policy_settings = cls._require_mapping(
            cls._first_present(indexing_settings, 'clusterPolicies', 'cluster_policies'),
            'review.indexing.clusterPolicies',
        )
        passes_settings = cls._require_mapping(review_settings.get('passes'), 'review.passes')
        file_pass_settings = cls._require_mapping(
            cls._first_present(passes_settings, 'file', 'filePasses', 'file_passes'),
            'review.passes.file',
        )
        cluster_pass_settings = cls._require_mapping(
            cls._first_present(passes_settings, 'cluster', 'clusterPasses', 'cluster_passes'),
            'review.passes.cluster',
        )
        parallel_reviews = cls._positive_int_setting(
            review_settings,
            ('parallelReviews', 'parallel_reviews'),
            ReviewConfig.parallel_reviews,
        )
        parallel_cluster_reviews = cls._positive_int_setting(
            review_settings,
            ('parallelClusterReviews', 'parallel_cluster_reviews'),
            parallel_reviews,
        )

        return Config(
            gitlab=GitLabConfig(token=token, api_url=api_url),
            agent=AgentConfig(
                base_url=base_url.rstrip('/'),
                api_key=agent_api_key,
                timeout_seconds=timeout_ms // 1000,
            ),
            review=ReviewConfig(
                review_root=review_root,
                resources_dir=resources_dir,
                agents_dir=agents_dir,
                parallel_reviews=parallel_reviews,
                parallel_cluster_reviews=parallel_cluster_reviews,
                parallel_dedup_reviews=cls._positive_int_setting(
                    dedup_settings,
                    ('parallelReviews', 'parallel_reviews'),
                    ReviewConfig.parallel_dedup_reviews,
                ),
                translation_language=cls._translation_language_setting(
                    translation_settings,
                    ReviewConfig.translation_language,
                ),
                parallel_translation_reviews=cls._positive_int_setting(
                    translation_settings,
                    ('parallelReviews', 'parallel_reviews'),
                    ReviewConfig.parallel_translation_reviews,
                ),
                dedup_line_window=cls._int_setting(
                    dedup_settings,
                    ('lineWindow', 'line_window'),
                    ReviewConfig.dedup_line_window,
                ),
                http_timeout_seconds=cls._positive_int_setting(
                    review_settings,
                    ('httpTimeoutSeconds', 'http_timeout_seconds'),
                    ReviewConfig.http_timeout_seconds,
                ),
                enabled_file_pass_ids=cls._string_tuple_setting(
                    file_pass_settings,
                    ('enabled', 'enabledPasses', 'enabled_passes'),
                    ReviewConfig.enabled_file_pass_ids,
                ),
                enabled_cluster_pass_ids=cls._string_tuple_setting(
                    cluster_pass_settings,
                    ('enabled', 'enabledPasses', 'enabled_passes'),
                    ReviewConfig.enabled_cluster_pass_ids,
                ),
                context_limits=context_limits,
                indexing_enabled=cls._bool_setting(
                    indexing_settings,
                    ('enabled',),
                    ReviewConfig.indexing_enabled,
                ),
                max_catalog_file_bytes=cls._positive_int_setting(
                    indexing_settings,
                    ('maxCatalogFileBytes', 'max_catalog_file_bytes'),
                    ReviewConfig.max_catalog_file_bytes,
                ),
                max_retrieved_artifacts_per_task=cls._positive_int_setting(
                    indexing_settings,
                    ('maxRetrievedArtifactsPerTask', 'max_retrieved_artifacts_per_task'),
                    ReviewConfig.max_retrieved_artifacts_per_task,
                ),
                max_retrieved_chars_per_task=cls._positive_int_setting(
                    indexing_settings,
                    ('maxRetrievedCharsPerTask', 'max_retrieved_chars_per_task'),
                    ReviewConfig.max_retrieved_chars_per_task,
                ),
                max_retrieved_lines_per_artifact=cls._positive_int_setting(
                    indexing_settings,
                    ('maxRetrievedLinesPerArtifact', 'max_retrieved_lines_per_artifact'),
                    ReviewConfig.max_retrieved_lines_per_artifact,
                ),
                file_retrieval_policies=cls._load_retrieval_policies(
                    file_policy_settings,
                    'review.indexing.filePolicies',
                ),
                cluster_retrieval_policies=cls._load_retrieval_policies(
                    cluster_policy_settings,
                    'review.indexing.clusterPolicies',
                ),
            ),
        )

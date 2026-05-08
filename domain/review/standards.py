from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from domain.models import ChangedFile
from domain.review.planning import ReviewScopePlanner
from runtime.async_ops import AsyncPathIO


@dataclass(frozen=True)
class ReviewPassPlan:
    pass_id: str
    focus_area: str
    goal: str
    checks: tuple[str, ...]
    standard_paths: tuple[str, ...]
    include_current_language_standard: bool = False
    run_always: bool = False
    run_focus_any: tuple[str, ...] = ()
    run_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterPassPlan:
    pass_id: str
    focus_area: str
    goal: str
    checks: tuple[str, ...]
    standard_paths: tuple[str, ...]
    include_language_standards: bool = False


@dataclass(frozen=True)
class ReviewPlan:
    language_standards: dict[str, str]
    file_passes: tuple[ReviewPassPlan, ...]
    cluster_passes: tuple[ClusterPassPlan, ...]


@dataclass(frozen=True)
class Resources:
    severity: str
    template: str
    standard_texts: dict[str, str]
    review_plan: ReviewPlan
    project_profile: str


class ReviewStandards:
    PROJECT_PROFILE_NAME = 'review-profile.md'
    REVIEW_PLAN_NAME = 'review-plan.toml'

    @staticmethod
    def compact_standards(text: str) -> str:
        return re.sub(r'\n{3,}', '\n\n', text.strip())

    @staticmethod
    async def _read(resources_dir: Path, name: str) -> str:
        path = resources_dir / name
        try:
            return ReviewStandards.compact_standards(
                await AsyncPathIO.read_text(path, errors='replace')
            )
        except Exception as exc:
            return f'# ERROR reading {name}: {exc}\n'

    @classmethod
    async def _read_project_profile(cls, review_root: Path | None) -> str:
        if review_root is None:
            return ''
        path = review_root / cls.PROJECT_PROFILE_NAME
        if not await AsyncPathIO.exists(path):
            return ''
        try:
            return cls.compact_standards(await AsyncPathIO.read_text(path, errors='replace'))
        except Exception:
            return ''

    @classmethod
    def _validate_unique_values(cls, values: list[str], label: str) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        if duplicates:
            joined = ', '.join(sorted(duplicates))
            raise ValueError(f'duplicate {label} values in review plan: {joined}')

    @classmethod
    def _validate_review_plan(cls, review_plan: ReviewPlan) -> None:
        cls._validate_unique_values(
            [file_pass.pass_id for file_pass in review_plan.file_passes],
            'file pass_id',
        )
        cls._validate_unique_values(
            [cluster_pass.pass_id for cluster_pass in review_plan.cluster_passes],
            'cluster pass_id',
        )
        cls._validate_unique_values(
            [cluster_pass.focus_area for cluster_pass in review_plan.cluster_passes],
            'cluster focus_area',
        )

    @classmethod
    async def _load_review_plan(cls, resources_dir: Path) -> ReviewPlan:
        raw = await AsyncPathIO.read_text(resources_dir / cls.REVIEW_PLAN_NAME, errors='replace')
        data = tomllib.loads(raw)

        file_passes = tuple(
            ReviewPassPlan(
                pass_id=str(item['pass_id']),
                focus_area=str(item['focus_area']),
                goal=str(item['goal']),
                checks=tuple(str(check) for check in item.get('checks', [])),
                standard_paths=tuple(str(path) for path in item.get('standards', [])),
                include_current_language_standard=bool(item.get('include_current_language_standard', False)),
                run_always=bool(item.get('run_always', False)),
                run_focus_any=tuple(str(value) for value in item.get('run_focus_any', [])),
                run_languages=tuple(str(value) for value in item.get('run_languages', [])),
            )
            for item in data.get('file_passes', [])
        )

        cluster_passes = tuple(
            ClusterPassPlan(
                pass_id=str(item['pass_id']),
                focus_area=str(item['focus_area']),
                goal=str(item['goal']),
                checks=tuple(str(check) for check in item.get('checks', [])),
                standard_paths=tuple(str(path) for path in item.get('standards', [])),
                include_language_standards=bool(item.get('include_language_standards', False)),
            )
            for item in data.get('cluster_passes', [])
        )

        review_plan = ReviewPlan(
            language_standards={
                str(language): str(path)
                for language, path in data.get('language_standards', {}).items()
            },
            file_passes=file_passes,
            cluster_passes=cluster_passes,
        )
        cls._validate_review_plan(review_plan)
        return review_plan

    @classmethod
    def _validate_requested_pass_ids(
        cls,
        available_pass_ids: set[str],
        configured_pass_ids: tuple[str, ...],
        setting_label: str,
    ) -> None:
        unknown = sorted(set(configured_pass_ids) - available_pass_ids)
        if unknown:
            joined = ', '.join(unknown)
            available = ', '.join(sorted(available_pass_ids))
            raise ValueError(
                f'{setting_label} contains unknown pass ids: {joined}. '
                f'Available pass ids: {available}'
            )

    @classmethod
    def _filter_review_plan(
        cls,
        review_plan: ReviewPlan,
        enabled_file_pass_ids: tuple[str, ...] | None,
        enabled_cluster_pass_ids: tuple[str, ...] | None,
    ) -> ReviewPlan:
        file_passes = review_plan.file_passes
        cluster_passes = review_plan.cluster_passes

        if enabled_file_pass_ids is not None:
            cls._validate_requested_pass_ids(
                {file_pass.pass_id for file_pass in review_plan.file_passes},
                enabled_file_pass_ids,
                'review.passes.file.enabled',
            )
            enabled_file_passes = set(enabled_file_pass_ids)
            file_passes = tuple(
                file_pass for file_pass in review_plan.file_passes
                if file_pass.pass_id in enabled_file_passes
            )

        if enabled_cluster_pass_ids is not None:
            cls._validate_requested_pass_ids(
                {cluster_pass.pass_id for cluster_pass in review_plan.cluster_passes},
                enabled_cluster_pass_ids,
                'review.passes.cluster.enabled',
            )
            enabled_cluster_passes = set(enabled_cluster_pass_ids)
            cluster_passes = tuple(
                cluster_pass for cluster_pass in review_plan.cluster_passes
                if cluster_pass.pass_id in enabled_cluster_passes
            )

        return ReviewPlan(
            language_standards=review_plan.language_standards,
            file_passes=file_passes,
            cluster_passes=cluster_passes,
        )

    @classmethod
    def _resource_paths(cls, review_plan: ReviewPlan) -> set[str]:
        paths: set[str] = set()
        language_standard_values = set(review_plan.language_standards.values())

        for file_pass in review_plan.file_passes:
            paths.update(file_pass.standard_paths)
            if not file_pass.include_current_language_standard:
                continue
            if file_pass.run_languages:
                for language in file_pass.run_languages:
                    path = review_plan.language_standards.get(language)
                    if path:
                        paths.add(path)
                continue
            paths.update(language_standard_values)

        for cluster_pass in review_plan.cluster_passes:
            paths.update(cluster_pass.standard_paths)
            if cluster_pass.include_language_standards:
                paths.update(language_standard_values)

        return paths

    @classmethod
    async def load_resources(
        cls,
        resources_dir: Path,
        review_root: Path | None = None,
        enabled_file_pass_ids: tuple[str, ...] | None = None,
        enabled_cluster_pass_ids: tuple[str, ...] | None = None,
    ) -> Resources:
        review_plan = cls._filter_review_plan(
            await cls._load_review_plan(resources_dir),
            enabled_file_pass_ids,
            enabled_cluster_pass_ids,
        )
        return Resources(
            severity=await cls._read(resources_dir, 'severity-levels.md'),
            template=await cls._read(resources_dir, 'templates/inline-comment.md'),
            standard_texts={
                resource_path: await cls._read(resources_dir, resource_path)
                for resource_path in sorted(cls._resource_paths(review_plan))
            },
            review_plan=review_plan,
            project_profile=await cls._read_project_profile(review_root),
        )

    @classmethod
    def _join_texts(cls, texts: list[str]) -> str:
        seen: set[str] = set()
        ordered: list[str] = []
        for text in texts:
            clean = text.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            ordered.append(clean)
        return '\n\n'.join(ordered)

    @classmethod
    def standards_for_paths(
        cls,
        resources: Resources,
        standard_paths: tuple[str, ...],
        languages: set[str] | None = None,
        include_current_language_standard: bool = False,
    ) -> str:
        texts = [resources.standard_texts.get(path, '') for path in standard_paths]
        if include_current_language_standard:
            for language in sorted(languages or set()):
                path = resources.review_plan.language_standards.get(language)
                if path:
                    texts.append(resources.standard_texts.get(path, ''))
        return cls._join_texts(texts)

    @classmethod
    def review_plan_text(cls, goal: str, checks: tuple[str, ...]) -> str:
        lines = [goal.strip()]
        if checks:
            lines.append('')
            lines.append('Checks:')
            lines.extend(f'{index}. {check}' for index, check in enumerate(checks, start=1))
        return '\n'.join(line for line in lines if line is not None).strip()

    @classmethod
    def _should_run_file_pass(
        cls,
        file_pass: ReviewPassPlan,
        language: str | None,
        focus_areas: set[str],
    ) -> bool:
        if file_pass.run_always:
            return True
        if language and language in file_pass.run_languages:
            return True
        if focus_areas.intersection(file_pass.run_focus_any):
            return True
        return False

    @classmethod
    def passes_for_file(cls, changed_file: ChangedFile, resources: Resources) -> list[tuple[str, str, str, str]]:
        analysis = changed_file.get('analysis', {})
        language = analysis.get('language') or ReviewScopePlanner.language_for_file(changed_file)
        focus_areas = set(analysis.get('focus_areas', []))
        passes: list[tuple[str, str, str, str]] = []

        for file_pass in resources.review_plan.file_passes:
            if not cls._should_run_file_pass(file_pass, language, focus_areas):
                continue
            languages = {language} if language else set()
            passes.append((
                file_pass.pass_id,
                file_pass.focus_area,
                cls.standards_for_paths(
                    resources,
                    file_pass.standard_paths,
                    languages,
                    include_current_language_standard=file_pass.include_current_language_standard,
                ),
                cls.review_plan_text(file_pass.goal, file_pass.checks),
            ))
        return passes

    @classmethod
    def cluster_pass_for_focus(
        cls,
        resources: Resources,
        focus_area: str,
    ) -> ClusterPassPlan | None:
        for cluster_pass in resources.review_plan.cluster_passes:
            if cluster_pass.focus_area == focus_area:
                return cluster_pass
        return None

    @classmethod
    def cluster_review_materials(
        cls,
        cluster: dict,
        cluster_files: list[ChangedFile],
        resources: Resources,
    ) -> tuple[str, str, str] | None:
        focus_area = str(cluster.get('focus_area', ''))
        cluster_pass = cls.cluster_pass_for_focus(resources, focus_area)
        if cluster_pass is None:
            return None
        languages = {
            (changed_file.get('analysis') or {}).get('language') or ReviewScopePlanner.language_for_file(changed_file)
            for changed_file in cluster_files
        }
        return (
            cluster_pass.pass_id,
            cls.review_plan_text(cluster_pass.goal, cluster_pass.checks),
            cls.standards_for_paths(
                resources,
                cluster_pass.standard_paths,
                languages,
                include_current_language_standard=cluster_pass.include_language_standards,
            ),
        )

    @classmethod
    def agent_input_payload(
        cls,
        changed_file: ChangedFile,
        ordinal: int,
        findings_path: Path,
        context_path: Path,
        existing: list[dict],
        pass_id: str,
        focus_area: str,
        max_findings_per_file: int = 10,
    ) -> dict:
        analysis = changed_file.get('analysis', {})
        language = analysis.get('language') or ReviewScopePlanner.language_for_file(changed_file)
        return {
            'findings_path': str(findings_path),
            'file': {
                'old_path': changed_file['old_path'],
                'new_path': changed_file['new_path'],
                'language': language,
                'is_new': changed_file.get('is_new', False),
                'is_renamed': changed_file.get('is_renamed', False),
                'hunks': changed_file.get('hunks', []),
                'analysis': analysis,
            },
            'review': {
                'ordinal': ordinal,
                'pass_id': pass_id,
                'focus_area': focus_area,
                'source_kind': 'file',
            },
            'existing_findings_for_file': existing,
            'context_path': str(context_path),
            'max_findings_per_file': max_findings_per_file,
        }

    @classmethod
    def cluster_input_payload(
        cls,
        cluster: dict,
        cluster_files: list[ChangedFile],
        findings_path: Path,
        context_path: Path,
        existing: list[dict],
        max_findings_total: int,
        pass_id: str,
        focus_area: str,
    ) -> dict:
        return {
            'findings_path': str(findings_path),
            'cluster': cluster,
            'files': [
                {
                    'old_path': changed_file['old_path'],
                    'new_path': changed_file['new_path'],
                    'language': (
                        (changed_file.get('analysis') or {}).get('language')
                        or ReviewScopePlanner.language_for_file(changed_file)
                    ),
                    'is_new': changed_file.get('is_new', False),
                    'is_renamed': changed_file.get('is_renamed', False),
                    'hunks': changed_file.get('hunks', []),
                    'analysis': changed_file.get('analysis', {}),
                }
                for changed_file in cluster_files
            ],
            'review': {
                'pass_id': pass_id,
                'focus_area': focus_area,
                'source_kind': 'cluster',
            },
            'existing_findings_for_cluster': existing,
            'context_path': str(context_path),
            'max_findings_total': max_findings_total,
        }


compact_standards = ReviewStandards.compact_standards
load_resources = ReviewStandards.load_resources
agent_input_payload = ReviewStandards.agent_input_payload

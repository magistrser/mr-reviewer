from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from domain.models import ChangedFile, ReviewCluster
from domain.review.context import ReviewContextBuilder
from domain.review.planning import ReviewScopePlanner
from runtime.async_ops import AsyncCommandRunner, AsyncPathIO


class RepoCatalogEntry(TypedDict, total=False):
    path: str
    language: str
    directory: str
    scope: str
    path_tokens: list[str]
    focus_areas: list[str]
    import_lines: list[str]
    import_targets: list[str]
    symbol_names: list[str]
    role_hints: list[str]
    api_terms: list[str]
    schema_terms: list[str]
    auth_terms: list[str]
    storage_terms: list[str]
    async_markers: list[str]
    retrieval_hints: list[dict[str, str]]
    path_aliases: list[str]
    symbol_spans: list['SymbolSpan']


class RepoCatalogLookups(TypedDict, total=False):
    symbols: dict[str, list[str]]
    import_targets: dict[str, list[str]]
    role_hints: dict[str, list[str]]
    terms: dict[str, list[str]]
    scopes: dict[str, list[str]]


class RepoCatalog(TypedDict, total=False):
    enabled: bool
    indexed_files: int
    skipped_large_files: int
    skipped_binary_files: int
    files: dict[str, RepoCatalogEntry]
    lookups: RepoCatalogLookups


class RetrievalRequest(TypedDict, total=False):
    kind: str
    query: str
    reason: str
    source_path: str
    focus_area: str


class SymbolSpan(TypedDict):
    name: str
    line_start: int
    line_end: int


class RetrievedArtifact(TypedDict, total=False):
    kind: str
    query: str
    path: str
    reason: str
    snippet: str
    language: str
    line_start: int
    line_end: int
    score: int
    unit: str
    symbol_name: str


@dataclass(frozen=True)
class RetrievalPolicyOverride:
    enabled: bool | None = None
    allowed_kinds: tuple[str, ...] | None = None
    hint_selection: str | None = None
    min_score: int | None = None
    require_unique_top: bool | None = None
    max_artifacts: int | None = None
    max_chars: int | None = None
    max_lines_per_artifact: int | None = None


@dataclass(frozen=True)
class ResolvedRetrievalPolicy:
    enabled: bool
    allowed_kinds: tuple[str, ...]
    hint_selection: str
    min_score: int
    require_unique_top: bool
    max_artifacts: int
    max_chars: int
    max_lines_per_artifact: int

    def as_payload(self) -> dict[str, object]:
        return {
            'enabled': self.enabled,
            'allowed_kinds': list(self.allowed_kinds),
            'hint_selection': self.hint_selection,
            'min_score': self.min_score,
            'require_unique_top': self.require_unique_top,
            'max_artifacts': self.max_artifacts,
            'max_chars': self.max_chars,
            'max_lines_per_artifact': self.max_lines_per_artifact,
        }


class RepoCatalogBuilder:
    @classmethod
    async def build(
        cls,
        repo_dir: Path,
        *,
        enabled: bool = True,
        max_file_bytes: int = 131072,
    ) -> RepoCatalog:
        if not enabled:
            return cls.empty_catalog(enabled=False)

        files: dict[str, RepoCatalogEntry] = {}
        symbol_lookup: dict[str, list[str]] = defaultdict(list)
        import_lookup: dict[str, list[str]] = defaultdict(list)
        role_lookup: dict[str, list[str]] = defaultdict(list)
        term_lookup: dict[str, list[str]] = defaultdict(list)
        scope_lookup: dict[str, list[str]] = defaultdict(list)
        skipped_large = 0
        skipped_binary = 0

        for relative_path in await cls._tracked_files(repo_dir):
            if not cls._is_source_like(relative_path):
                continue
            file_path = repo_dir / relative_path
            try:
                stat_result = await AsyncPathIO.stat(file_path)
            except Exception:
                continue
            if stat_result.st_size > max_file_bytes:
                skipped_large += 1
                continue
            try:
                source_bytes = await AsyncPathIO.read_bytes(file_path)
            except Exception:
                continue
            if b'\x00' in source_bytes:
                skipped_binary += 1
                continue
            source_text = source_bytes.decode(errors='replace')
            changed_file: ChangedFile = {
                'old_path': relative_path,
                'new_path': relative_path,
            }
            analysis = ReviewScopePlanner.build_file_analysis(changed_file, source_text)
            source_lines = source_text.splitlines()
            entry: RepoCatalogEntry = {
                'path': relative_path,
                'language': analysis.get('language', 'text'),
                'directory': analysis.get('directory', '.'),
                'scope': ReviewScopePlanner.cluster_scope_for_directory(
                    analysis.get('directory', '.'),
                ),
                'path_tokens': list(analysis.get('path_tokens', [])),
                'focus_areas': list(analysis.get('focus_areas', [])),
                'import_lines': list(analysis.get('import_lines', [])),
                'import_targets': list(analysis.get('import_targets', [])),
                'symbol_names': list(analysis.get('symbol_names', [])),
                'role_hints': list(analysis.get('role_hints', [])),
                'api_terms': list(analysis.get('api_terms', [])),
                'schema_terms': list(analysis.get('schema_terms', [])),
                'auth_terms': list(analysis.get('auth_terms', [])),
                'storage_terms': list(analysis.get('storage_terms', [])),
                'async_markers': list(analysis.get('async_markers', [])),
                'retrieval_hints': list(analysis.get('retrieval_hints', [])),
                'path_aliases': cls._path_aliases(relative_path),
                'symbol_spans': cls._symbol_spans(source_lines, analysis.get('language', 'text')),
            }
            files[relative_path] = entry

            for symbol_name in entry.get('symbol_names', []):
                symbol_lookup[symbol_name].append(relative_path)
            for import_target in entry.get('import_targets', []):
                import_lookup[import_target].append(relative_path)
            for role_hint in entry.get('role_hints', []):
                role_lookup[role_hint].append(relative_path)
            for term in cls._entry_terms(entry):
                term_lookup[term].append(relative_path)
            scope_lookup[entry.get('scope', '.')].append(relative_path)

        return {
            'enabled': True,
            'indexed_files': len(files),
            'skipped_large_files': skipped_large,
            'skipped_binary_files': skipped_binary,
            'files': cls._sorted_mapping(files),
            'lookups': {
                'symbols': cls._sorted_lookup(symbol_lookup),
                'import_targets': cls._sorted_lookup(import_lookup),
                'role_hints': cls._sorted_lookup(role_lookup),
                'terms': cls._sorted_lookup(term_lookup),
                'scopes': cls._sorted_lookup(scope_lookup),
            },
        }

    @classmethod
    def empty_catalog(cls, *, enabled: bool) -> RepoCatalog:
        return {
            'enabled': enabled,
            'indexed_files': 0,
            'skipped_large_files': 0,
            'skipped_binary_files': 0,
            'files': {},
            'lookups': {
                'symbols': {},
                'import_targets': {},
                'role_hints': {},
                'terms': {},
                'scopes': {},
            },
        }

    @classmethod
    async def _tracked_files(cls, repo_dir: Path) -> list[str]:
        try:
            result = await AsyncCommandRunner.run_checked(['git', 'ls-files'], cwd=repo_dir)
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            results: list[str] = []
            for path in sorted(repo_dir.rglob('*')):
                if not path.is_file() or '.git' in path.parts:
                    continue
                results.append(str(path.relative_to(repo_dir)))
            return results

    @classmethod
    def _is_source_like(cls, path: str) -> bool:
        suffix = Path(path).suffix.lstrip('.').lower()
        return suffix in ReviewScopePlanner.KNOWN_SOURCE_EXTENSIONS

    @classmethod
    def _path_aliases(cls, path: str) -> list[str]:
        clean = path.replace('\\', '/')
        without_ext = clean.rsplit('.', 1)[0]
        parts = [part for part in without_ext.split('/') if part]
        aliases = {without_ext, parts[-1] if parts else without_ext}
        if parts:
            aliases.add('/'.join(parts[-2:]))
            aliases.add('.'.join(parts))
            aliases.add('.'.join(parts[-2:]))
            aliases.add('::'.join(parts))
            aliases.add('::'.join(parts[-2:]))
        return sorted(alias for alias in aliases if alias)

    @classmethod
    def _entry_terms(cls, entry: RepoCatalogEntry) -> list[str]:
        values = (
            list(entry.get('api_terms', []))
            + list(entry.get('schema_terms', []))
            + list(entry.get('auth_terms', []))
            + list(entry.get('storage_terms', []))
            + list(entry.get('async_markers', []))
        )
        return sorted(dict.fromkeys(values))

    @classmethod
    def _symbol_spans(cls, source_lines: list[str], language: str) -> list[SymbolSpan]:
        spans: list[SymbolSpan] = []
        for symbol_name, start, end in ReviewContextBuilder._symbol_ranges(source_lines, language):
            spans.append({
                'name': symbol_name,
                'line_start': start + 1,
                'line_end': end + 1,
            })
        return spans

    @classmethod
    def _sorted_mapping(cls, mapping: dict[str, RepoCatalogEntry]) -> dict[str, RepoCatalogEntry]:
        return {key: mapping[key] for key in sorted(mapping)}

    @classmethod
    def _sorted_lookup(cls, lookup: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            key: sorted(dict.fromkeys(values))
            for key, values in sorted(lookup.items())
        }


class ReviewRetrievalPlanner:
    DEFAULT_MAX_ARTIFACTS = 3
    DEFAULT_MAX_CHARS = 2500
    DEFAULT_MAX_LINES_PER_ARTIFACT = 80
    SYMBOL_CONTEXT_RADIUS = 3
    SUPPORTED_KINDS = frozenset({
        'symbol_definition',
        'import_owner',
        'schema_partner',
        'service_partner',
        'async_partner',
    })
    SYMBOL_RETRIEVAL_KINDS = frozenset({
        'symbol_definition',
        'schema_partner',
        'service_partner',
        'async_partner',
    })
    FILE_POLICY_OVERRIDES: dict[str, RetrievalPolicyOverride] = {
        'security': RetrievalPolicyOverride(
            enabled=True,
            allowed_kinds=('import_owner', 'service_partner', 'symbol_definition'),
            hint_selection='exactly_one',
            min_score=85,
            require_unique_top=True,
            max_artifacts=1,
        ),
        'async': RetrievalPolicyOverride(
            enabled=True,
            allowed_kinds=('async_partner', 'import_owner', 'service_partner'),
            hint_selection='exactly_one',
            min_score=85,
            require_unique_top=True,
            max_artifacts=1,
        ),
    }
    CLUSTER_POLICY_OVERRIDES: dict[str, RetrievalPolicyOverride] = {
        'security': RetrievalPolicyOverride(enabled=True),
        'api_boundary': RetrievalPolicyOverride(enabled=True),
        'data_integrity': RetrievalPolicyOverride(enabled=True),
        'async_concurrency': RetrievalPolicyOverride(enabled=True),
        'architecture': RetrievalPolicyOverride(enabled=True),
    }

    @classmethod
    def build_cluster_evidence(cls, cluster_files: list[ChangedFile]) -> dict[str, object]:
        common_scope = ReviewScopePlanner.cluster_scope_for_directory(
            str((cluster_files[0].get('analysis') or {}).get('directory', '.'))
        ) if cluster_files else '.'
        evidence: dict[str, object] = {'common_scope': common_scope}
        shared_symbols = cls._shared_values(cluster_files, 'symbol_names')
        shared_import_targets = cls._shared_values(cluster_files, 'import_targets')
        shared_role_hints = cls._shared_values(cluster_files, 'role_hints')
        shared_terms = {
            'api_terms': cls._shared_values(cluster_files, 'api_terms'),
            'schema_terms': cls._shared_values(cluster_files, 'schema_terms'),
            'auth_terms': cls._shared_values(cluster_files, 'auth_terms'),
            'storage_terms': cls._shared_values(cluster_files, 'storage_terms'),
            'async_markers': cls._shared_values(cluster_files, 'async_markers'),
        }
        if shared_symbols:
            evidence['shared_symbols'] = shared_symbols[:6]
        if shared_import_targets:
            evidence['shared_import_targets'] = shared_import_targets[:6]
        if shared_role_hints:
            evidence['shared_role_hints'] = shared_role_hints[:6]
        compact_terms = {
            key: values[:6]
            for key, values in shared_terms.items()
            if values
        }
        if compact_terms:
            evidence['shared_terms'] = compact_terms
        return evidence

    @classmethod
    def build_cluster_retrieval_requests(
        cls,
        cluster: ReviewCluster,
        cluster_files: list[ChangedFile],
    ) -> list[RetrievalRequest]:
        evidence = cls.build_cluster_evidence(cluster_files)
        requests: list[RetrievalRequest] = []
        focus_area = str(cluster.get('focus_area', 'cluster'))
        files = list(cluster.get('files', []))
        source_path = files[0] if files else ''

        shared_symbols = list(evidence.get('shared_symbols', []))
        shared_import_targets = list(evidence.get('shared_import_targets', []))
        shared_role_hints = list(evidence.get('shared_role_hints', []))
        shared_terms = evidence.get('shared_terms', {})
        if not isinstance(shared_terms, dict):
            shared_terms = {}

        if shared_symbols:
            requests.append({
                'kind': 'symbol_definition',
                'query': str(shared_symbols[0]),
                'reason': f'Look up the shared symbol `{shared_symbols[0]}` outside the MR cluster.',
                'source_path': source_path,
                'focus_area': focus_area,
            })
        if shared_import_targets:
            requests.append({
                'kind': 'import_owner',
                'query': str(shared_import_targets[0]),
                'reason': f'Resolve the shared import target `{shared_import_targets[0]}` to its owning file.',
                'source_path': source_path,
                'focus_area': focus_area,
            })
        if focus_area in {'api_boundary', 'data_integrity'}:
            schema_terms = shared_terms.get('schema_terms', [])
            if schema_terms:
                requests.append({
                    'kind': 'schema_partner',
                    'query': str(schema_terms[0]),
                    'reason': f'Inspect related schema context for `{schema_terms[0]}`.',
                    'source_path': source_path,
                    'focus_area': focus_area,
                })
        if focus_area in {'security', 'architecture'} and shared_role_hints:
            requests.append({
                'kind': 'service_partner',
                'query': str(shared_role_hints[0]),
                'reason': f'Inspect the neighboring service or boundary role `{shared_role_hints[0]}`.',
                'source_path': source_path,
                'focus_area': focus_area,
            })
        if focus_area == 'async_concurrency':
            async_terms = shared_terms.get('async_markers', [])
            if async_terms:
                requests.append({
                    'kind': 'async_partner',
                    'query': str(async_terms[0]),
                    'reason': f'Inspect the async partner for marker `{async_terms[0]}`.',
                    'source_path': source_path,
                    'focus_area': focus_area,
                })

        deduped: list[RetrievalRequest] = []
        seen: set[tuple[str, str]] = set()
        for request in requests:
            key = (request['kind'], request['query'])
            if key in seen or request['kind'] not in cls.SUPPORTED_KINDS:
                continue
            seen.add(key)
            deduped.append(request)
        return deduped

    @classmethod
    def resolve_file_policy(
        cls,
        *,
        pass_id: str,
        max_artifacts: int,
        max_chars: int,
        max_lines_per_artifact: int,
        policy_override: object | None = None,
    ) -> ResolvedRetrievalPolicy:
        policy = cls._base_policy(scope='file')
        policy = cls._apply_override(
            policy,
            RetrievalPolicyOverride(
                max_artifacts=max_artifacts,
                max_chars=max_chars,
                max_lines_per_artifact=max_lines_per_artifact,
            ),
        )
        policy = cls._apply_override(policy, cls.FILE_POLICY_OVERRIDES.get(pass_id))
        policy = cls._apply_override(policy, cls._override_from_object(policy_override))
        return policy

    @classmethod
    def resolve_cluster_policy(
        cls,
        *,
        focus_area: str,
        max_artifacts: int,
        max_chars: int,
        max_lines_per_artifact: int,
        policy_override: object | None = None,
    ) -> ResolvedRetrievalPolicy:
        policy = cls._base_policy(scope='cluster')
        policy = cls._apply_override(
            policy,
            RetrievalPolicyOverride(
                max_artifacts=max_artifacts,
                max_chars=max_chars,
                max_lines_per_artifact=max_lines_per_artifact,
            ),
        )
        policy = cls._apply_override(policy, cls.CLUSTER_POLICY_OVERRIDES.get(focus_area))
        policy = cls._apply_override(policy, cls._override_from_object(policy_override))
        return policy

    @classmethod
    async def plan_file_task(
        cls,
        *,
        changed_file: ChangedFile,
        pass_id: str,
        focus_area: str,
        catalog: RepoCatalog,
        repo_dir: Path,
        policy: ResolvedRetrievalPolicy,
    ) -> dict[str, object]:
        if not policy.enabled:
            return cls._plan_result(
                stage='file',
                requests=[],
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy='retrieval disabled by applied policy',
            )

        analysis = changed_file.get('analysis', {})
        requests = cls._filter_requests_by_policy(
            cls._coerce_requests(analysis.get('retrieval_hints', []), focus_area),
            policy,
        )
        if not requests:
            return cls._plan_result(
                stage='file',
                requests=[],
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy='no file retrieval requests matched the applied policy',
            )

        selected_requests, policy_message = cls._select_requests(requests, policy)
        if policy_message is not None:
            return cls._plan_result(
                stage='file',
                requests=requests,
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy=policy_message,
            )

        artifacts, empty = await cls._resolve_requests(
            selected_requests,
            catalog=catalog,
            repo_dir=repo_dir,
            source_paths={changed_file.get('new_path', '')},
            source_analysis=[analysis],
            policy=policy,
        )
        return cls._plan_result(
            stage='file',
            requests=selected_requests,
            artifacts=artifacts,
            applied_policy=policy,
            empty_retrievals=empty,
        )

    @classmethod
    async def plan_cluster_task(
        cls,
        *,
        cluster: ReviewCluster,
        cluster_files: list[ChangedFile],
        catalog: RepoCatalog,
        repo_dir: Path,
        policy: ResolvedRetrievalPolicy,
    ) -> dict[str, object]:
        if not policy.enabled:
            return cls._plan_result(
                stage='cluster',
                requests=[],
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy='retrieval disabled by applied policy',
            )

        requests = cls._filter_requests_by_policy(
            cls._coerce_requests(cluster.get('retrieval_requests', []), str(cluster.get('focus_area', 'cluster'))),
            policy,
        )
        if not requests:
            return cls._plan_result(
                stage='cluster',
                requests=[],
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy='no cluster retrieval requests matched the applied policy',
            )

        selected_requests, policy_message = cls._select_requests(requests, policy)
        if policy_message is not None:
            return cls._plan_result(
                stage='cluster',
                requests=requests,
                artifacts=[],
                applied_policy=policy,
                skipped_by_policy=1,
                policy=policy_message,
            )

        artifacts, empty = await cls._resolve_requests(
            selected_requests,
            catalog=catalog,
            repo_dir=repo_dir,
            source_paths={changed_file.get('new_path', '') for changed_file in cluster_files},
            source_analysis=[changed_file.get('analysis', {}) for changed_file in cluster_files],
            policy=policy,
        )
        return cls._plan_result(
            stage='cluster',
            requests=selected_requests,
            artifacts=artifacts,
            applied_policy=policy,
            empty_retrievals=empty,
        )

    @classmethod
    def _coerce_requests(
        cls,
        raw_requests: list[dict[str, str]],
        focus_area: str,
    ) -> list[RetrievalRequest]:
        requests: list[RetrievalRequest] = []
        for raw in raw_requests:
            kind = str(raw.get('kind', ''))
            query = str(raw.get('query', '')).strip()
            if kind not in cls.SUPPORTED_KINDS or not query:
                continue
            requests.append({
                'kind': kind,
                'query': query,
                'reason': str(raw.get('reason', '')).strip() or f'Inspect `{query}`.',
                'source_path': str(raw.get('source_path', '')).strip(),
                'focus_area': focus_area,
            })
        return requests

    @classmethod
    def _filter_requests_by_policy(
        cls,
        requests: list[RetrievalRequest],
        policy: ResolvedRetrievalPolicy,
    ) -> list[RetrievalRequest]:
        allowed = set(policy.allowed_kinds)
        return [
            request for request in requests
            if request.get('kind') in allowed
        ]

    @classmethod
    def _select_requests(
        cls,
        requests: list[RetrievalRequest],
        policy: ResolvedRetrievalPolicy,
    ) -> tuple[list[RetrievalRequest], str | None]:
        if policy.hint_selection == 'exactly_one' and len(requests) != 1:
            return requests, 'retrieval requires exactly one request after policy filtering'
        return requests, None

    @classmethod
    async def _resolve_requests(
        cls,
        requests: list[RetrievalRequest],
        *,
        catalog: RepoCatalog,
        repo_dir: Path,
        source_paths: set[str],
        source_analysis: list[dict],
        policy: ResolvedRetrievalPolicy,
    ) -> tuple[list[RetrievedArtifact], int]:
        artifacts: list[RetrievedArtifact] = []
        empty_retrievals = 0
        used_chars = 0

        files = catalog.get('files', {})
        for request in requests:
            if (
                len(artifacts) >= policy.max_artifacts
                or used_chars >= policy.max_chars
                or not files
            ):
                break
            candidate = cls._rank_candidates(
                request,
                files,
                source_paths=source_paths,
                source_analysis=source_analysis,
                min_score=policy.min_score,
                require_unique_top=policy.require_unique_top,
            )
            if candidate is None:
                empty_retrievals += 1
                continue
            artifact = await cls._build_artifact(
                request,
                candidate,
                repo_dir=repo_dir,
                max_lines=policy.max_lines_per_artifact,
            )
            if artifact is None:
                empty_retrievals += 1
                continue
            remaining = policy.max_chars - used_chars
            artifact = cls._truncate_artifact(artifact, remaining)
            if not artifact.get('snippet'):
                empty_retrievals += 1
                continue
            artifacts.append(artifact)
            used_chars += len(artifact['snippet'])
        return artifacts, empty_retrievals

    @classmethod
    def _rank_candidates(
        cls,
        request: RetrievalRequest,
        files: dict[str, RepoCatalogEntry],
        *,
        source_paths: set[str],
        source_analysis: list[dict],
        min_score: int,
        require_unique_top: bool,
    ) -> RepoCatalogEntry | None:
        ranked: list[tuple[int, str, RepoCatalogEntry]] = []
        for path, entry in files.items():
            if path in source_paths:
                continue
            score = cls._score_candidate(request, entry, source_analysis)
            if score < min_score:
                continue
            ranked.append((score, path, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return None
        if require_unique_top and len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        best_score, _, best_entry = ranked[0]
        entry = dict(best_entry)
        entry['score'] = best_score  # type: ignore[typeddict-item]
        return entry

    @classmethod
    def _score_candidate(
        cls,
        request: RetrievalRequest,
        entry: RepoCatalogEntry,
        source_analysis: list[dict],
    ) -> int:
        normalized_query = request['query'].strip().lower()
        source_scopes = {
            ReviewScopePlanner.cluster_scope_for_directory(str(analysis.get('directory', '.')))
            for analysis in source_analysis
        }
        source_roles = {
            role
            for analysis in source_analysis
            for role in analysis.get('role_hints', [])
        }
        source_languages = {
            str(analysis.get('language', ''))
            for analysis in source_analysis
            if analysis.get('language')
        }
        source_tokens = {
            token
            for analysis in source_analysis
            for token in analysis.get('path_tokens', [])
        }

        score = 0
        if cls._exact_match(request, normalized_query, entry):
            score += 60
        if entry.get('scope') in source_scopes:
            score += 20
        if source_roles.intersection(entry.get('role_hints', [])):
            score += 15
        if entry.get('language') in source_languages:
            score += 10
        score += min(
            10,
            len(source_tokens.intersection(entry.get('path_tokens', []))) * 2,
        )
        return score

    @classmethod
    def _exact_match(
        cls,
        request: RetrievalRequest,
        normalized_query: str,
        entry: RepoCatalogEntry,
    ) -> bool:
        symbol_names = {value.lower() for value in entry.get('symbol_names', [])}
        import_targets = {value.lower() for value in entry.get('import_targets', [])}
        path_aliases = {value.lower() for value in entry.get('path_aliases', [])}
        role_hints = {value.lower() for value in entry.get('role_hints', [])}
        schema_terms = {value.lower() for value in entry.get('schema_terms', [])}
        async_markers = {value.lower() for value in entry.get('async_markers', [])}
        if request['kind'] == 'symbol_definition':
            return normalized_query in symbol_names
        if request['kind'] == 'import_owner':
            return (
                normalized_query in import_targets
                or normalized_query in path_aliases
                or any(alias.endswith(normalized_query) for alias in path_aliases)
            )
        if request['kind'] == 'schema_partner':
            return normalized_query in symbol_names or normalized_query in schema_terms
        if request['kind'] == 'service_partner':
            return normalized_query in symbol_names or normalized_query in role_hints
        if request['kind'] == 'async_partner':
            return normalized_query in symbol_names or normalized_query in async_markers
        return False

    @classmethod
    async def _build_artifact(
        cls,
        request: RetrievalRequest,
        entry: RepoCatalogEntry,
        *,
        repo_dir: Path,
        max_lines: int,
    ) -> RetrievedArtifact | None:
        source_path = repo_dir / entry['path']
        try:
            source_text = await AsyncPathIO.read_text(source_path, errors='replace')
        except Exception:
            return None
        source_lines = source_text.splitlines()
        if not source_lines:
            return None

        unit = 'file'
        symbol_name = ''
        symbol_span = cls._matching_symbol_span(request, entry)
        if symbol_span is not None:
            line_start, line_end = cls._symbol_snippet_bounds(
                symbol_span,
                len(source_lines),
                max_lines=max_lines,
            )
            unit = 'symbol'
            symbol_name = symbol_span['name']
        else:
            line_start, line_end = cls._snippet_bounds(
                request,
                entry,
                source_lines,
                max_lines=max_lines,
            )
        snippet = cls._render_snippet(source_lines, line_start, line_end)
        if not snippet:
            return None
        artifact: RetrievedArtifact = {
            'kind': request['kind'],
            'query': request['query'],
            'path': entry['path'],
            'reason': request['reason'],
            'snippet': snippet,
            'language': entry.get('language', 'text'),
            'line_start': line_start,
            'line_end': line_end,
            'score': int(entry.get('score', 0)),  # type: ignore[arg-type]
            'unit': unit,
        }
        if symbol_name:
            artifact['symbol_name'] = symbol_name
        return artifact

    @classmethod
    def _snippet_bounds(
        cls,
        request: RetrievalRequest,
        entry: RepoCatalogEntry,
        source_lines: list[str],
        *,
        max_lines: int,
    ) -> tuple[int, int]:
        query = request['query'].strip().lower()
        language = entry.get('language', 'text')
        if request['kind'] == 'symbol_definition':
            for symbol_name, start, end in ReviewContextBuilder._symbol_ranges(source_lines, language):
                if symbol_name.lower() == query:
                    return start + 1, min(end + 1, start + max_lines)
        for line_number, line in enumerate(source_lines, start=1):
            lowered = line.lower()
            if query in lowered:
                start = max(1, line_number - 10)
                end = min(len(source_lines), start + max_lines - 1)
                return start, end
        return 1, min(len(source_lines), max_lines)

    @classmethod
    def _matching_symbol_span(
        cls,
        request: RetrievalRequest,
        entry: RepoCatalogEntry,
    ) -> SymbolSpan | None:
        if request['kind'] not in cls.SYMBOL_RETRIEVAL_KINDS:
            return None
        query = request['query'].strip().lower()
        for span in entry.get('symbol_spans', []):
            if str(span.get('name', '')).strip().lower() == query:
                return span
        return None

    @classmethod
    def _symbol_snippet_bounds(
        cls,
        symbol_span: SymbolSpan,
        total_lines: int,
        *,
        max_lines: int,
    ) -> tuple[int, int]:
        desired_start = max(1, int(symbol_span['line_start']) - cls.SYMBOL_CONTEXT_RADIUS)
        desired_end = min(total_lines, int(symbol_span['line_end']) + cls.SYMBOL_CONTEXT_RADIUS)
        if desired_end - desired_start + 1 <= max_lines:
            return desired_start, desired_end
        return desired_start, min(total_lines, desired_start + max_lines - 1)

    @classmethod
    def _render_snippet(cls, source_lines: list[str], line_start: int, line_end: int) -> str:
        return '\n'.join(
            f'{line_number}: {source_lines[line_number - 1]}'
            for line_number in range(line_start, line_end + 1)
        )

    @classmethod
    def _truncate_artifact(
        cls,
        artifact: RetrievedArtifact,
        remaining_chars: int,
    ) -> RetrievedArtifact:
        if remaining_chars <= 0:
            artifact['snippet'] = ''
            return artifact
        snippet = artifact.get('snippet', '')
        if len(snippet) <= remaining_chars:
            return artifact
        artifact['snippet'] = snippet[: max(0, remaining_chars - 13)].rstrip() + '\n[truncated]'
        return artifact

    @classmethod
    def _shared_values(cls, cluster_files: list[ChangedFile], key: str) -> list[str]:
        counts: dict[str, int] = defaultdict(int)
        for changed_file in cluster_files:
            analysis = changed_file.get('analysis', {})
            for value in set(analysis.get(key, [])):
                counts[str(value)] += 1
        return sorted(value for value, count in counts.items() if count >= 2)

    @classmethod
    def _base_policy(cls, *, scope: str) -> ResolvedRetrievalPolicy:
        if scope == 'cluster':
            return ResolvedRetrievalPolicy(
                enabled=False,
                allowed_kinds=tuple(sorted(cls.SUPPORTED_KINDS)),
                hint_selection='first_n',
                min_score=55,
                require_unique_top=False,
                max_artifacts=cls.DEFAULT_MAX_ARTIFACTS,
                max_chars=cls.DEFAULT_MAX_CHARS,
                max_lines_per_artifact=cls.DEFAULT_MAX_LINES_PER_ARTIFACT,
            )
        return ResolvedRetrievalPolicy(
            enabled=False,
            allowed_kinds=tuple(sorted(cls.SUPPORTED_KINDS)),
            hint_selection='exactly_one',
            min_score=85,
            require_unique_top=True,
            max_artifacts=cls.DEFAULT_MAX_ARTIFACTS,
            max_chars=cls.DEFAULT_MAX_CHARS,
            max_lines_per_artifact=cls.DEFAULT_MAX_LINES_PER_ARTIFACT,
        )

    @classmethod
    def _override_from_object(
        cls,
        raw_override: object | None,
    ) -> RetrievalPolicyOverride | None:
        if raw_override is None:
            return None
        if isinstance(raw_override, dict):
            allowed_kinds = raw_override.get('allowed_kinds')
            return RetrievalPolicyOverride(
                enabled=raw_override.get('enabled'),
                allowed_kinds=tuple(allowed_kinds) if allowed_kinds is not None else None,
                hint_selection=raw_override.get('hint_selection'),
                min_score=raw_override.get('min_score'),
                require_unique_top=raw_override.get('require_unique_top'),
                max_artifacts=raw_override.get('max_artifacts'),
                max_chars=raw_override.get('max_chars'),
                max_lines_per_artifact=raw_override.get('max_lines_per_artifact'),
            )
        allowed_kinds = getattr(raw_override, 'allowed_kinds', None)
        return RetrievalPolicyOverride(
            enabled=getattr(raw_override, 'enabled', None),
            allowed_kinds=tuple(allowed_kinds) if allowed_kinds is not None else None,
            hint_selection=getattr(raw_override, 'hint_selection', None),
            min_score=getattr(raw_override, 'min_score', None),
            require_unique_top=getattr(raw_override, 'require_unique_top', None),
            max_artifacts=getattr(raw_override, 'max_artifacts', None),
            max_chars=getattr(raw_override, 'max_chars', None),
            max_lines_per_artifact=getattr(raw_override, 'max_lines_per_artifact', None),
        )

    @classmethod
    def _apply_override(
        cls,
        policy: ResolvedRetrievalPolicy,
        override: RetrievalPolicyOverride | None,
    ) -> ResolvedRetrievalPolicy:
        if override is None:
            return policy
        allowed_kinds = policy.allowed_kinds
        if override.allowed_kinds is not None:
            allowed_kinds = tuple(
                kind for kind in override.allowed_kinds
                if kind in cls.SUPPORTED_KINDS
            )
        return ResolvedRetrievalPolicy(
            enabled=policy.enabled if override.enabled is None else bool(override.enabled),
            allowed_kinds=allowed_kinds,
            hint_selection=policy.hint_selection if override.hint_selection is None else override.hint_selection,
            min_score=policy.min_score if override.min_score is None else int(override.min_score),
            require_unique_top=(
                policy.require_unique_top
                if override.require_unique_top is None else bool(override.require_unique_top)
            ),
            max_artifacts=policy.max_artifacts if override.max_artifacts is None else int(override.max_artifacts),
            max_chars=policy.max_chars if override.max_chars is None else int(override.max_chars),
            max_lines_per_artifact=(
                policy.max_lines_per_artifact
                if override.max_lines_per_artifact is None else int(override.max_lines_per_artifact)
            ),
        )

    @classmethod
    def _plan_result(
        cls,
        *,
        stage: str,
        requests: list[RetrievalRequest],
        artifacts: list[RetrievedArtifact],
        applied_policy: ResolvedRetrievalPolicy,
        empty_retrievals: int = 0,
        skipped_by_policy: int = 0,
        policy: str = '',
    ) -> dict[str, object]:
        return {
            'stage': stage,
            'policy': policy,
            'applied_policy': applied_policy.as_payload(),
            'requests': requests,
            'artifacts': artifacts,
            'stats': {
                'planned_retrievals': len(requests),
                'executed_retrievals': len(artifacts),
                'empty_retrievals': empty_retrievals,
                'skipped_by_policy': skipped_by_policy,
            },
        }

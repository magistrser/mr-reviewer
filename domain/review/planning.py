from __future__ import annotations

import re
from pathlib import Path

from domain.models import ChangedFile, FileAnalysis, ReviewCluster
from domain.review.gate import ReviewGate
from runtime.async_ops import AsyncPathIO


class ReviewScopePlanner:
    MAX_IMPORT_LINES = 12
    MAX_SYMBOL_NAMES = 12
    MAX_CLUSTERS = 3
    MAX_FILES_PER_CLUSTER = 4
    KNOWN_SOURCE_EXTENSIONS = frozenset(ReviewGate.LANG_MAP)
    CLUSTER_PRIORITY: dict[str, int] = {
        'security': 0,
        'api_boundary': 1,
        'data_integrity': 2,
        'async_concurrency': 3,
        'architecture': 4,
    }
    SYMBOL_PATTERNS: dict[str, re.Pattern[str]] = {
        'python': re.compile(r'^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b'),
        'rust': re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][A-Za-z0-9_]*)\b'),
        'typescript': re.compile(
            r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)\b'
        ),
        'javascript': re.compile(
            r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b'
        ),
    }
    IMPORT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
        'python': (
            re.compile(r'^\s*import\s+\S+'),
            re.compile(r'^\s*from\s+\S+\s+import\s+'),
        ),
        'rust': (
            re.compile(r'^\s*use\s+'),
            re.compile(r'^\s*mod\s+'),
        ),
        'typescript': (
            re.compile(r'^\s*import\s+'),
            re.compile(r'.*require\('),
        ),
        'javascript': (
            re.compile(r'^\s*import\s+'),
            re.compile(r'.*require\('),
        ),
    }
    SECURITY_TOKENS = frozenset({
        'auth', 'token', 'secret', 'jwt', 'oauth', 'permission', 'policy', 'acl', 'crypto', 'signature',
    })
    API_TOKENS = frozenset({
        'api', 'route', 'router', 'handler', 'controller', 'endpoint', 'request', 'response', 'schema', 'dto',
        'serializer', 'graphql',
    })
    SCHEMA_TOKENS = frozenset({
        'schema', 'dto', 'serializer', 'payload', 'request', 'response', 'contract',
    })
    DATA_TOKENS = frozenset({
        'db', 'sql', 'migration', 'model', 'repository', 'repo', 'transaction', 'store', 'persistence',
    })
    ARCHITECTURE_TOKENS = frozenset({
        'service', 'usecase', 'manager', 'client', 'gateway', 'repository', 'repo', 'handler', 'controller',
    })
    ROLE_HINTS: dict[str, frozenset[str]] = {
        'handler': frozenset({'handler', 'endpoint', 'route'}),
        'controller': frozenset({'controller'}),
        'service': frozenset({'service', 'usecase', 'manager'}),
        'client': frozenset({'client', 'gateway'}),
        'serializer': frozenset({'serializer'}),
        'schema': frozenset({'schema', 'payload', 'contract'}),
        'dto': frozenset({'dto', 'request', 'response'}),
        'repository': frozenset({'repository', 'repo', 'store'}),
        'model': frozenset({'model'}),
        'migration': frozenset({'migration'}),
        'worker': frozenset({'worker', 'job'}),
        'consumer': frozenset({'consumer', 'subscriber'}),
        'producer': frozenset({'producer', 'publisher'}),
        'policy': frozenset({'policy', 'permission'}),
    }
    ASYNC_TEXT_MARKERS = (
        'async ',
        'await ',
        'promise',
        'tokio::spawn',
        'spawn_blocking',
        'asyncio.',
        'thread',
        'mutex',
        'channel',
        'queue',
        'consumer',
        'producer',
        'worker',
    )

    @classmethod
    async def enrich_files(
        cls,
        files: list[ChangedFile],
        repo_dir: Path,
        repo_catalog: dict | None = None,
    ) -> None:
        catalog_files = (
            repo_catalog.get('files', {})
            if isinstance(repo_catalog, dict) else {}
        )
        for changed_file in files:
            if changed_file.get('skip') or changed_file.get('is_binary') or changed_file.get('is_deleted'):
                continue
            new_path = changed_file.get('new_path', '')
            catalog_entry = catalog_files.get(new_path) if isinstance(catalog_files, dict) else None
            if isinstance(catalog_entry, dict):
                changed_file['analysis'] = cls._analysis_from_catalog_entry(changed_file, catalog_entry)
                continue
            source_text = ''
            source_path = repo_dir / new_path
            if await AsyncPathIO.exists(source_path):
                try:
                    source_text = await AsyncPathIO.read_text(source_path, errors='replace')
                except Exception:
                    source_text = ''
            changed_file['analysis'] = cls.build_file_analysis(changed_file, source_text)

    @classmethod
    def build_file_analysis(cls, changed_file: ChangedFile, source_text: str) -> FileAnalysis:
        new_path = changed_file.get('new_path', '')
        language = cls.language_for_file(changed_file)
        directory = str(Path(new_path).parent) if Path(new_path).parent != Path('.') else '.'
        path_tokens = cls._tokenize_path(new_path)
        import_lines = cls._extract_import_lines(source_text, language)
        import_targets = cls._extract_import_targets(import_lines, language)
        symbol_names = cls._extract_symbol_names(source_text, language)
        api_terms = cls._matched_terms(
            cls.API_TOKENS,
            path_tokens,
            import_lines,
            import_targets,
            symbol_names,
            source_text,
        )
        schema_terms = cls._matched_terms(
            cls.SCHEMA_TOKENS,
            path_tokens,
            import_lines,
            import_targets,
            symbol_names,
            source_text,
        )
        auth_terms = cls._matched_terms(
            cls.SECURITY_TOKENS,
            path_tokens,
            import_lines,
            import_targets,
            symbol_names,
            source_text,
        )
        storage_terms = cls._matched_terms(
            cls.DATA_TOKENS,
            path_tokens,
            import_lines,
            import_targets,
            symbol_names,
            source_text,
        )
        role_hints = cls._role_hints(path_tokens, symbol_names, import_targets)
        async_markers = cls._async_markers(source_text)
        focus_areas = cls._detect_focus_areas(
            new_path,
            source_text,
            language,
            path_tokens,
            import_lines,
            import_targets,
            symbol_names,
            role_hints,
            async_markers,
        )
        retrieval_hints = cls._retrieval_hints(
            new_path,
            import_targets,
            symbol_names,
            role_hints,
            schema_terms,
            auth_terms,
            storage_terms,
            async_markers,
        )
        return FileAnalysis(
            path=new_path,
            language=language,
            directory=directory,
            path_tokens=path_tokens,
            focus_areas=focus_areas,
            import_lines=import_lines,
            import_targets=import_targets,
            symbol_names=symbol_names,
            role_hints=role_hints,
            api_terms=api_terms,
            schema_terms=schema_terms,
            auth_terms=auth_terms,
            storage_terms=storage_terms,
            async_markers=async_markers,
            retrieval_hints=retrieval_hints,
        )

    @classmethod
    def build_clusters(cls, files: list[ChangedFile]) -> list[ReviewCluster]:
        eligible = [
            changed_file for changed_file in files
            if not changed_file.get('skip') and changed_file.get('analysis')
        ]
        buckets: list[tuple[int, str, str, list[str]]] = []
        for focus_area, priority in cls.CLUSTER_PRIORITY.items():
            grouped: dict[str, list[str]] = {}
            for changed_file in eligible:
                analysis = changed_file.get('analysis', {})
                if focus_area not in analysis.get('focus_areas', []):
                    continue
                scope = cls.cluster_scope_for_directory(str(analysis.get('directory', '.')))
                grouped.setdefault(scope, []).append(changed_file['new_path'])
            for scope, paths in grouped.items():
                unique_paths = sorted(dict.fromkeys(paths))
                if len(unique_paths) >= 2:
                    buckets.append((priority, scope, focus_area, unique_paths))

        clusters: list[ReviewCluster] = []
        used_paths: set[str] = set()
        for priority, scope, focus_area, paths in sorted(
            buckets,
            key=lambda item: (item[0], -len(item[3]), item[1]),
        ):
            selected_paths = [path for path in paths if path not in used_paths][:cls.MAX_FILES_PER_CLUSTER]
            if len(selected_paths) < 2:
                continue
            cluster_id = f'cluster_{len(clusters) + 1:02d}'
            clusters.append(ReviewCluster(
                cluster_id=cluster_id,
                title=cls._cluster_title(focus_area, scope, selected_paths),
                reason=cls._cluster_reason(focus_area, scope, selected_paths),
                focus_area=focus_area,
                files=selected_paths,
            ))
            used_paths.update(selected_paths)
            if len(clusters) >= cls.MAX_CLUSTERS:
                break
        return clusters

    @classmethod
    def language_for_file(cls, changed_file: ChangedFile) -> str:
        analysis = changed_file.get('analysis', {})
        if analysis.get('language'):
            return analysis['language']
        ext = Path(changed_file.get('new_path', '')).suffix.lstrip('.') or 'text'
        return ReviewGate.LANG_MAP.get(ext, ext)

    @classmethod
    def cluster_scope_for_directory(cls, directory: str) -> str:
        if not directory or directory == '.':
            return '.'
        parts = [part for part in directory.split('/') if part and part != '.']
        return '/'.join(parts[:2]) if parts else '.'

    @classmethod
    def _analysis_from_catalog_entry(cls, changed_file: ChangedFile, entry: dict) -> FileAnalysis:
        return FileAnalysis(
            path=changed_file.get('new_path', ''),
            language=str(entry.get('language', cls.language_for_file(changed_file))),
            directory=str(entry.get('directory', '.')),
            path_tokens=list(entry.get('path_tokens', [])),
            focus_areas=list(entry.get('focus_areas', [])),
            import_lines=list(entry.get('import_lines', [])),
            import_targets=list(entry.get('import_targets', [])),
            symbol_names=list(entry.get('symbol_names', [])),
            role_hints=list(entry.get('role_hints', [])),
            api_terms=list(entry.get('api_terms', [])),
            schema_terms=list(entry.get('schema_terms', [])),
            auth_terms=list(entry.get('auth_terms', [])),
            storage_terms=list(entry.get('storage_terms', [])),
            async_markers=list(entry.get('async_markers', [])),
            retrieval_hints=list(entry.get('retrieval_hints', [])),
        )

    @classmethod
    def _tokenize_path(cls, path: str) -> list[str]:
        return [token for token in re.split(r'[^a-z0-9]+', path.lower()) if token]

    @classmethod
    def _extract_import_lines(cls, source_text: str, language: str) -> list[str]:
        patterns = cls.IMPORT_PATTERNS.get(language, ())
        lines: list[str] = []
        for line in source_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern.match(stripped) for pattern in patterns):
                lines.append(stripped)
            if len(lines) >= cls.MAX_IMPORT_LINES:
                break
        return lines

    @classmethod
    def _extract_import_targets(cls, import_lines: list[str], language: str) -> list[str]:
        targets: list[str] = []
        for line in import_lines:
            if language == 'python':
                import_match = re.match(r'^\s*import\s+(.+)$', line)
                from_match = re.match(r'^\s*from\s+(\S+)\s+import\s+(.+)$', line)
                if from_match:
                    module = from_match.group(1)
                    names = [
                        name.strip().split(' as ')[0]
                        for name in from_match.group(2).split(',')
                    ]
                    targets.append(module)
                    targets.extend(
                        f'{module}.{name}'
                        for name in names
                        if name and name != '*'
                    )
                elif import_match:
                    names = [
                        name.strip().split(' as ')[0]
                        for name in import_match.group(1).split(',')
                    ]
                    targets.extend(name for name in names if name)
            elif language == 'rust':
                use_match = re.match(r'^\s*use\s+([^;]+);?$', line)
                mod_match = re.match(r'^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)', line)
                if use_match:
                    targets.append(use_match.group(1).strip())
                elif mod_match:
                    targets.append(mod_match.group(1).strip())
            elif language in {'typescript', 'javascript'}:
                from_match = re.search(r"from\s+['\"]([^'\"]+)['\"]", line)
                require_match = re.search(r"require\(['\"]([^'\"]+)['\"]\)", line)
                target = from_match.group(1) if from_match else (
                    require_match.group(1) if require_match else ''
                )
                if target:
                    targets.append(target)
            if len(targets) >= cls.MAX_IMPORT_LINES:
                break
        return sorted(dict.fromkeys(targets))[: cls.MAX_IMPORT_LINES]

    @classmethod
    def _extract_symbol_names(cls, source_text: str, language: str) -> list[str]:
        pattern = cls.SYMBOL_PATTERNS.get(language)
        if not pattern:
            return []
        names: list[str] = []
        for line in source_text.splitlines():
            match = pattern.match(line)
            if match:
                names.append(match.group(1))
            if len(names) >= cls.MAX_SYMBOL_NAMES:
                break
        return names

    @classmethod
    def _matched_terms(
        cls,
        tokens: frozenset[str],
        path_tokens: list[str],
        import_lines: list[str],
        import_targets: list[str],
        symbol_names: list[str],
        source_text: str,
    ) -> list[str]:
        haystack = ' '.join(path_tokens + import_lines + import_targets + symbol_names).lower()
        text_lower = source_text.lower()
        matched = [
            token for token in sorted(tokens)
            if token in path_tokens or token in haystack or token in text_lower
        ]
        return matched

    @classmethod
    def _role_hints(
        cls,
        path_tokens: list[str],
        symbol_names: list[str],
        import_targets: list[str],
    ) -> list[str]:
        haystack = ' '.join(path_tokens + import_targets + symbol_names).lower()
        hints = [
            role for role, variants in sorted(cls.ROLE_HINTS.items())
            if any(variant in haystack for variant in variants)
        ]
        return hints

    @classmethod
    def _async_markers(cls, source_text: str) -> list[str]:
        text_lower = source_text.lower()
        return [
            marker for marker in cls.ASYNC_TEXT_MARKERS
            if marker in text_lower
        ]

    @classmethod
    def _detect_focus_areas(
        cls,
        new_path: str,
        source_text: str,
        language: str,
        path_tokens: list[str],
        import_lines: list[str],
        import_targets: list[str],
        symbol_names: list[str],
        role_hints: list[str],
        async_markers: list[str],
    ) -> list[str]:
        focus_areas: list[str] = []
        haystack = ' '.join(path_tokens + import_lines + import_targets + symbol_names + role_hints).lower()
        text_lower = source_text.lower()

        if cls.SECURITY_TOKENS.intersection(path_tokens) or any(token in haystack for token in cls.SECURITY_TOKENS):
            focus_areas.append('security')
        if cls.API_TOKENS.intersection(path_tokens) or any(token in haystack for token in cls.API_TOKENS):
            focus_areas.append('api_boundary')
        if cls.DATA_TOKENS.intersection(path_tokens) or any(token in haystack for token in cls.DATA_TOKENS):
            focus_areas.append('data_integrity')
        if async_markers or any(marker in text_lower for marker in cls.ASYNC_TEXT_MARKERS):
            focus_areas.append('async_concurrency')
        if (
            cls.ARCHITECTURE_TOKENS.intersection(path_tokens)
            or any(token in haystack for token in cls.ARCHITECTURE_TOKENS)
        ):
            focus_areas.append('architecture')
        if not focus_areas:
            focus_areas.append('correctness')
        elif 'correctness' not in focus_areas:
            focus_areas.insert(0, 'correctness')
        return focus_areas

    @classmethod
    def _retrieval_hints(
        cls,
        new_path: str,
        import_targets: list[str],
        symbol_names: list[str],
        role_hints: list[str],
        schema_terms: list[str],
        auth_terms: list[str],
        storage_terms: list[str],
        async_markers: list[str],
    ) -> list[dict[str, str]]:
        hints: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_hint(kind: str, query: str, reason: str) -> None:
            query = query.strip()
            if not query:
                return
            key = (kind, query)
            if key in seen:
                return
            seen.add(key)
            hints.append({
                'kind': kind,
                'query': query,
                'reason': reason,
                'source_path': new_path,
            })

        for import_target in import_targets[:3]:
            add_hint(
                'import_owner',
                import_target,
                f'Find the repo file that owns the imported target `{import_target}`.',
            )
        for symbol_name in symbol_names[:4]:
            lowered = symbol_name.lower()
            if any(token in lowered for token in cls.SCHEMA_TOKENS):
                add_hint(
                    'schema_partner',
                    symbol_name,
                    f'Inspect related schema or DTO context for `{symbol_name}`.',
                )
            if any(token in lowered for token in {'service', 'client', 'gateway', 'manager'}):
                add_hint(
                    'service_partner',
                    symbol_name,
                    f'Inspect related service context for `{symbol_name}`.',
                )
            if any(token in lowered for token in {'worker', 'consumer', 'producer', 'queue', 'job'}):
                add_hint(
                    'async_partner',
                    symbol_name,
                    f'Inspect the async partner for `{symbol_name}`.',
                )
        if auth_terms and any(role in role_hints for role in {'service', 'client', 'policy'}):
            add_hint(
                'service_partner',
                auth_terms[0],
                f'Inspect the neighboring auth boundary for `{auth_terms[0]}`.',
            )
        if storage_terms and any(role in role_hints for role in {'repository', 'model', 'migration'}):
            add_hint(
                'symbol_definition',
                storage_terms[0],
                f'Inspect the storage definition tied to `{storage_terms[0]}`.',
            )
        if schema_terms:
            add_hint(
                'schema_partner',
                schema_terms[0],
                f'Inspect related schema context for `{schema_terms[0]}`.',
            )
        if async_markers:
            add_hint(
                'async_partner',
                async_markers[0],
                f'Inspect the async partner for marker `{async_markers[0]}`.',
            )
        return hints

    @classmethod
    def _cluster_title(cls, focus_area: str, scope: str, files: list[str]) -> str:
        label = focus_area.replace('_', ' ')
        if scope == '.':
            return f'{label.title()} changes across {len(files)} files'
        return f'{label.title()} changes in {scope}'

    @classmethod
    def _cluster_reason(cls, focus_area: str, scope: str, files: list[str]) -> str:
        if focus_area == 'security':
            return f'Multiple changed files in {scope} touch authentication, authorization, or secret-handling flows.'
        if focus_area == 'api_boundary':
            return f'Multiple changed files in {scope} affect request, response, or schema boundaries.'
        if focus_area == 'data_integrity':
            return f'Multiple changed files in {scope} change storage, transaction, or schema-related behavior.'
        if focus_area == 'async_concurrency':
            return (
                f'Multiple changed files in {scope} use async or concurrent execution paths that should stay '
                'consistent.'
            )
        return f'Multiple changed files in {scope} likely need a cross-file consistency review.'

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from domain.models import ChangedFile, ReviewCluster
from domain.review.planning import ReviewScopePlanner
from domain.review.standards import Resources
from runtime.async_ops import AsyncPathIO


@dataclass(frozen=True)
class ReviewContextLimits:
    scale: float = 1.0
    max_context_chars: int = 28000
    max_cluster_context_chars: int = 30000
    review_goal_chars: int = 400
    project_profile_chars: int = 1800
    pr_summary_chars: int = 1600
    file_profile_chars: int = 1400
    excerpt_chars: int = 4200
    imports_chars: int = 900
    symbols_chars: int = 4200
    review_plan_chars: int = 2800
    standards_chars: int = 18000
    severity_chars: int = 900
    template_chars: int = 900
    cluster_intro_chars: int = 1400
    cluster_file_chars: int = 3200
    max_symbol_blocks: int = 4
    max_symbol_lines: int = 80
    cluster_excerpt_radius: int = 4
    cluster_max_segments: int = 2

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError('review.context.scale must be greater than 0')

    def scaled_value(self, value: int) -> int:
        return max(1, int(round(value * self.scale)))

    def section_chars(self) -> dict[str, int]:
        return {
            'review_goal': self.scaled_value(self.review_goal_chars),
            'project_profile': self.scaled_value(self.project_profile_chars),
            'pr_summary': self.scaled_value(self.pr_summary_chars),
            'file_profile': self.scaled_value(self.file_profile_chars),
            'excerpt': self.scaled_value(self.excerpt_chars),
            'imports': self.scaled_value(self.imports_chars),
            'symbols': self.scaled_value(self.symbols_chars),
            'review_plan': self.scaled_value(self.review_plan_chars),
            'standards': self.scaled_value(self.standards_chars),
            'severity': self.scaled_value(self.severity_chars),
            'template': self.scaled_value(self.template_chars),
            'cluster_intro': self.scaled_value(self.cluster_intro_chars),
            'cluster_file': self.scaled_value(self.cluster_file_chars),
        }


class ReviewContextBuilder:
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

    @classmethod
    async def write_file_context(
        cls,
        changed_file: ChangedFile,
        repo_dir: Path,
        excerpts_path: Path,
        context_path: Path,
        pass_id: str,
        focus_area: str,
        standards_text: str,
        review_plan_text: str,
        resources: Resources,
        pr_summary_path: Path | None = None,
        limits: ReviewContextLimits | None = None,
        retrieved_artifacts: list[dict[str, object]] | None = None,
    ) -> None:
        limits = limits or ReviewContextLimits()
        section_chars = limits.section_chars()
        source_path = repo_dir / changed_file['new_path']
        source_text = await cls._read_optional_text(source_path)
        source_lines = source_text.splitlines()
        excerpt_text = await cls._read_optional_text(excerpts_path)
        summary_text = await cls._read_optional_text(pr_summary_path) if pr_summary_path else ''
        analysis = changed_file.get('analysis', {})
        language = analysis.get('language') or ReviewScopePlanner.language_for_file(changed_file)
        changed_lines = cls._changed_lines(changed_file)

        sections = [
            cls._section('Review Goal', cls._truncate(
                f'Pass `{pass_id}` focuses on `{focus_area}` for `{changed_file["new_path"]}`. '
                'Use only changed lines or deterministic context derived from nearby code to support findings.',
                section_chars['review_goal'],
            )),
            cls._section(
                'Project Review Profile',
                cls._truncate(resources.project_profile, section_chars['project_profile']),
            ),
            cls._section('PR Summary', cls._truncate(summary_text, section_chars['pr_summary'])),
            cls._section(
                'File Profile',
                cls._truncate(cls._file_profile(changed_file, focus_area), section_chars['file_profile']),
            ),
            cls._section('Changed Excerpts', cls._truncate(excerpt_text, section_chars['excerpt'])),
            cls._section('Relevant Imports', cls._truncate(cls._imports_section(analysis), section_chars['imports'])),
            cls._section(
                'Changed Symbol Bodies',
                cls._truncate(
                    cls._symbol_section(source_lines, language, changed_lines, limits),
                    section_chars['symbols'],
                ),
            ),
            cls._section('Related Repo Context', cls._related_repo_context(retrieved_artifacts or [])),
            cls._section('Review Plan', cls._truncate(review_plan_text, section_chars['review_plan'])),
            cls._section('Coding Standards', cls._truncate(standards_text, section_chars['standards'])),
            cls._section('Severity Levels', cls._truncate(resources.severity, section_chars['severity'])),
            cls._section('Inline Comment Template', cls._truncate(resources.template, section_chars['template'])),
        ]
        await AsyncPathIO.write_text(
            context_path,
            cls._compose(sections, limits.scaled_value(limits.max_context_chars)),
        )

    @classmethod
    async def write_cluster_context(
        cls,
        cluster: ReviewCluster,
        cluster_files: list[ChangedFile],
        repo_dir: Path,
        context_path: Path,
        standards_text: str,
        review_plan_text: str,
        resources: Resources,
        pr_summary_path: Path | None = None,
        limits: ReviewContextLimits | None = None,
        retrieved_artifacts: list[dict[str, object]] | None = None,
    ) -> None:
        limits = limits or ReviewContextLimits()
        section_chars = limits.section_chars()
        summary_text = await cls._read_optional_text(pr_summary_path) if pr_summary_path else ''
        sections = [
            cls._section(
                'Cluster Review Goal',
                cls._truncate(
                    f'Review cluster `{cluster["cluster_id"]}` ({cluster["title"]}). Focus on cross-file defects only. '
                    f'Reason: {cluster["reason"]}',
                    section_chars['cluster_intro'],
                ),
            ),
            cls._section(
                'Project Review Profile',
                cls._truncate(resources.project_profile, section_chars['project_profile']),
            ),
            cls._section('PR Summary', cls._truncate(summary_text, section_chars['pr_summary'])),
            cls._section('Related Repo Context', cls._related_repo_context(retrieved_artifacts or [])),
            cls._section('Review Plan', cls._truncate(review_plan_text, section_chars['review_plan'])),
            cls._section('Coding Standards', cls._truncate(standards_text, section_chars['standards'])),
        ]
        for changed_file in cluster_files:
            source_path = repo_dir / changed_file['new_path']
            source_text = await cls._read_optional_text(source_path)
            source_lines = source_text.splitlines()
            analysis = changed_file.get('analysis', {})
            language = analysis.get('language') or ReviewScopePlanner.language_for_file(changed_file)
            changed_lines = cls._changed_lines(changed_file)
            cluster_body = '\n\n'.join(
                part for part in [
                    cls._file_profile(changed_file, cluster.get('focus_area', 'cluster')),
                    cls._imports_section(analysis),
                    cls._cluster_excerpt_section(source_lines, changed_lines, changed_file['new_path'], limits),
                    cls._symbol_section(source_lines, language, changed_lines, limits),
                ]
                if part
            )
            sections.append(
                cls._section(
                    f'File Context: {changed_file["new_path"]}',
                    cls._truncate(cluster_body, section_chars['cluster_file']),
                )
            )
        sections.extend([
            cls._section('Severity Levels', cls._truncate(resources.severity, section_chars['severity'])),
            cls._section('Inline Comment Template', cls._truncate(resources.template, section_chars['template'])),
        ])
        await AsyncPathIO.write_text(
            context_path,
            cls._compose(sections, limits.scaled_value(limits.max_cluster_context_chars)),
        )

    @classmethod
    async def _read_optional_text(cls, path: Path | None) -> str:
        if path is None:
            return ''
        if not await AsyncPathIO.exists(path):
            return ''
        try:
            return await AsyncPathIO.read_text(path, errors='replace')
        except Exception as exc:
            return f'# ERROR reading {path}: {exc}\n'

    @classmethod
    def _section(cls, title: str, body: str) -> str:
        if not body.strip():
            return ''
        return f'## {title}\n\n{body.strip()}\n'

    @classmethod
    def _compose(cls, sections: list[str], max_chars: int) -> str:
        out = '# Review Context\n\n'
        for section in sections:
            if not section:
                continue
            remaining = max_chars - len(out)
            if remaining <= 0:
                break
            if len(section) <= remaining:
                out += section + '\n'
                continue
            out += cls._truncate(section, max(remaining - 16, 0))
            out += '\n'
            break
        return out.rstrip() + '\n'

    @classmethod
    def _truncate(cls, text: str, max_chars: int) -> str:
        clean = text.strip()
        if max_chars <= 0 or not clean:
            return ''
        if len(clean) <= max_chars:
            return clean
        return clean[: max_chars - 13].rstrip() + '\n[truncated]'

    @classmethod
    def _file_profile(cls, changed_file: ChangedFile, focus_area: str) -> str:
        analysis = changed_file.get('analysis', {})
        focus_areas = ', '.join(analysis.get('focus_areas', [])) or focus_area
        symbol_names = ', '.join(analysis.get('symbol_names', [])[:8]) or '(none)'
        return (
            f'- Path: `{changed_file["new_path"]}`\n'
            f'- Language: `{analysis.get("language", ReviewScopePlanner.language_for_file(changed_file))}`\n'
            f'- Focus areas: `{focus_areas}`\n'
            f'- Symbols in file: {symbol_names}'
        )

    @classmethod
    def _imports_section(cls, analysis: dict) -> str:
        imports = analysis.get('import_lines', [])
        if not imports:
            return '(no import lines captured)'
        return '\n'.join(f'- `{line}`' for line in imports[: ReviewScopePlanner.MAX_IMPORT_LINES])

    @classmethod
    def _related_repo_context(cls, retrieved_artifacts: list[dict[str, object]]) -> str:
        if not retrieved_artifacts:
            return ''
        blocks: list[str] = []
        for artifact in retrieved_artifacts:
            path = str(artifact.get('path', 'unknown'))
            reason = str(artifact.get('reason', '')).strip()
            snippet = str(artifact.get('snippet', '')).strip()
            if not snippet:
                continue
            header = f'### {path}'
            if reason:
                header += f'\nReason: {reason}'
            blocks.append(f'{header}\n\n{snippet}')
        return '\n\n'.join(blocks)

    @classmethod
    def _changed_lines(cls, changed_file: ChangedFile) -> list[int]:
        changed_lines: list[int] = []
        for hunk in changed_file.get('hunks', []):
            if hunk['new_count'] <= 0:
                continue
            changed_lines.extend(range(hunk['new_start'], hunk['new_start'] + hunk['new_count']))
        if changed_file.get('is_new') and not changed_lines:
            return [1]
        return sorted(dict.fromkeys(changed_lines))

    @classmethod
    def _symbol_section(
        cls,
        source_lines: list[str],
        language: str,
        changed_lines: list[int],
        limits: ReviewContextLimits,
    ) -> str:
        if not source_lines or not changed_lines:
            return '(no symbol bodies captured)'
        symbol_ranges = cls._symbol_ranges(source_lines, language)
        selected: list[tuple[str, int, int]] = []
        for symbol_name, start, end in symbol_ranges:
            if any(start + 1 <= line <= end + 1 for line in changed_lines):
                selected.append((symbol_name, start, end))
            if len(selected) >= limits.scaled_value(limits.max_symbol_blocks):
                break
        if not selected:
            return '(no symbol bodies captured)'
        blocks: list[str] = []
        for symbol_name, start, end in selected:
            block_end = min(end, start + limits.scaled_value(limits.max_symbol_lines) - 1)
            rendered = [
                f'{line_number}: {source_lines[line_number - 1]}'
                for line_number in range(start + 1, block_end + 2)
            ]
            blocks.append(
                f'### {symbol_name} ({start + 1}-{block_end + 1})\n' + '\n'.join(rendered)
            )
        return '\n\n'.join(blocks)

    @classmethod
    def _symbol_ranges(cls, source_lines: list[str], language: str) -> list[tuple[str, int, int]]:
        pattern = cls.SYMBOL_PATTERNS.get(language)
        if not pattern:
            return []
        starts: list[tuple[str, int]] = []
        for index, line in enumerate(source_lines):
            match = pattern.match(line)
            if match:
                starts.append((match.group(1), index))
        ranges: list[tuple[str, int, int]] = []
        for position, (symbol_name, start) in enumerate(starts):
            next_start = starts[position + 1][1] if position + 1 < len(starts) else len(source_lines)
            if language == 'python':
                end = cls._python_symbol_end(source_lines, start, next_start)
            else:
                end = cls._brace_symbol_end(source_lines, start, next_start)
            ranges.append((symbol_name, start, end))
        return ranges

    @classmethod
    def _python_symbol_end(cls, source_lines: list[str], start: int, next_start: int) -> int:
        start_indent = len(source_lines[start]) - len(source_lines[start].lstrip())
        end = min(next_start - 1, len(source_lines) - 1)
        for index in range(start + 1, min(next_start, len(source_lines))):
            line = source_lines[index]
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= start_indent and not line.lstrip().startswith(('#', '@')):
                end = index - 1
                break
        return max(end, start)

    @classmethod
    def _brace_symbol_end(cls, source_lines: list[str], start: int, next_start: int) -> int:
        end = min(next_start - 1, len(source_lines) - 1)
        brace_depth = 0
        seen_open = False
        for index in range(start, min(next_start, len(source_lines))):
            line = source_lines[index]
            brace_depth += line.count('{')
            if line.count('{'):
                seen_open = True
            brace_depth -= line.count('}')
            if seen_open and brace_depth <= 0 and index > start:
                end = index
                break
        return max(end, start)

    @classmethod
    def _cluster_excerpt_section(
        cls,
        source_lines: list[str],
        changed_lines: list[int],
        new_path: str,
        limits: ReviewContextLimits,
    ) -> str:
        if not source_lines or not changed_lines:
            return f'# {new_path}\n(no changed excerpts captured)'
        ranges: list[tuple[int, int]] = []
        for line_number in changed_lines:
            radius = limits.scaled_value(limits.cluster_excerpt_radius)
            start = max(0, line_number - 1 - radius)
            end = min(len(source_lines), line_number + radius)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))
            if len(ranges) >= limits.scaled_value(limits.cluster_max_segments):
                break
        rendered = [f'# {new_path}']
        for start, end in ranges:
            rendered.append(f'## lines {start + 1}-{end}')
            rendered.extend(
                f'{line_number}: {source_lines[line_number - 1]}'
                for line_number in range(start + 1, end + 1)
            )
        return '\n'.join(rendered)

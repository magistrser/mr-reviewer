from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from domain.models import ExistingComment, Finding


class CommentDedupPlanner:
    DEFAULT_LINE_WINDOW = 3
    CONFIDENCE_ORDER = {
        'High': 3,
        'Medium': 2,
        'Low': 1,
    }

    @classmethod
    def sort_key(cls, finding: Finding) -> tuple[int, int, int]:
        anchor = finding.get('anchor', {})
        anchor_line = anchor.get('new_line') or anchor.get('old_line') or 0
        return (
            cls._severity_rank(finding),
            cls.CONFIDENCE_ORDER.get(finding.get('confidence', ''), 0),
            -anchor_line,
        )

    @classmethod
    def build_groups(
        cls,
        findings: list[Finding],
        existing_comments: Iterable[ExistingComment],
        line_window: int = DEFAULT_LINE_WINDOW,
    ) -> list[dict]:
        indexed = [
            {
                'index': index,
                'file_path': cls.finding_path(finding),
                'line': cls.finding_line(finding),
                'finding': finding,
            }
            for index, finding in enumerate(findings, start=1)
        ]
        by_path: dict[str, list[dict]] = defaultdict(list)
        for item in indexed:
            by_path[item['file_path']].append(item)

        existing_by_path: dict[str, list[ExistingComment]] = defaultdict(list)
        for comment in existing_comments:
            file_path = str(comment.get('file_path', ''))
            if not file_path:
                continue
            existing_by_path[file_path].append(comment)

        groups: list[tuple[int, dict]] = []
        for file_path, items in by_path.items():
            items.sort(key=lambda item: (item['line'] is None, item['line'] or 0, item['index']))
            current: list[dict] = []
            last_line: int | None = None
            for item in items:
                line = item['line']
                if line is None:
                    if current:
                        groups.append(
                            cls._materialize_group(file_path, current, existing_by_path[file_path], line_window)
                        )
                        current = []
                    groups.append(
                        cls._materialize_group(file_path, [item], existing_by_path[file_path], line_window)
                    )
                    last_line = None
                    continue
                if current and last_line is not None and line - last_line > line_window:
                    groups.append(
                        cls._materialize_group(file_path, current, existing_by_path[file_path], line_window)
                    )
                    current = []
                current.append(item)
                last_line = line
            if current:
                groups.append(
                    cls._materialize_group(file_path, current, existing_by_path[file_path], line_window)
                )

        groups.sort(key=lambda item: item[0])
        return [group for _, group in groups]

    @classmethod
    def finding_path(cls, finding: Finding) -> str:
        anchor = finding.get('anchor', {})
        return str(anchor.get('new_path') or anchor.get('old_path') or '')

    @classmethod
    def finding_line(cls, finding: Finding) -> int | None:
        anchor = finding.get('anchor', {})
        line = anchor.get('new_line')
        if line is not None:
            return int(line)
        old_line = anchor.get('old_line')
        return int(old_line) if old_line is not None else None

    @classmethod
    def _materialize_group(
        cls,
        file_path: str,
        items: list[dict],
        existing_comments: list[ExistingComment],
        line_window: int,
    ) -> tuple[int, dict]:
        group_lines = [int(item['line']) for item in items if item['line'] is not None]
        nearby_existing = [
            {
                'note_id': comment.get('note_id'),
                'author': comment.get('author', ''),
                'file_path': comment.get('file_path', file_path),
                'line': comment.get('line'),
                'body': comment.get('body', ''),
            }
            for comment in existing_comments
            if cls._is_existing_comment_near_group(comment, group_lines, line_window)
        ]
        earliest_index = min(int(item['index']) for item in items)
        return earliest_index, {
            'group_id': f'group_{earliest_index:03d}',
            'file_path': file_path,
            'line_window': line_window,
            'start_line': min(group_lines) if group_lines else None,
            'end_line': max(group_lines) if group_lines else None,
            'new_comments': [
                {
                    'index': item['index'],
                    'line': item['line'],
                    'short_title': item['finding'].get('short_title', ''),
                    'severity': item['finding'].get('severity', ''),
                    'confidence': item['finding'].get('confidence', ''),
                    'rule_ids': item['finding'].get('rule_ids', []),
                    'body': item['finding'].get('body', ''),
                    'evidence': item['finding'].get('evidence', ''),
                    'impact': item['finding'].get('impact', ''),
                    'focus_area': item['finding'].get('focus_area', ''),
                    'source_pass': item['finding'].get('source_pass', ''),
                    'source_kind': item['finding'].get('source_kind', ''),
                    'dedup_key': item['finding'].get('dedup_key', ''),
                    'anchor': item['finding'].get('anchor', {}),
                }
                for item in items
            ],
            'existing_comments': nearby_existing,
        }

    @classmethod
    def _is_existing_comment_near_group(
        cls,
        comment: ExistingComment,
        group_lines: list[int],
        line_window: int,
    ) -> bool:
        if not group_lines:
            return False
        line = comment.get('line')
        if line is None:
            return False
        return any(abs(int(line) - group_line) <= line_window for group_line in group_lines)

    @staticmethod
    def _severity_rank(finding: Finding) -> int:
        severity = str(finding.get('severity', ''))
        return {
            'Critical': 4,
            'Major': 3,
            'Minor': 2,
            'Suggestion': 1,
        }.get(severity, 0)

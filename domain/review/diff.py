from __future__ import annotations

import re
from pathlib import Path

from domain.models import DiffHunk
from runtime.async_ops import AsyncPathIO


class DiffReviewTools:
    EXCERPT_CONTEXT_LINES = 20

    @classmethod
    def parse_hunks(cls, diff_text: str | None) -> tuple[list[DiffHunk], bool]:
        if not diff_text:
            return [], False
        if 'Binary files' in diff_text:
            return [], True
        hunks: list[DiffHunk] = []
        for line in diff_text.split('\n'):
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                hunks.append(DiffHunk(
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2) if match.group(2) is not None else 1),
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4) if match.group(4) is not None else 1),
                ))
        return hunks, False

    @classmethod
    async def write_excerpts(
        cls,
        source_path: Path,
        hunks: list[DiffHunk],
        excerpts_path: Path,
        new_path: str,
    ) -> None:
        ctx = cls.EXCERPT_CONTEXT_LINES
        try:
            all_lines = (await AsyncPathIO.read_text(source_path, errors='replace')).splitlines(keepends=True)
        except Exception as exc:
            await AsyncPathIO.write_text(excerpts_path, f'# ERROR reading {source_path}: {exc}\n')
            return
        total = len(all_lines)
        ranges: list[tuple[int, int]] = []
        for hunk in hunks:
            start = max(0, hunk['new_start'] - 1 - ctx)
            end = min(total, hunk['new_start'] - 1 + hunk['new_count'] + ctx)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))
        out = [f'# {new_path} — hunk excerpts (±{ctx} lines context)\n']
        for segment_start, segment_end in ranges:
            out.append(f'\n## lines {segment_start + 1}–{segment_end}\n')
            for index in range(segment_end - segment_start):
                out.append(f'{segment_start + index + 1}: {all_lines[segment_start + index]}')
        await AsyncPathIO.write_text(excerpts_path, ''.join(out))

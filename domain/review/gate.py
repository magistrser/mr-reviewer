from __future__ import annotations

from pathlib import Path

from domain.models import ChangedFile
from runtime.async_ops import AsyncPathIO


class ReviewGate:
    SKIP_EXTS: frozenset[str] = frozenset({
        '.toml', '.sql', '.lock', '.md', '.txt', '.json',
        '.yaml', '.yml', '.env', '.gitignore', '.dockerfile', '.sh',
    })
    SKIP_PATHS: tuple[str, ...] = (
        'tests/', '/tests/', '/test/', 'test_', '_test.', '.spec.', 'spec/',
    )
    LANG_MAP: dict[str, str] = {
        'rs': 'rust',
        'py': 'python',
        'ts': 'typescript',
        'tsx': 'typescript',
        'js': 'javascript',
        'jsx': 'javascript',
        'go': 'go',
        'java': 'java',
        'kt': 'kotlin',
        'rb': 'ruby',
        'c': 'c',
        'cpp': 'cpp',
    }
    MAX_ELIGIBLE_FILES = 50
    LARGE_FILE_BYTES = 65536

    @classmethod
    async def gate(
        cls,
        files: list[ChangedFile],
        repo_dir: Path,
        max_eligible: int = MAX_ELIGIBLE_FILES,
        large_file_bytes: int = LARGE_FILE_BYTES,
    ) -> None:
        for changed_file in files:
            if changed_file.get('is_binary') or changed_file.get('is_deleted'):
                changed_file['skip'] = True
                continue
            new_path = changed_file.get('new_path', '')
            path = Path(new_path)
            ext = path.suffix.lower()
            if not ext:
                ext = path.name.lower()
            if ext in cls.SKIP_EXTS or any(fragment in new_path for fragment in cls.SKIP_PATHS):
                changed_file['skip'] = True
                continue
            try:
                stat_result = await AsyncPathIO.stat(repo_dir / new_path)
                size = stat_result.st_size
            except Exception:
                size = 0
            if size > large_file_bytes:
                changed_file['skip'] = True

        eligible_all = [changed_file for changed_file in files if not changed_file.get('skip')]
        eligible_all.sort(key=lambda changed_file: len(changed_file.get('hunks', [])), reverse=True)
        for changed_file in eligible_all[max_eligible:]:
            changed_file['skip'] = True

        kept = [changed_file for changed_file in eligible_all if not changed_file.get('skip')]
        skipped = [changed_file for changed_file in files if changed_file.get('skip')]
        files[:] = kept + skipped

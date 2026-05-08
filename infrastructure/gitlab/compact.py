from __future__ import annotations

import re

from domain.models import ChangedFile, ExistingComment, ExistingDiscussion, MrInfo
from domain.review.diff import DiffReviewTools


class GitLabCompactor:
    CS_RULE_RE = re.compile(r'CS-[A-Z]+-\d+')

    @classmethod
    def compact_mr(cls, mr: dict) -> MrInfo:
        web_url = mr.get('web_url', '')
        fallback_http = web_url.rsplit('/-/', 1)[0] + '.git' if '/-/' in web_url else ''
        return MrInfo(
            id=mr.get('id'),
            iid=mr.get('iid'),
            title=mr.get('title', ''),
            source_branch=mr.get('source_branch', ''),
            target_branch=mr.get('target_branch', ''),
            diff_refs=mr.get('diff_refs'),
            web_url=web_url,
            http_url_to_repo=mr.get('http_url_to_repo') or fallback_http,
        )

    @classmethod
    def compact_mr_comments(cls, discussions: list[dict]) -> list[ExistingComment]:
        result: list[ExistingComment] = []
        for disc in discussions:
            for note in disc.get('notes', []):
                if note.get('system'):
                    continue
                comment: ExistingComment = {
                    'note_id': note['id'],
                    'author': note.get('author', {}).get('username', ''),
                    'body': note.get('body', ''),
                }
                position = note.get('position')
                if position and position.get('new_path'):
                    comment['file_path'] = position['new_path']
                    comment['line'] = position.get('new_line') or position.get('old_line')
                result.append(comment)
        return result

    @classmethod
    def compact_discussions(cls, discussions: list[dict]) -> list[ExistingDiscussion]:
        result: list[ExistingDiscussion] = []
        for disc in discussions:
            for note in disc.get('notes', []):
                if note.get('system') or note.get('resolved', False):
                    continue
                position = note.get('position')
                if not position or not position.get('new_path'):
                    continue
                new_line = position.get('new_line')
                old_line = position.get('old_line')
                if new_line is None and old_line is None:
                    continue
                rule_ids: list[str] = cls.CS_RULE_RE.findall(note.get('body', ''))
                result.append(ExistingDiscussion(
                    note_id=note['id'],
                    file_path=position['new_path'],
                    new_line=new_line,
                    old_line=old_line,
                    rule_ids=rule_ids,
                    body=note.get('body', ''),
                ))
        return result

    @classmethod
    def compact_diff_for_summary(
        cls,
        changes_raw: dict | list,
        max_per_file: int = 600,
        max_total: int = 6000,
    ) -> list[dict]:
        changes = changes_raw.get('changes', []) if isinstance(changes_raw, dict) else changes_raw
        result: list[dict] = []
        total = 0
        for change in changes:
            if total >= max_total:
                break
            path = change.get('new_path') or change.get('old_path', '')
            diff_text = change.get('diff', '')
            lines = [
                line for line in diff_text.split('\n')
                if (line.startswith('+') or line.startswith('-'))
                and not line.startswith('+++')
                and not line.startswith('---')
            ]
            compact = '\n'.join(lines)
            if len(compact) > max_per_file:
                compact = compact[:max_per_file] + '\n[truncated]'
            if not compact:
                continue
            result.append({'file': path, 'diff': compact})
            total += len(compact)
        return result

    @classmethod
    def compact_changes(cls, changes_data: dict | list) -> list[ChangedFile]:
        changes = changes_data.get('changes', []) if isinstance(changes_data, dict) else changes_data
        result: list[ChangedFile] = []
        for change in changes:
            hunks, is_binary = DiffReviewTools.parse_hunks(change.get('diff', ''))
            result.append(ChangedFile(
                old_path=change.get('old_path', ''),
                new_path=change.get('new_path', ''),
                is_new=change.get('new_file', False),
                is_deleted=change.get('deleted_file', False),
                is_renamed=change.get('renamed_file', False),
                is_binary=is_binary,
                hunks=hunks,
            ))
        return result

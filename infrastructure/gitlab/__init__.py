from infrastructure.gitlab.client import GitLabClient
from infrastructure.gitlab.compact import GitLabCompactor

compact_mr = GitLabCompactor.compact_mr
compact_mr_comments = GitLabCompactor.compact_mr_comments
compact_discussions = GitLabCompactor.compact_discussions
compact_diff_for_summary = GitLabCompactor.compact_diff_for_summary
compact_changes = GitLabCompactor.compact_changes

__all__ = [
    'GitLabClient',
    'GitLabCompactor',
    'compact_changes',
    'compact_diff_for_summary',
    'compact_discussions',
    'compact_mr',
    'compact_mr_comments',
]

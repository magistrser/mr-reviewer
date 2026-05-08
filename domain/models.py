from __future__ import annotations

from typing import TypedDict


class DiffHunk(TypedDict):
    old_start: int
    old_count: int
    new_start: int
    new_count: int


class Anchor(TypedDict):
    old_path: str
    new_path: str
    new_line: int | None
    old_line: int | None


class Finding(TypedDict, total=False):
    severity: str
    confidence: str
    rule_ids: list[str]
    short_title: str
    anchor: Anchor
    body: str
    language: str
    focus_area: str
    evidence: str
    impact: str
    dedup_key: str
    source_pass: str
    source_kind: str
    cluster_id: str


class ChangedFile(TypedDict, total=False):
    old_path: str
    new_path: str
    is_new: bool
    is_deleted: bool
    is_renamed: bool
    is_binary: bool
    hunks: list[DiffHunk]
    skip: bool
    analysis: FileAnalysis


class ExistingDiscussion(TypedDict):
    note_id: int
    file_path: str
    new_line: int | None
    old_line: int | None
    rule_ids: list[str]
    body: str


class ExistingComment(TypedDict, total=False):
    note_id: int
    author: str
    file_path: str
    line: int
    body: str


class FileAnalysis(TypedDict, total=False):
    path: str
    language: str
    directory: str
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


class ReviewCluster(TypedDict, total=False):
    cluster_id: str
    title: str
    reason: str
    focus_area: str
    files: list[str]
    evidence: dict[str, object]
    retrieval_requests: list[dict[str, str]]


class BenchmarkExpectedFinding(TypedDict, total=False):
    file_path: str
    line: int
    rule_id: str
    severity: str
    short_title: str


class BenchmarkCase(TypedDict, total=False):
    name: str
    actual_findings: list[Finding]
    expected_findings: list[BenchmarkExpectedFinding]


class BenchmarkScore(TypedDict, total=False):
    name: str
    matched: int
    expected: int
    actual: int
    missed: int
    extra: int
    precision: float
    recall: float


class WorkspaceMeta(TypedDict):
    project_path: str
    mr_iid: str
    head_sha: str
    base_sha: str
    start_sha: str


class DiffRefs(TypedDict):
    base_sha: str
    start_sha: str
    head_sha: str


class MrInfo(TypedDict):
    id: int
    iid: int
    title: str
    source_branch: str
    target_branch: str
    diff_refs: DiffRefs | None
    web_url: str
    http_url_to_repo: str


class PostResult(TypedDict, total=False):
    ok: bool
    data: dict
    status: int | None
    error: str | None


class InlinePost(TypedDict, total=False):
    finding_index: int
    status: str
    note_id: str | None
    error: str | None


class SummaryLabels(TypedDict, total=False):
    title: str
    verdict_label: str
    scope_reviewed_label: str
    scope_reviewed_template: str
    severity_counts_heading: str
    severity_column: str
    count_column: str
    highlights_heading: str
    no_findings: str
    anchor_failures_heading: str
    out_of_hunk_heading: str
    footer: str
    severity_labels: dict[str, str]
    verdict_values: dict[str, str]


class PreparedReviewPublication(TypedDict, total=False):
    findings: list[Finding]
    out_of_hunk_findings: list[Finding]
    counts: dict[str, int]
    verdict: str
    eligible_file_count: int
    hunk_count: int
    summary_labels: SummaryLabels


SEVERITY_ORDER: dict[str, int] = {
    'Critical': 4,
    'Major': 3,
    'Minor': 2,
    'Suggestion': 1,
}

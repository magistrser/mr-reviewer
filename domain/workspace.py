from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def findings_dir(self) -> Path:
        return self.root / 'findings'

    @property
    def excerpts_dir(self) -> Path:
        return self.root / 'excerpts'

    @property
    def contexts_dir(self) -> Path:
        return self.root / 'contexts'

    @property
    def review_inputs_dir(self) -> Path:
        return self.root / 'review-inputs'

    @property
    def cluster_inputs_dir(self) -> Path:
        return self.root / 'cluster-inputs'

    @property
    def cluster_findings_dir(self) -> Path:
        return self.root / 'cluster-findings'

    @property
    def retrieval_plans_dir(self) -> Path:
        return self.root / 'retrieval-plans'

    @property
    def dedup_inputs_dir(self) -> Path:
        return self.root / 'dedup-inputs'

    @property
    def dedup_results_dir(self) -> Path:
        return self.root / 'dedup-results'

    @property
    def translation_inputs_dir(self) -> Path:
        return self.root / 'translation-inputs'

    @property
    def translation_results_dir(self) -> Path:
        return self.root / 'translation-results'

    @property
    def repo_dir(self) -> Path:
        return self.root / 'repo'

    @property
    def meta(self) -> Path:
        return self.root / 'meta.json'

    @property
    def progress(self) -> Path:
        return self.root / 'progress.json'

    @property
    def changed_files(self) -> Path:
        return self.root / 'changed-files.json'

    @property
    def file_analysis(self) -> Path:
        return self.root / 'file-analysis.json'

    @property
    def cluster_plan(self) -> Path:
        return self.root / 'cluster-plan.json'

    @property
    def repo_catalog(self) -> Path:
        return self.root / 'repo-catalog.json'

    @property
    def existing_discussions(self) -> Path:
        return self.root / 'existing-discussions.json'

    @property
    def existing_comments(self) -> Path:
        return self.root / 'existing-comments.json'

    @property
    def raw_findings(self) -> Path:
        return self.root / 'raw-findings.json'

    @property
    def all_findings(self) -> Path:
        return self.root / 'all-findings.json'

    @property
    def pr_summary(self) -> Path:
        return self.root / 'pr-summary.md'

    @property
    def summary_input(self) -> Path:
        return self.root / 'summary-input.json'

    @property
    def attempts(self) -> Path:
        return self.root / '.attempts.json'

    @property
    def cluster_attempts(self) -> Path:
        return self.root / '.cluster-attempts.json'

    @property
    def quality_report(self) -> Path:
        return self.root / 'quality-report.json'

    @property
    def dedup_report(self) -> Path:
        return self.root / 'dedup-report.json'

    @property
    def retrieval_report(self) -> Path:
        return self.root / 'retrieval-report.json'

    @property
    def translation_report(self) -> Path:
        return self.root / 'translation-report.json'

    @property
    def translated_publication(self) -> Path:
        return self.root / 'translated-publication.json'

    @property
    def preview_publication(self) -> Path:
        return self.root / 'preview-publication.json'

    @property
    def preview_report(self) -> Path:
        return self.root / 'preview-report.json'

    @property
    def publish_result(self) -> Path:
        return self.root / 'publish-result.json'

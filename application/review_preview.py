from __future__ import annotations

import json

from application.ports import ReviewPreviewPort
from application.publish_review import ReviewPublisher
from domain.models import Finding, PreparedReviewPublication
from domain.review.preview import PreviewItem, PreviewSessionState
from domain.workspace import WorkspacePaths
from runtime.async_ops import AsyncPathIO


class ReviewPreviewService:
    @classmethod
    async def preview_publication(
        cls,
        paths: WorkspacePaths,
        publication: PreparedReviewPublication,
        *,
        enabled: bool,
        previewer: ReviewPreviewPort | None,
    ) -> tuple[PreparedReviewPublication, dict[str, object]]:
        existing = await cls._load_existing_preview(paths)
        if existing is not None:
            return existing

        items = cls._preview_items(publication)

        if not enabled:
            return await cls._write_passthrough(
                paths,
                publication,
                status='disabled',
                item_count=len(items),
            )

        if not items:
            return await cls._write_passthrough(
                paths,
                publication,
                status='skipped-no-findings',
                item_count=0,
            )

        if previewer is None:
            raise RuntimeError('Preview mode is enabled but no previewer is configured.')

        session = PreviewSessionState(items=tuple(items))
        result = await previewer.preview(session)
        final_items = list(result.items)
        previewed_publication = cls._publication_from_items(publication, final_items)
        edited_count = cls._edited_count(items, final_items)
        unpublished_count = sum(1 for item in final_items if not item.publish)
        await AsyncPathIO.write_json(paths.preview_publication, previewed_publication, indent=2)
        report = {
            'status': 'reviewed',
            'items': len(final_items),
            'edited': edited_count,
            'unpublished': unpublished_count,
            'artifact_path': str(paths.preview_publication),
        }
        await AsyncPathIO.write_json(paths.preview_report, report, indent=2)
        return previewed_publication, {
            **report,
            'report_path': str(paths.preview_report),
        }

    @classmethod
    async def _write_passthrough(
        cls,
        paths: WorkspacePaths,
        publication: PreparedReviewPublication,
        *,
        status: str,
        item_count: int,
    ) -> tuple[PreparedReviewPublication, dict[str, object]]:
        await AsyncPathIO.write_json(paths.preview_publication, publication, indent=2)
        report = {
            'status': status,
            'items': item_count,
            'edited': 0,
            'unpublished': 0,
            'artifact_path': str(paths.preview_publication),
        }
        await AsyncPathIO.write_json(paths.preview_report, report, indent=2)
        return publication, {
            **report,
            'report_path': str(paths.preview_report),
        }

    @classmethod
    async def _load_existing_preview(
        cls,
        paths: WorkspacePaths,
    ) -> tuple[PreparedReviewPublication, dict[str, object]] | None:
        if not await AsyncPathIO.exists(paths.preview_publication):
            return None
        if not await AsyncPathIO.exists(paths.preview_report):
            return None
        try:
            publication = await AsyncPathIO.read_json(paths.preview_publication)
            report = await AsyncPathIO.read_json(paths.preview_report)
        except Exception:
            return None
        if not isinstance(publication, dict) or not isinstance(report, dict):
            return None
        if not isinstance(report.get('status'), str):
            return None
        return publication, {
            **report,
            'report_path': str(paths.preview_report),
        }

    @classmethod
    def _preview_items(cls, publication: PreparedReviewPublication) -> list[PreviewItem]:
        items: list[PreviewItem] = []

        for source_index, finding in enumerate(publication.get('findings', [])):
            items.append(
                cls._build_preview_item(
                    finding,
                    index=len(items) + 1,
                    target='inline',
                    source_index=source_index,
                )
            )

        for source_index, finding in enumerate(publication.get('out_of_hunk_findings', [])):
            items.append(
                cls._build_preview_item(
                    finding,
                    index=len(items) + 1,
                    target='summary-only',
                    source_index=source_index,
                )
            )
        return items

    @classmethod
    def _build_preview_item(
        cls,
        finding: Finding,
        *,
        index: int,
        target: str,
        source_index: int,
    ) -> PreviewItem:
        anchor = finding.get('anchor', {})
        line = anchor.get('new_line')
        if line is None:
            line = anchor.get('old_line')
        file_path = anchor.get('new_path') or anchor.get('old_path') or '?'
        return PreviewItem(
            index=index,
            target=target,
            severity=str(finding.get('severity', 'Suggestion')),
            file_path=str(file_path),
            line=line if isinstance(line, int) else None,
            short_title=str(finding.get('short_title', '')),
            body=str(finding.get('body', '')),
            publish=True,
            source_index=source_index,
        )

    @classmethod
    def _publication_from_items(
        cls,
        publication: PreparedReviewPublication,
        items: list[PreviewItem],
    ) -> PreparedReviewPublication:
        inline: list[Finding] = []
        out_of_hunk: list[Finding] = []

        findings = cls._copy_findings(publication.get('findings', []))
        summary_only = cls._copy_findings(publication.get('out_of_hunk_findings', []))

        for item in items:
            target_list = findings if item.target == 'inline' else summary_only
            finding = target_list[item.source_index]
            finding['short_title'] = item.short_title
            finding['body'] = item.body
            if not item.publish:
                continue
            if item.target == 'inline':
                inline.append(finding)
            else:
                out_of_hunk.append(finding)

        return ReviewPublisher.rebuild_publication(
            publication,
            findings=inline,
            out_of_hunk_findings=out_of_hunk,
        )

    @staticmethod
    def _copy_findings(findings: list[Finding]) -> list[Finding]:
        return json.loads(json.dumps(findings))

    @staticmethod
    def _edited_count(original: list[PreviewItem], final: list[PreviewItem]) -> int:
        return sum(
            1
            for before, after in zip(original, final)
            if before.short_title != after.short_title or before.body != after.body
        )

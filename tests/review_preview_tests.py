from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from application.publish_review import ReviewPublisher
from application.review_preview import ReviewPreviewService
from domain.review.preview import PreviewSessionResult
from domain.workspace import WorkspacePaths


class ReviewPreviewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_disabled_writes_passthrough_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication()

            previewed, summary = await ReviewPreviewService.preview_publication(
                paths=paths,
                publication=publication,
                enabled=False,
                previewer=None,
            )

            self.assertEqual(summary['status'], 'disabled')
            self.assertEqual(summary['edited'], 0)
            self.assertEqual(previewed, publication)
            self.assertEqual(json.loads(paths.preview_publication.read_text()), publication)

    async def test_preview_skips_when_there_are_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(include_inline=False, include_out_of_hunk=False)

            previewed, summary = await ReviewPreviewService.preview_publication(
                paths=paths,
                publication=publication,
                enabled=True,
                previewer=_UnexpectedPreviewer(),
            )

            self.assertEqual(summary['status'], 'skipped-no-findings')
            self.assertEqual(summary['items'], 0)
            self.assertEqual(previewed, publication)

    async def test_preview_applies_edited_inline_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication()

            previewed, summary = await ReviewPreviewService.preview_publication(
                paths=paths,
                publication=publication,
                enabled=True,
                previewer=_EditingPreviewer(),
            )

            self.assertEqual(summary['status'], 'reviewed')
            self.assertEqual(summary['edited'], 1)
            self.assertEqual(previewed['findings'][0]['short_title'], 'Edited title')
            self.assertEqual(previewed['findings'][0]['body'], 'Edited body')

    async def test_preview_can_unpublish_inline_finding_and_recompute_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(include_out_of_hunk=False)

            previewed, summary = await ReviewPreviewService.preview_publication(
                paths=paths,
                publication=publication,
                enabled=True,
                previewer=_UnpublishingInlinePreviewer(),
            )

            self.assertEqual(summary['unpublished'], 1)
            self.assertEqual(previewed['findings'], [])
            self.assertEqual(previewed['counts'], {})
            self.assertEqual(previewed['verdict'], 'approve')

    async def test_preview_can_unpublish_summary_only_finding_without_changing_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication()

            previewed, summary = await ReviewPreviewService.preview_publication(
                paths=paths,
                publication=publication,
                enabled=True,
                previewer=_UnpublishingSummaryPreviewer(),
            )

            self.assertEqual(summary['unpublished'], 1)
            self.assertEqual(len(previewed['findings']), 1)
            self.assertEqual(previewed['counts'], {'Major': 1})
            self.assertEqual(previewed['verdict'], 'request changes')
            self.assertEqual(previewed['out_of_hunk_findings'], [])

    @staticmethod
    def _publication(
        *,
        include_inline: bool = True,
        include_out_of_hunk: bool = True,
    ) -> dict:
        findings = []
        out_of_hunk = []
        if include_inline:
            findings.append(
                {
                    'severity': 'Major',
                    'short_title': 'Original title',
                    'body': 'Original body',
                    'anchor': {
                        'old_path': 'src/app.py',
                        'new_path': 'src/app.py',
                        'new_line': 10,
                        'old_line': None,
                    },
                }
            )
        if include_out_of_hunk:
            out_of_hunk.append(
                {
                    'severity': 'Minor',
                    'short_title': 'Summary-only title',
                    'body': 'Summary-only body',
                    'anchor': {
                        'old_path': 'src/app.py',
                        'new_path': 'src/app.py',
                        'new_line': 99,
                        'old_line': None,
                    },
                }
            )
        return ReviewPublisher.rebuild_publication(
            {
                'summary_labels': ReviewPublisher.default_summary_labels(),
                'eligible_file_count': 1,
                'hunk_count': 1,
            },
            findings=findings,
            out_of_hunk_findings=out_of_hunk,
        )


class _UnexpectedPreviewer:
    async def preview(self, session) -> PreviewSessionResult:
        raise AssertionError('Previewer should not be called.')


class _EditingPreviewer:
    async def preview(self, session) -> PreviewSessionResult:
        first = replace(session.items[0], short_title='Edited title', body='Edited body')
        return PreviewSessionResult(items=(first, *session.items[1:]))


class _UnpublishingInlinePreviewer:
    async def preview(self, session) -> PreviewSessionResult:
        first = replace(session.items[0], publish=False)
        return PreviewSessionResult(items=(first,))


class _UnpublishingSummaryPreviewer:
    async def preview(self, session) -> PreviewSessionResult:
        second = replace(session.items[1], publish=False)
        return PreviewSessionResult(items=(session.items[0], second))


if __name__ == '__main__':
    unittest.main()

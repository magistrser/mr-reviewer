from __future__ import annotations

import unittest
from pathlib import Path

from application.publish_review import ReviewPublisher


class ReviewPublisherTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_text_for_gitlab_decodes_escaped_newlines(self) -> None:
        text = 'Line one\\n\\n```rust\\nlet value = 1;\\n```'

        normalized = ReviewPublisher.normalize_text_for_gitlab(text)

        self.assertEqual(normalized, 'Line one\n\n```rust\nlet value = 1;\n```')

    async def test_post_inline_findings_uses_normalized_body(self) -> None:
        client = _FakeGitLabClient()
        findings = [
            {
                'body': 'First line\\n\\nSecond line',
                'anchor': {
                    'old_path': 'src/file.rs',
                    'new_path': 'src/file.rs',
                    'new_line': 7,
                    'old_line': None,
                },
            }
        ]

        posts, anchor_failures = await ReviewPublisher.post_inline_findings(
            client=client,
            findings=findings,
            project_path='group/project',
            mr_iid='42',
            base_sha='base',
            start_sha='start',
            head_sha='head',
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(anchor_failures, [])
        self.assertEqual(client.payloads[0]['body'], 'First line\n\nSecond line')

    def test_prepare_publication_partitions_out_of_hunk_findings(self) -> None:
        publication = ReviewPublisher.prepare_publication(
            findings=[
                {
                    'severity': 'Major',
                    'short_title': 'In hunk',
                    'body': 'Body',
                    'anchor': {
                        'old_path': 'src/file.py',
                        'new_path': 'src/file.py',
                        'new_line': 10,
                        'old_line': None,
                    },
                },
                {
                    'severity': 'Minor',
                    'short_title': 'Out of hunk',
                    'body': 'Body',
                    'anchor': {
                        'old_path': 'src/file.py',
                        'new_path': 'src/file.py',
                        'new_line': 30,
                        'old_line': None,
                    },
                },
            ],
            files_data=[
                {
                    'new_path': 'src/file.py',
                    'hunks': [{'new_start': 8, 'new_count': 5, 'old_start': 8, 'old_count': 5}],
                }
            ],
        )

        self.assertEqual(len(publication['findings']), 1)
        self.assertEqual(len(publication['out_of_hunk_findings']), 1)
        self.assertEqual(publication['counts']['Major'], 1)
        self.assertEqual(publication['eligible_file_count'], 1)
        self.assertEqual(publication['hunk_count'], 1)
        self.assertEqual(publication['verdict'], 'request changes')

    async def test_publish_prepared_review_uses_translated_summary_labels(self) -> None:
        client = _TranslatedSummaryGitLabClient()
        publication = {
            'findings': [
                {
                    'severity': 'Major',
                    'short_title': 'Translated highlight',
                    'body': 'Translated body',
                    'anchor': {
                        'old_path': 'src/file.py',
                        'new_path': 'src/file.py',
                        'new_line': 7,
                        'old_line': None,
                    },
                }
            ],
            'out_of_hunk_findings': [
                {
                    'severity': 'Minor',
                    'short_title': 'Translated out of hunk',
                    'body': 'Translated body',
                    'anchor': {
                        'old_path': 'src/file.py',
                        'new_path': 'src/file.py',
                        'new_line': 99,
                        'old_line': None,
                    },
                }
            ],
            'counts': {
                'Major': 1,
            },
            'verdict': 'request changes',
            'eligible_file_count': 1,
            'hunk_count': 1,
            'summary_labels': {
                'title': 'Resumen automatizado',
                'verdict_label': 'Veredicto:',
                'scope_reviewed_label': 'Alcance revisado:',
                'scope_reviewed_template': '{files} archivos modificados, {hunks} bloques.',
                'severity_counts_heading': 'Conteo por severidad',
                'severity_column': 'Severidad',
                'count_column': 'Cantidad',
                'highlights_heading': 'Hallazgos clave',
                'no_findings': '_Sin hallazgos._',
                'anchor_failures_heading': 'Hallazgos que no se pudieron anclar',
                'out_of_hunk_heading': 'Hallazgos fuera del diff',
                'footer': '_Resumen traducido._',
                'severity_labels': {
                    'Critical': 'Crítico',
                    'Major': 'Mayor',
                    'Minor': 'Menor',
                    'Suggestion': 'Sugerencia',
                },
                'verdict_values': {
                    'request changes': 'solicitar cambios',
                    'comment': 'comentario',
                    'approve': 'aprobar',
                },
            },
        }

        summary = await ReviewPublisher.publish_prepared_review(
            client=client,
            publication=publication,
            project_path='group/project',
            mr_iid='42',
            base_sha='base',
            start_sha='start',
            head_sha='head',
            result_path=Path('/tmp/test-publish-review.json'),
        )

        self.assertEqual(summary['posted'], 0)
        self.assertEqual(summary['anchor_errors'], 1)
        note_body = client.note_payloads[0]['body']
        self.assertIn('## Resumen automatizado', note_body)
        self.assertIn('**Veredicto:** solicitar cambios', note_body)
        self.assertIn('### Hallazgos que no se pudieron anclar', note_body)
        self.assertIn('### Hallazgos fuera del diff', note_body)
        self.assertIn('Translated highlight', note_body)
        self.assertIn('Translated out of hunk', note_body)


class _FakeGitLabClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def proj_url(self, project_id: str | int, *segments: str) -> str:
        return f'https://gitlab.example.com/{project_id}/' + '/'.join(segments)

    async def post(self, url: str, payload: dict) -> dict:
        self.payloads.append(payload)
        return {'ok': True, 'data': {'id': 123}}


class _TranslatedSummaryGitLabClient:
    def __init__(self) -> None:
        self.discussion_payloads: list[dict] = []
        self.note_payloads: list[dict] = []

    def proj_url(self, project_id: str | int, *segments: str) -> str:
        return f'https://gitlab.example.com/{project_id}/' + '/'.join(segments)

    async def post(self, url: str, payload: dict) -> dict:
        if url.endswith('/discussions'):
            self.discussion_payloads.append(payload)
            return {'ok': False, 'error': 'anchor failed'}
        self.note_payloads.append(payload)
        return {'ok': True, 'data': {'id': 456}}


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from application.publish_review import ReviewPublisher
from application.review_translation import ReviewTranslationService
from domain.workspace import WorkspacePaths


class ReviewTranslationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_translate_publication_skips_eng_and_writes_passthrough_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(findings_count=1)

            translated_publication, summary = await ReviewTranslationService.translate_publication(
                paths=paths,
                agent=_UnexpectedAgent(),
                model='test-model',
                publication=publication,
                target_language='  eng  ',
                parallel_reviews=2,
            )

            self.assertEqual(summary['status'], 'skipped')
            self.assertEqual(summary['language'], 'ENG')
            self.assertEqual(summary['jobs'], 0)
            self.assertEqual(translated_publication, publication)
            self.assertTrue(paths.translated_publication.exists())
            self.assertEqual(json.loads(paths.translated_publication.read_text()), publication)
            report = json.loads(paths.translation_report.read_text())
            self.assertEqual(report['status'], 'skipped')

    async def test_translate_publication_runs_summary_and_findings_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(findings_count=2)
            agent = _TranslatingAgent(delay=0.02)
            output = _RecordingOutput()

            translated_publication, summary = await ReviewTranslationService.translate_publication(
                paths=paths,
                agent=agent,
                model='test-model',
                publication=publication,
                target_language='RUS',
                parallel_reviews=2,
                output=output,
            )

            self.assertEqual(summary['status'], 'translated')
            self.assertEqual(summary['language'], 'RUS')
            self.assertEqual(summary['jobs'], 3)
            self.assertEqual(summary['parallel_reviews'], 2)
            self.assertEqual(agent.max_in_flight, 2)
            self.assertEqual(translated_publication['summary_labels']['title'], 'RUS:Automated review summary')
            self.assertEqual(translated_publication['findings'][0]['short_title'], 'RUS:First title')
            self.assertEqual(translated_publication['findings'][1]['body'], 'RUS:Second body')
            self.assertTrue(paths.translated_publication.exists())
            report = json.loads(paths.translation_report.read_text())
            self.assertEqual(len(report['job_reports']), 3)
            self.assertTrue(output.progress_updates)
            self.assertEqual(output.progress_updates[-1].parallel_limit, 2)

    async def test_translate_publication_raises_when_result_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(findings_count=1)

            with self.assertRaisesRegex(RuntimeError, 'translation agent did not write'):
                await ReviewTranslationService.translate_publication(
                    paths=paths,
                    agent=_MissingFindingResultAgent(),
                    model='test-model',
                    publication=publication,
                    target_language='DE',
                    parallel_reviews=1,
                )

    async def test_translate_publication_reuses_existing_job_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = WorkspacePaths(root=Path(tmp))
            publication = self._publication(findings_count=1)
            paths.translation_results_dir.mkdir(parents=True)
            (paths.translation_results_dir / '000-summary-labels.json').write_text(
                json.dumps({'summary_labels': publication['summary_labels']})
            )
            (paths.translation_results_dir / '001-finding.json').write_text(
                json.dumps({'short_title': 'cached title', 'body': 'cached body'})
            )

            translated_publication, summary = await ReviewTranslationService.translate_publication(
                paths=paths,
                agent=_UnexpectedAgent(),
                model='test-model',
                publication=publication,
                target_language='RUS',
                parallel_reviews=2,
            )

            self.assertEqual(summary['status'], 'translated')
            self.assertEqual(summary['jobs'], 2)
            self.assertEqual(translated_publication['findings'][0]['short_title'], 'cached title')
            self.assertEqual(translated_publication['findings'][0]['body'], 'cached body')

    @staticmethod
    def _publication(findings_count: int) -> dict:
        findings = []
        for index in range(findings_count):
            findings.append(
                {
                    'severity': 'Major',
                    'confidence': 'High',
                    'rule_ids': ['CS-CORE-001'],
                    'short_title': 'First title' if index == 0 else 'Second title',
                    'anchor': {
                        'old_path': f'src/file_{index}.py',
                        'new_path': f'src/file_{index}.py',
                        'new_line': index + 10,
                        'old_line': None,
                    },
                    'body': 'First body' if index == 0 else 'Second body',
                }
            )
        return {
            'findings': findings,
            'out_of_hunk_findings': [],
            'counts': {'Major': findings_count},
            'verdict': 'request changes',
            'eligible_file_count': findings_count,
            'hunk_count': findings_count,
            'summary_labels': ReviewPublisher.default_summary_labels(),
        }


class _UnexpectedAgent:
    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        raise AssertionError('translation agent should not be called when language is ENG')


class _TranslatingAgent:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(self.delay)
        payload = json.loads(Path(prompt).read_text())
        result_path = Path(payload['result_path'])
        if payload['kind'] == 'summary_labels':
            labels = payload['summary_labels']
            translated = {}
            for key, value in labels.items():
                if isinstance(value, dict):
                    translated[key] = {
                        nested_key: f"{payload['target_language']}:{nested_value}"
                        for nested_key, nested_value in value.items()
                    }
                else:
                    translated[key] = f"{payload['target_language']}:{value}"
            result_path.write_text(json.dumps({'summary_labels': translated}))
        else:
            finding = payload['finding']
            result_path.write_text(
                json.dumps(
                    {
                        'short_title': f"{payload['target_language']}:{finding['short_title']}",
                        'body': f"{payload['target_language']}:{finding['body']}",
                    }
                )
            )
        self.in_flight -= 1
        return ''


class _MissingFindingResultAgent:
    async def default_model(self) -> str:
        return 'test-model'

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        payload = json.loads(Path(prompt).read_text())
        if payload['kind'] == 'summary_labels':
            Path(payload['result_path']).write_text(json.dumps({'summary_labels': payload['summary_labels']}))
        return ''


class _RecordingOutput:
    def __init__(self) -> None:
        self.progress_updates = []

    def task_progress(self, progress) -> None:
        self.progress_updates.append(progress)


if __name__ == '__main__':
    unittest.main()

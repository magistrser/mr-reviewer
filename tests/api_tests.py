from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from application.dto import ReviewResult
from application.review_jobs import ReviewJobError, ReviewJobRequest, ReviewJobSnapshot
from infrastructure.api.jobs import BackgroundReviewJobRunner
from infrastructure.endpoints.v1.reviews import set_job_runner
from infrastructure.workspace.setup import WorkspaceBuilder
from main import app
from settings import ApiSettings, Settings


class ApiEndpointTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_job_runner(None)

    def test_health_check(self) -> None:
        with TestClient(app) as client:
            response = client.get('/health')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, 'Ok')

    def test_metrics(self) -> None:
        with TestClient(app) as client:
            response = client.get('/metrics')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response.headers['content-type'])

    def test_create_review_job_returns_status_url(self) -> None:
        fake_runner = _FakeJobRunner(
            ReviewJobSnapshot(
                job_id='job-1',
                status='queued',
                request=ReviewJobRequest(
                    mr_url='https://gitlab.example.com/group/project/-/merge_requests/42',
                    model='model-a',
                ),
            )
        )
        set_job_runner(fake_runner)

        with TestClient(app) as client:
            response = client.post(
                '/api/v1/reviews',
                json={
                    'mr_url': 'https://gitlab.example.com/group/project/-/merge_requests/42',
                    'model': 'model-a',
                },
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload['job_id'], 'job-1')
        self.assertEqual(payload['status'], 'queued')
        self.assertTrue(payload['status_url'].endswith('/api/v1/reviews/job-1'))

    def test_get_review_job_returns_success_payload(self) -> None:
        snapshot = ReviewJobSnapshot(
            job_id='job-2',
            status='succeeded',
            request=ReviewJobRequest(mr_url='https://gitlab.example.com/group/project/-/merge_requests/42'),
            progress={'kind': 'job', 'message': 'Review job completed'},
            warnings=('example warning',),
            result={'mr_iid': '42', 'verdict': 'comment'},
        )
        set_job_runner(_FakeJobRunner(snapshot))

        with TestClient(app) as client:
            response = client.get('/api/v1/reviews/job-2')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'succeeded')
        self.assertEqual(payload['result']['verdict'], 'comment')
        self.assertEqual(payload['warnings'], ['example warning'])

    def test_get_review_job_returns_structured_error(self) -> None:
        snapshot = ReviewJobSnapshot(
            job_id='job-3',
            status='failed',
            request=ReviewJobRequest(mr_url='https://gitlab.example.com/group/project/-/merge_requests/42'),
            error=ReviewJobError(type='ValueError', message='bad request'),
        )
        set_job_runner(_FakeJobRunner(snapshot))

        with TestClient(app) as client:
            response = client.get('/api/v1/reviews/job-3')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['error'], {'type': 'ValueError', 'message': 'bad request'})

    def test_create_review_job_validates_payload(self) -> None:
        with TestClient(app) as client:
            response = client.post('/api/v1/reviews', json={'mr_url': ''})

        self.assertEqual(response.status_code, 422)


class ApiCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_runner_uses_shared_use_case_with_api_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_factory = _FakeApplicationFactory()
            runner = BackgroundReviewJobRunner(
                Settings(api=ApiSettings(review_root=Path(tmp), max_concurrent_jobs=1)),
                factory=fake_factory,
            )

            snapshot = runner.enqueue(
                ReviewJobRequest(
                    mr_url='https://gitlab.example.com/group/project/-/merge_requests/42',
                    model='model-a',
                )
            )
            for _ in range(50):
                current = runner.get(snapshot.job_id)
                if current is not None and current.status == 'succeeded':
                    break
                await asyncio.sleep(0.01)

            current = runner.get(snapshot.job_id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, 'succeeded')
        self.assertEqual(current.result['model'], 'model-a')
        self.assertIs(
            fake_factory.workspace_provisioner.__func__,
            WorkspaceBuilder.setup_workspace_job_safe.__func__,
        )
        self.assertEqual(fake_factory.use_case.options.preview_mode, False)


class _FakeJobRunner:
    def __init__(self, snapshot: ReviewJobSnapshot) -> None:
        self._snapshot = snapshot

    def enqueue(self, request: ReviewJobRequest) -> ReviewJobSnapshot:
        return self._snapshot

    def get(self, job_id: str) -> ReviewJobSnapshot | None:
        if job_id == self._snapshot.job_id:
            return self._snapshot
        return None


class _FakeApplicationFactory:
    def __init__(self) -> None:
        self.workspace_provisioner: Any = None
        self.use_case = _FakeUseCase()

    async def build_use_case(self, **kwargs: Any) -> Any:
        self.workspace_provisioner = kwargs['workspace_provisioner']
        return self.use_case


class _FakeUseCase:
    def __init__(self) -> None:
        self.options: Any = None

    async def execute(self, mr_url: str, options: Any) -> ReviewResult:
        self.options = options
        return ReviewResult(
            mr_url=mr_url,
            project_path='group/project',
            mr_iid='42',
            workspace=Path('/tmp/workspace'),
            files_changed=1,
            open_discussions=0,
            eligible_files=1,
            review_passes=1,
            cluster_reviews=0,
            findings_kept=0,
            invalid_findings=0,
            posted=0,
            anchor_errors=0,
            out_of_hunk_findings=0,
            verdict='clean',
            summary_status='skipped',
            translation_status='skipped',
            translation_language='ENG',
            preview_status='skipped',
            preview_edited=0,
            preview_unpublished=0,
            overall_comment_status='ok',
            overall_note_id=None,
            result_saved_to='/tmp/publish.json',
            quality_report_path='/tmp/quality.json',
            preview_report_path='/tmp/preview.json',
            model=options.model or '',
            warnings=(),
        )


if __name__ == '__main__':
    unittest.main()

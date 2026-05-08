from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

from application.review_jobs import ReviewJobRequest, ReviewJobSnapshot
from infrastructure.api.jobs import BackgroundReviewJobRunner
from infrastructure.endpoints.v1.router import router
from settings import settings


class CreateReviewRequest(BaseModel):
    mr_url: str = Field(min_length=1)
    model: str | None = None


class CreateReviewResponse(BaseModel):
    job_id: str
    status: str
    status_url: str


_job_runner: BackgroundReviewJobRunner | None = None


def get_job_runner() -> BackgroundReviewJobRunner:
    global _job_runner
    if _job_runner is None:
        _job_runner = BackgroundReviewJobRunner(settings)
    return _job_runner


def set_job_runner(job_runner: BackgroundReviewJobRunner | None) -> None:
    global _job_runner
    _job_runner = job_runner


@router.post(
    path='/reviews',
    name='Create review job',
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateReviewResponse,
)
async def create_review_job(payload: CreateReviewRequest, request: Request) -> CreateReviewResponse:
    snapshot = get_job_runner().enqueue(
        ReviewJobRequest(
            mr_url=payload.mr_url,
            model=payload.model,
        )
    )
    return CreateReviewResponse(
        job_id=snapshot.job_id,
        status=snapshot.status,
        status_url=str(request.url_for('get_review_job', job_id=snapshot.job_id)),
    )


@router.get(
    path='/reviews/{job_id}',
    name='get_review_job',
)
async def get_review_job(job_id: str) -> dict[str, Any]:
    snapshot = get_job_runner().get(job_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Review job not found')
    return _snapshot_response(snapshot)


def _snapshot_response(snapshot: ReviewJobSnapshot) -> dict[str, Any]:
    return {
        'job_id': snapshot.job_id,
        'status': snapshot.status,
        'request': {
            'mr_url': snapshot.request.mr_url,
            'model': snapshot.request.model,
        },
        'progress': snapshot.progress,
        'warnings': list(snapshot.warnings),
        'result': snapshot.result,
        'error': (
            {
                'type': snapshot.error.type,
                'message': snapshot.error.message,
            }
            if snapshot.error else None
        ),
    }

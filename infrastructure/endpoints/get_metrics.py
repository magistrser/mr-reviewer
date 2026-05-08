from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from infrastructure.endpoints.router import router
from infrastructure.metrics import prometheus_registry


@router.get(
    path='/metrics',
    name='Prometheus metrics',
    description='Exposes internal service metrics for Prometheus monitoring',
)
async def get_metrics() -> Response:
    return Response(
        content=generate_latest(prometheus_registry),
        headers={'Content-Type': CONTENT_TYPE_LATEST},
    )

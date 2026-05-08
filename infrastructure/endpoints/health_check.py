from fastapi import Response, status

from infrastructure.endpoints.router import router


@router.get(
    path='/health',
    name='Health Check',
    description='Health Check',
)
async def health_check_handler() -> Response:
    return Response(content='Ok', headers={'Content-Type': 'text/plain'}, status_code=status.HTTP_200_OK)

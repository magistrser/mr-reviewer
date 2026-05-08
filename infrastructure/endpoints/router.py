from fastapi import APIRouter

from infrastructure.endpoints.v1 import v1_router


router = APIRouter()
router.include_router(v1_router, prefix='/api')

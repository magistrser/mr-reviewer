from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from infrastructure.endpoints import root_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[dict]:
    yield {}


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:8000'],
    allow_methods=['*'],
    allow_headers=['*'],
)
app.include_router(root_router)

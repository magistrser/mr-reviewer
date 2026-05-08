from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal


ReviewJobStatus = Literal['queued', 'running', 'succeeded', 'failed']


@dataclass(frozen=True)
class ReviewJobRequest:
    mr_url: str
    model: str | None = None


@dataclass(frozen=True)
class ReviewJobError:
    type: str
    message: str


@dataclass(frozen=True)
class ReviewJobSnapshot:
    job_id: str
    status: ReviewJobStatus
    request: ReviewJobRequest
    progress: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    result: dict[str, Any] | None = None
    error: ReviewJobError | None = None


class ReviewResultSerializer:
    @classmethod
    def to_jsonable(cls, value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                key: cls.to_jsonable(item)
                for key, item in asdict(value).items()
            }
        if isinstance(value, dict):
            return {
                str(key): cls.to_jsonable(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls.to_jsonable(item) for item in value]
        return value

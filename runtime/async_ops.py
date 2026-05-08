from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class AsyncPathIO:
    @staticmethod
    async def read_text(path: Path, errors: str | None = None) -> str:
        def _read() -> str:
            kwargs = {'errors': errors} if errors is not None else {}
            return path.read_text(**kwargs)
        return await asyncio.to_thread(_read)

    @staticmethod
    async def read_bytes(path: Path) -> bytes:
        return await asyncio.to_thread(path.read_bytes)

    @staticmethod
    async def write_text(path: Path, content: str) -> int:
        return await asyncio.to_thread(path.write_text, content)

    @classmethod
    async def read_json(cls, path: Path) -> Any:
        return json.loads(await cls.read_text(path))

    @classmethod
    async def write_json(cls, path: Path, data: Any, **kwargs: Any) -> int:
        return await cls.write_text(path, json.dumps(data, **kwargs))

    @staticmethod
    async def exists(path: Path) -> bool:
        return await asyncio.to_thread(path.exists)

    @staticmethod
    async def is_dir(path: Path) -> bool:
        return await asyncio.to_thread(path.is_dir)

    @staticmethod
    async def mkdir(path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        await asyncio.to_thread(path.mkdir, parents=parents, exist_ok=exist_ok)

    @staticmethod
    async def iterdir(path: Path) -> list[Path]:
        return await asyncio.to_thread(lambda: list(path.iterdir()))

    @staticmethod
    async def glob(pattern: str) -> list[str]:
        return await asyncio.to_thread(glob.glob, pattern)

    @staticmethod
    async def remove(path: Path) -> None:
        await asyncio.to_thread(path.unlink)

    @staticmethod
    async def rename(src: Path, dst: Path) -> None:
        await asyncio.to_thread(src.rename, dst)

    @staticmethod
    async def rmtree(path: Path, ignore_errors: bool = False) -> None:
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=ignore_errors)

    @staticmethod
    async def makedirs(path: Path, exist_ok: bool = False) -> None:
        await asyncio.to_thread(os.makedirs, path, exist_ok=exist_ok)

    @staticmethod
    async def stat(path: Path) -> os.stat_result:
        return await asyncio.to_thread(path.stat)


class AsyncCommandRunner:
    @staticmethod
    async def run(cmd: list[str], cwd: Path | None = None) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode(),
            stderr=stderr_bytes.decode(),
        )

    @classmethod
    async def run_checked(cls, cmd: list[str], cwd: Path | None = None) -> CommandResult:
        result = await cls.run(cmd, cwd=cwd)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f'exit {result.returncode}')
        return result

    @staticmethod
    async def run_shell(command: str, cwd: Path | None = None) -> CommandResult:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode(),
            stderr=stderr_bytes.decode(),
        )

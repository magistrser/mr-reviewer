from __future__ import annotations

import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGES = ('application', 'domain', 'infrastructure', 'runtime', 'resources')


def _remove_source_bytecode() -> None:
    for package_name in SOURCE_PACKAGES:
        package_path = PROJECT_ROOT / package_name
        if not package_path.exists():
            continue
        for cache_dir in package_path.rglob('__pycache__'):
            shutil.rmtree(cache_dir, ignore_errors=True)
        for pyc_file in package_path.rglob('*.pyc'):
            pyc_file.unlink(missing_ok=True)


def pytest_sessionstart(session: object) -> None:
    sys.dont_write_bytecode = True
    _remove_source_bytecode()


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    _remove_source_bytecode()

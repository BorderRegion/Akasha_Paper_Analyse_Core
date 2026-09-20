"""P07 integration fixtures shared by the module's test files."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"

"""Pytest projects also own any runtime caches generated beside them."""
from pathlib import Path

import pytest

from _scratch import temporary_project


@pytest.fixture
def scratch_project():
    with temporary_project(prefix="pytest_") as directory:
        yield Path(directory)

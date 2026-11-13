"""Fixed sufficient host memory samples for offline voice worker tests."""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from src.core.host_memory import HostMemorySnapshot

SUFFICIENT_HOST_MEMORY = HostMemorySnapshot(
    physical_available_mb=65536,
    commit_available_mb=65536,
)

LOW_PHYSICAL_HOST_MEMORY = HostMemorySnapshot(
    physical_available_mb=1024,
    commit_available_mb=65536,
)

LOW_COMMIT_HOST_MEMORY = HostMemorySnapshot(
    physical_available_mb=65536,
    commit_available_mb=1024,
)


@contextmanager
def patch_sufficient_host_memory() -> Iterator[None]:
    with patch(
        "src.core.host_memory.read_host_memory_snapshot",
        return_value=SUFFICIENT_HOST_MEMORY,
    ):
        yield


def module_sufficient_host_memory_patch():
    """Bounded module fixture for ``unittest.enterModuleContext`` in setUpModule."""
    return patch(
        "src.core.host_memory.read_host_memory_snapshot",
        return_value=SUFFICIENT_HOST_MEMORY,
    )


def install_module_sufficient_host_memory() -> None:
    """Register module-level sufficient host memory (Python 3.11+)."""
    unittest.enterModuleContext(module_sufficient_host_memory_patch())

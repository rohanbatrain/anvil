"""Shared test configuration.

The degradation warnings the graph emits when the model is unavailable are
expected behaviour, not noise to be fixed -- but there are thousands of them in
a batch test, and they bury the actual failures. Silenced here rather than in
the modules, so production logging is untouched.
"""

from __future__ import annotations

import logging

import pytest
import structlog


@pytest.fixture(autouse=True, scope="session")
def _quiet_logs() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

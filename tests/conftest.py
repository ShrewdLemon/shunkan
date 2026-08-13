"""Test setup: force offline mode and isolate state BEFORE shunkan imports."""

import os
import tempfile

os.environ["SHUNKAN_OFFLINE"] = "1"
os.environ["SHUNKAN_HOME"] = tempfile.mkdtemp(prefix="shunkan-test-")

import pytest  # noqa: E402

from shunkan.data.provider import SyntheticProvider  # noqa: E402


@pytest.fixture
def provider() -> SyntheticProvider:
    return SyntheticProvider()


@pytest.fixture
def prices(provider):
    return provider.history("TEST", period="2y")

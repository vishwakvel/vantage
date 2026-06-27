"""Pytest configuration for Vantage tests.

Restricts anyio backend to asyncio only — trio is not installed.
"""

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param

"""Fixtures for the web tests (helpers live in tests/web/support.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from diffbell.web.app import create_app
from tests.web.support import BASE, Env, log_in, make_context, make_env


@pytest.fixture
def env(tmp_dir: Path) -> Env:
    return make_env(tmp_dir)


@pytest.fixture
async def anon(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no session."""
    transport = httpx.ASGITransport(app=create_app(make_context(env)))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


@pytest.fixture
async def client(env: Env, anon: httpx.AsyncClient) -> httpx.AsyncClient:
    """A logged-in client that sends the CSRF header."""
    await log_in(env, anon)
    return anon

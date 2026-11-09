"""Load the plugin as the package ``hermes_ussd`` (the repo directory name is not importable)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "hermes_ussd"


def _load() -> None:
    if PKG in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)


_load()


class Clock:
    """Controllable time for deterministic tests."""

    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    from hermes_ussd.store import Store

    conn = sqlite3.connect(tmp_path / "data.db", check_same_thread=False)
    s = Store(conn, clock=clock)
    s.migrate()
    yield s
    s.close()


SECRET = "s3cret_" + "A" * 30


@pytest.fixture
def settings():
    from hermes_ussd.settings import load_settings

    env = {"AT_USERNAME": "sandbox", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET,
           "AT_ALLOWED_USERS": "+254700000001,+254700000002"}
    return load_settings(env.get)


@pytest.fixture(scope="session", autouse=True)
def _platform_registered():
    """Make ``Platform("africastalking")`` resolve for every test, independent of test order."""
    try:
        from gateway.platform_registry import platform_registry
    except ImportError:
        yield
        return
    from fakectx import FakeCtx

    import hermes_ussd

    ctx = FakeCtx()
    hermes_ussd.register(ctx)
    platform_registry.register(ctx.entry)
    yield
    platform_registry.unregister("africastalking")

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sandbox mode at boot (#4622): the settings, the guards, the stored mode.

* **Settings** -- ``MEHO_SANDBOX_MODE`` and ``MEHO_SANDBOX_MODE_CONFIRM``
  are ``Settings`` fields read at boot; only ``settings.py`` reads the
  environment for them.
* **Boot refusals** -- the mode on without the confirm value; a confirm
  value that is not the host of the MCP resource URI; a host with no
  ``sandbox`` part; a stored mode that is not the setting, both ways.
  Each has a plain message.
* **Stored mode** -- a new database stores the mode at its first start;
  two starts with different settings, one after the other and at the same
  time on separate connections: one stores, the other refuses, and one
  mode remains stored; the mode is stored and checked before MEHO serves
  a request.
* **Normal boot** -- with the mode off and no sandbox settings the app
  boots and shows ``"off"``.
"""

from __future__ import annotations

import asyncio
import gc
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.db.engine import create_engine_for_url, get_sessionmaker
from meho_backplane.db.models import SandboxModeState
from meho_backplane.main import app
from meho_backplane.sandbox_mode import (
    SandboxModeConfigError,
    assert_sandbox_mode_consistent,
    check_sandbox_settings,
    sandbox_mode_active,
    store_or_check_sandbox_mode,
)
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import SANDBOX_HOST, turn_sandbox_mode_off, turn_sandbox_mode_on

_SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", f"https://{SANDBOX_HOST}")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.delenv("MEHO_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("MEHO_SANDBOX_MODE_CONFIRM", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**env: str) -> object:
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_off() -> None:
    settings = get_settings()
    assert settings.sandbox_mode is False
    assert settings.sandbox_mode_confirm == ""


def test_settings_read_both_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", f" {SANDBOX_HOST} ")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.sandbox_mode is True
    assert settings.sandbox_mode_confirm == SANDBOX_HOST


def test_only_settings_module_reads_the_environment() -> None:
    """``grep -rn "environ.*MEHO_SANDBOX_MODE" backend/src`` finds only settings.py."""
    pattern = re.compile(r"environ.*MEHO_SANDBOX_MODE")
    hits = sorted(
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert hits == ["settings.py"]


# ---------------------------------------------------------------------------
# Boot refusals: the settings guards (no database needed)
# ---------------------------------------------------------------------------


def test_mode_off_checks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", "garbage")
    check_sandbox_settings(_settings())  # must not raise


def test_refuses_mode_on_without_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    with pytest.raises(SandboxModeConfigError, match="MEHO_SANDBOX_MODE_CONFIRM is not set"):
        check_sandbox_settings(_settings())


def test_refuses_confirm_that_is_not_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", "other.sandbox.test")
    with pytest.raises(SandboxModeConfigError, match="is not the host name of this instance"):
        check_sandbox_settings(_settings())


@pytest.mark.parametrize("host", ["meho.example", "meho-sandbox.example", "sandboxes.example"])
def test_refuses_host_without_sandbox_part(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("BACKPLANE_URL", f"https://{host}")
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", host)
    with pytest.raises(SandboxModeConfigError, match="has no such part"):
        check_sandbox_settings(_settings())


@pytest.mark.parametrize("host", ["meho.sandbox.example", "sandbox.example", "a.b.sandbox.c"])
def test_accepts_host_with_sandbox_part(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("BACKPLANE_URL", f"https://{host}")
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", host)
    check_sandbox_settings(_settings())


def test_confirm_checks_the_mcp_resource_uri_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit MCP resource URI is the address the confirm value must match."""
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.example")
    monkeypatch.setenv("MCP_RESOURCE_URI", "https://meho.sandbox.example/api/mcp")
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", "meho.sandbox.example")
    check_sandbox_settings(_settings())


# ---------------------------------------------------------------------------
# The stored mode
# ---------------------------------------------------------------------------


async def _stored() -> bool | None:
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(SandboxModeState))).scalar_one_or_none()
    return None if row is None else row.sandbox_mode


@pytest.mark.asyncio
async def test_new_database_stores_the_mode_at_first_start(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _stored() is None
    turn_sandbox_mode_on(monkeypatch)
    assert await store_or_check_sandbox_mode(get_settings()) is True
    assert await _stored() is True
    # A second start with the same setting passes.
    assert await store_or_check_sandbox_mode(get_settings()) is True


@pytest.mark.asyncio
async def test_boot_write_passes_a_stale_cursor_from_an_earlier_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read cursor left in a reference cycle must not block the boot write.

    When the app stops, a background read cancelled half-way leaves its
    cursor alive inside the cancellation traceback, a reference cycle. A
    closed SQLite connection keeps its shared lock until that cursor is
    freed, so a test that starts the app twice in one process would fail
    at the first write of the second start. The cycle is built by hand
    here so the test does not depend on cancellation timing. Automatic
    garbage collection is off, so only the boot step itself can free it.
    """
    turn_sandbox_mode_off(monkeypatch)
    settings = get_settings()
    stale = sqlite3.connect(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    # Many rows, none fetched: the statement stays half-way and holds a shared lock.
    cursor = stale.execute("SELECT name FROM sqlite_master")
    cycle: list[object] = [cursor]
    cycle.append(cycle)
    del cursor, cycle
    stale.close()  # the lock stays until the cursor is freed
    gc.disable()
    try:
        assert await store_or_check_sandbox_mode(settings) is False
    finally:
        gc.enable()
    assert await _stored() is False


@pytest.mark.asyncio
async def test_two_starts_with_different_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first start stores its mode; the other refuses, both ways."""
    turn_sandbox_mode_off(monkeypatch)
    assert await store_or_check_sandbox_mode(get_settings()) is False
    turn_sandbox_mode_on(monkeypatch)
    with pytest.raises(SandboxModeConfigError, match="installed with sandbox mode off"):
        await store_or_check_sandbox_mode(get_settings())
    assert await _stored() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("first_on", [True, False])
async def test_two_starts_at_once_with_different_settings(
    monkeypatch: pytest.MonkeyPatch, first_on: bool
) -> None:
    """Two processes start at once on one new database, one with the mode on.

    Each start runs on its own engine, so on its own database connection.
    One stores its mode, the other refuses, and exactly one mode remains
    stored, whichever start was first.
    """
    turn_sandbox_mode_on(monkeypatch)
    on = get_settings()
    turn_sandbox_mode_off(monkeypatch)
    off = get_settings()
    assert on.sandbox_mode is True and off.sandbox_mode is False
    starts = (on, off) if first_on else (off, on)
    engines = [
        create_engine_for_url(off.database_url, pool_size=1, pool_timeout=5.0) for _ in starts
    ]
    try:
        results = await asyncio.gather(
            *(
                store_or_check_sandbox_mode(settings, engine=engine)
                for settings, engine in zip(starts, engines, strict=True)
            ),
            return_exceptions=True,
        )
    finally:
        for engine in engines:
            await engine.dispose()

    stored = [result for result in results if isinstance(result, bool)]
    refused = [result for result in results if isinstance(result, SandboxModeConfigError)]
    assert len(stored) == 1 and len(refused) == 1, results
    assert "The mode is fixed for the life of the database" in str(refused[0])
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(SandboxModeState))).scalars().all()
    assert len(rows) == 1
    assert rows[0].sandbox_mode is stored[0]


@pytest.mark.asyncio
async def test_stored_on_refuses_a_start_with_the_mode_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await store_or_check_sandbox_mode(get_settings())
    turn_sandbox_mode_off(monkeypatch)
    with pytest.raises(SandboxModeConfigError, match="installed with sandbox mode on"):
        await assert_sandbox_mode_consistent(get_settings())
    assert sandbox_mode_active() is False


@pytest.mark.asyncio
async def test_consistent_boot_sets_the_process_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    from meho_backplane.sandbox_mode import set_sandbox_mode_active

    set_sandbox_mode_active(False)
    await assert_sandbox_mode_consistent(get_settings())
    assert sandbox_mode_active() is True


# ---------------------------------------------------------------------------
# The app lifespan: stored before a request is served
# ---------------------------------------------------------------------------


def test_lifespan_stores_the_mode_and_serves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    from meho_backplane.sandbox_mode import set_sandbox_mode_active

    set_sandbox_mode_active(False)
    try:
        with TestClient(app) as client:
            ready = client.get("/ready")
            assert ready.json()["sandbox_mode"] == "on"
            assert sandbox_mode_active() is True
    finally:
        get_settings.cache_clear()
    import asyncio

    assert asyncio.run(_stored()) is True


def test_lifespan_refuses_a_bad_sandbox_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    get_settings.cache_clear()
    try:
        with (
            pytest.raises(SandboxModeConfigError, match="MEHO_SANDBOX_MODE_CONFIRM is not set"),
            TestClient(app),
        ):
            pass
    finally:
        get_settings.cache_clear()
    import asyncio

    # Refused before the first database step: nothing was stored.
    assert asyncio.run(_stored()) is None


def test_lifespan_refuses_a_database_installed_with_the_other_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    turn_sandbox_mode_off(monkeypatch)
    asyncio.run(store_or_check_sandbox_mode(get_settings()))
    turn_sandbox_mode_on(monkeypatch)
    try:
        with (
            pytest.raises(SandboxModeConfigError, match="installed with sandbox mode off"),
            TestClient(app),
        ):
            pass
    finally:
        get_settings.cache_clear()


def test_normal_boot_with_the_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_off(monkeypatch)
    try:
        with TestClient(app) as client:
            assert client.get("/ready").json()["sandbox_mode"] == "off"
    finally:
        get_settings.cache_clear()
    import asyncio

    assert asyncio.run(_stored()) is False

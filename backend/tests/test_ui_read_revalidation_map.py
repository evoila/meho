# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-path revalidation: the anchor map never lets a check be skipped.

These tests drive the real :mod:`meho_backplane.ui.auth.revalidation`
module with a fake clock and a fake token check. The token check
refuses one "disabled" session and accepts every other one. The
sessions are plain in-memory rows; no database, no IdP.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from meho_backplane.ui.auth import revalidation
from meho_backplane.ui.auth import session_ref as session_ref_module
from meho_backplane.ui.auth.session_store import DecryptedSession

_WINDOW_SECONDS = 300
_BOUND = revalidation._MAX_TRACKED_SESSIONS


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTokenCheck:
    """Refuses the disabled session, accepts every other one, counts calls."""

    def __init__(self, disabled: uuid.UUID) -> None:
        self.disabled = disabled
        self.calls: list[uuid.UUID] = []

    async def __call__(
        self, decrypted: DecryptedSession, *, expected_audience: str
    ) -> tuple[DecryptedSession, None]:
        self.calls.append(decrypted.id)
        if decrypted.id == self.disabled:
            raise HTTPException(status_code=401, detail="session_expired")
        return decrypted, None


def _session(session_id: uuid.UUID | None = None) -> DecryptedSession:
    now = datetime.now(UTC)
    return DecryptedSession(
        id=session_id or uuid.uuid4(),
        operator_sub="op-1",
        tenant_id=uuid.uuid4(),
        created_at=now,
        expires_at=now + timedelta(hours=1),
        access_token="made-up-access-token",
        refresh_token="made-up-refresh-token",
        last_seen_at=now,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(revalidation, "time", fake)
    return fake


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    def _set(seconds: int) -> None:
        settings = SimpleNamespace(
            ui_session_read_revalidation_seconds=seconds,
            keycloak_audience="meho",
            ui_session_encryption_key="made-up-key",
        )
        monkeypatch.setattr(revalidation, "get_settings", lambda: settings)
        # The refusal log names the session by its ref, made with this key.
        monkeypatch.setattr(session_ref_module, "get_settings", lambda: settings)

    _set(_WINDOW_SECONDS)
    return _set


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> tuple[DecryptedSession, _FakeTokenCheck]:
    revalidation.reset_read_revalidation_cache_for_testing()
    session = _session()
    check = _FakeTokenCheck(session.id)
    monkeypatch.setattr(revalidation, "verify_access_token_with_refresh", check)
    yield session, check
    revalidation.reset_read_revalidation_cache_for_testing()


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


async def _served(session: DecryptedSession) -> bool:
    return await revalidation.revalidate_read_session(session) is not None


async def _touch_all(sessions: list[DecryptedSession]) -> None:
    for session in sessions:
        assert await revalidation.revalidate_read_session(session) is not None


def test_disabled_session_is_refused_once_the_window_has_passed(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    session, check = disabled
    revalidation.mark_session_validated(session.id)

    async def _scenario() -> tuple[bool, bool]:
        first = await _served(session)
        clock.advance(_WINDOW_SECONDS + 1)
        second = await _served(session)
        return first, second

    first, second = _run(_scenario())

    assert first is True
    assert second is False
    assert check.calls == [session.id]
    assert session.id not in revalidation._last_validated


@pytest.mark.parametrize("other_count", [_BOUND, _BOUND - 1])
def test_disabled_session_is_refused_within_one_window_however_many_sessions_are_active(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
    other_count: int,
) -> None:
    """Many other active sessions fill the map; the disabled one is still refused.

    Each cycle visits the disabled session, lets 200 s pass, touches
    the other sessions, and lets 101 s pass. The disabled session is
    served at most once (right after login) and refused from the next
    visit on, whether the other sessions reach the map bound or not.
    """
    session, check = disabled
    revalidation.mark_session_validated(session.id)
    others = [_session() for _ in range(other_count)]

    async def _scenario() -> list[bool]:
        served: list[bool] = []
        for _cycle in range(3):
            served.append(await _served(session))
            clock.advance(200)
            await _touch_all(others)
            assert len(revalidation._last_validated) <= _BOUND
            clock.advance(101)
        return served

    served = _run(_scenario())

    assert served == [True, False, False]
    assert check.calls.count(session.id) == 2


def test_session_without_anchor_is_checked_on_its_first_read(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    _unused, check = disabled
    fresh = _session()

    async def _scenario() -> tuple[bool, bool]:
        first = await _served(fresh)
        clock.advance(10)
        second = await _served(fresh)
        return first, second

    first, second = _run(_scenario())

    assert (first, second) == (True, True)
    assert check.calls == [fresh.id]


def test_disabled_session_without_anchor_is_refused_on_its_first_read(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    session, check = disabled

    assert _run(_served(session)) is False
    assert check.calls == [session.id]


def test_marked_session_skips_the_check_inside_the_window(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    _unused, check = disabled
    fresh = _session()
    revalidation.mark_session_validated(fresh.id)
    clock.advance(_WINDOW_SECONDS - 1)

    assert _run(_served(fresh)) is True
    assert check.calls == []


def test_zero_window_checks_every_read_also_for_a_marked_session(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    session, check = disabled
    window(0)
    revalidation.mark_session_validated(session.id)

    assert _run(_served(session)) is False
    assert check.calls == [session.id]


def test_dropped_entry_means_a_check_even_inside_the_window(
    clock: _FakeClock,
    window: Callable[[int], None],
    disabled: tuple[DecryptedSession, _FakeTokenCheck],
) -> None:
    """Filling the map right after login does not buy the disabled session a free read."""
    session, check = disabled
    revalidation.mark_session_validated(session.id)
    clock.advance(1)
    for _ in range(_BOUND):
        revalidation.mark_session_validated(uuid.uuid4())
    clock.advance(1)

    assert session.id not in revalidation._last_validated
    assert _run(_served(session)) is False
    assert check.calls == [session.id]


def test_full_map_drops_only_the_oldest_anchors(clock: _FakeClock) -> None:
    revalidation.reset_read_revalidation_cache_for_testing()
    ids = [uuid.uuid4() for _ in range(_BOUND + 2)]
    for session_id in ids:
        revalidation.mark_session_validated(session_id)
        clock.advance(1)

    assert len(revalidation._last_validated) == _BOUND
    assert ids[0] not in revalidation._last_validated
    assert ids[1] not in revalidation._last_validated
    assert all(session_id in revalidation._last_validated for session_id in ids[2:])
    revalidation.reset_read_revalidation_cache_for_testing()

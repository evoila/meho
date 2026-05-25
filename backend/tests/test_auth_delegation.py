# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the resource-server delegation context (G11.2-T2 #816).

Covers the bind/read/restore contract of
:func:`~meho_backplane.auth.delegation.actor_delegation` +
:func:`~meho_backplane.auth.delegation.resolve_actor_sub` — the seam that
records the RFC 8693 actor (acting agent) on audit rows without an IdP-minted
``act`` claim. No DB or network; pure contextvar behaviour.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from meho_backplane.auth.delegation import (
    ACTOR_SUB_KEY,
    actor_delegation,
    resolve_actor_sub,
)


def setup_function() -> None:
    """Each test starts with a clean structlog contextvar slate."""
    structlog.contextvars.clear_contextvars()


def test_resolve_is_none_when_unbound() -> None:
    assert resolve_actor_sub() is None


def test_actor_delegation_binds_and_restores() -> None:
    assert resolve_actor_sub() is None
    with actor_delegation("agent:incident-triage"):
        assert resolve_actor_sub() == "agent:incident-triage"
    # Restored on exit — a direct human request after the run records no actor.
    assert resolve_actor_sub() is None


def test_actor_delegation_restores_on_exception() -> None:
    with pytest.raises(RuntimeError), actor_delegation("agent:bot"):
        assert resolve_actor_sub() == "agent:bot"
        raise RuntimeError("loop blew up")
    assert resolve_actor_sub() is None


def test_actor_delegation_fails_closed_on_empty() -> None:
    """A delegated run must never silently drop the actor."""
    with pytest.raises(ValueError, match="non-empty agent principal"), actor_delegation(""):
        pass  # pragma: no cover - body never runs


def test_actor_delegation_fails_closed_on_whitespace() -> None:
    """A whitespace-only ref is not a usable actor principal."""
    with pytest.raises(ValueError, match="non-empty agent principal"), actor_delegation("   "):
        pass  # pragma: no cover - body never runs


def test_resolve_normalises_blank_to_none() -> None:
    # A blank contextvar value (defensive: only reachable via direct bind)
    # normalises to None rather than an empty actor attribution.
    structlog.contextvars.bind_contextvars(**{ACTOR_SUB_KEY: ""})
    assert resolve_actor_sub() is None


@pytest.mark.asyncio
async def test_actor_delegation_survives_await() -> None:
    """The binding holds across an await inside the block (single task)."""
    with actor_delegation("agent:async-bot"):
        assert resolve_actor_sub() == "agent:async-bot"
        await asyncio.sleep(0)
        assert resolve_actor_sub() == "agent:async-bot"
    assert resolve_actor_sub() is None


@pytest.mark.asyncio
async def test_child_task_created_inside_block_inherits_actor() -> None:
    """A task created inside the block inherits actor_sub (the AgentRun shape).

    ``run()`` binds the actor then creates the loop task; the task snapshots
    the contextvars at creation, so it carries the actor for its whole life
    even after the binder's ``with`` block exits.
    """
    seen: list[str | None] = []

    async def _child() -> None:
        await asyncio.sleep(0)
        seen.append(resolve_actor_sub())

    with actor_delegation("agent:snapshot-bot"):
        task = asyncio.create_task(_child())
    # The binder's block has exited here, but the task keeps its snapshot.
    assert resolve_actor_sub() is None
    await task
    assert seen == ["agent:snapshot-bot"]

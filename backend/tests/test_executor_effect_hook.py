# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The executor's effect-audit bracketing hook (#3193, mechanism 4).

Verifies that executing a ``remote-write`` item with an effect chain brackets the
mutation with an intent record (before) and an outcome record (after), while a
``safe`` item — or a call with no chain — records nothing (effect audit is a
write-tier concern only). Driven through the private bracketing helper with a
stub handler so the hook is tested without the full #3189 signing apparatus that
edge screening otherwise requires.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner.effect_audit import EffectAuditChain
from meho_backplane.runner.executor import _invoke_recording_effect
from meho_backplane.runner.wire import RunnerPrincipal, RunnerWorkItem

_RUNNER = "runner-hook"


def _item(safety_level: str) -> RunnerWorkItem:
    return RunnerWorkItem(
        check_ref="chk-1",
        op_id="vmware.vm.tag.set",
        product="vmware",
        handler_ref="meho_backplane.connectors.stub.handler",
        params={"tag": "prod"},
        safety_level=safety_level,
        principal=RunnerPrincipal(
            sub="runner-sub",
            tenant_id=uuid.uuid4(),
            tenant_role=TenantRole.OPERATOR,
        ),
        signature="sig-1",
    )


async def _stub_handler(_operator: Any, _target: Any, _params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


def _chain(tmp_path: Path) -> EffectAuditChain:
    return EffectAuditChain(tmp_path / "chain", runner_id=_RUNNER)


@pytest.mark.asyncio
async def test_remote_write_brackets_intent_and_outcome(tmp_path: Path) -> None:
    """A caution item records an intent (before) + an outcome (after)."""
    chain = _chain(tmp_path)
    result = await _invoke_recording_effect(
        _stub_handler, _item("caution"), effect_chain=chain, command_id="cmd-1"
    )
    assert result.status == "ok"

    records = chain.unforwarded()
    assert [r.phase.value for r in records] == ["intent", "outcome"]
    assert records[0].outcome is None
    assert records[1].outcome == "ok"
    assert records[1].signature == "sig-1"  # non-repudiation anchor carried
    assert records[1].command_id == "cmd-1"


@pytest.mark.asyncio
async def test_safe_item_records_nothing(tmp_path: Path) -> None:
    """A safe (read) item never touches the effect chain even when one is supplied."""
    chain = _chain(tmp_path)
    await _invoke_recording_effect(
        _stub_handler, _item("safe"), effect_chain=chain, command_id="cmd-1"
    )
    assert chain.unforwarded() == []


@pytest.mark.asyncio
async def test_remote_write_without_chain_records_nothing(tmp_path: Path) -> None:
    """No chain / no command id → the mutation runs unbracketed (backward-compatible)."""
    chain = _chain(tmp_path)
    await _invoke_recording_effect(
        _stub_handler, _item("caution"), effect_chain=None, command_id="cmd-1"
    )
    await _invoke_recording_effect(
        _stub_handler, _item("caution"), effect_chain=chain, command_id=None
    )
    assert chain.unforwarded() == []

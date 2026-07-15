# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runner-side request-id dedup for gateway capability commands (#2500).

The runner's half of the at-most-once guarantee: a redelivered command id is
never re-executed. :class:`ExecutedCommandStore` records the id before
dispatch; :func:`execute_command_once` re-submits the spooled result on a
redelivery (or returns a ``duplicate_delivery`` refusal when no result was
stored — a crash mid-dispatch), never running the handler twice.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import meho_backplane.runner.executor as executor_mod
from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner.executor import execute_command_once
from meho_backplane.runner.spool import ExecutedCommandStore
from meho_backplane.runner.wire import RunnerPrincipal, RunnerResult, RunnerWorkItem


def _item() -> RunnerWorkItem:
    return RunnerWorkItem(
        check_ref="chk-1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": 9},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="runner-svc", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
    )


async def test_runner_refuses_duplicate_command_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redelivered command id runs the handler once and re-submits the result."""
    store = ExecutedCommandStore(tmp_path / "executed")
    item = _item()
    calls = {"n": 0}

    async def _fake_execute(work_item: RunnerWorkItem) -> RunnerResult:
        calls["n"] += 1
        return RunnerResult(
            result_uid=uuid.uuid4().hex,
            check_ref=work_item.check_ref,
            op_id=work_item.op_id,
            status="ok",
            result={"connected": True},
            error=None,
        )

    monkeypatch.setattr(executor_mod, "execute_work_item", _fake_execute)

    command_id = uuid.uuid4().hex
    first = await execute_command_once(command_id, item, store)
    second = await execute_command_once(command_id, item, store)

    assert calls["n"] == 1, "exactly one local execution for a redelivered command id"
    assert first.status == "ok"
    # The redelivery re-submits the spooled result verbatim (same result_uid),
    # so central ingest dedups it idempotently.
    assert second == first
    assert store.has(command_id)


async def test_recorded_without_result_returns_duplicate_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded-but-un-stored id (crash mid-dispatch) refuses without re-running."""
    store = ExecutedCommandStore(tmp_path / "executed")
    item = _item()
    # Simulate the record-before-execute marker surviving a crash before the
    # result was stored.
    assert store.record(uuid.uuid4().hex) is True
    command_id = uuid.uuid4().hex
    store.record(command_id)  # recorded, no result stored

    calls = {"n": 0}

    async def _fake_execute(work_item: RunnerWorkItem) -> RunnerResult:
        calls["n"] += 1
        raise AssertionError("handler must not run for a recorded command id")

    monkeypatch.setattr(executor_mod, "execute_work_item", _fake_execute)

    result = await execute_command_once(command_id, item, store)

    assert calls["n"] == 0
    assert result.status == "refused"
    assert "duplicate_delivery" in (result.error or "")


def test_record_is_atomic_at_most_once(tmp_path: Path) -> None:
    """``record`` returns True exactly once per id (O_EXCL create)."""
    store = ExecutedCommandStore(tmp_path / "executed")
    command_id = uuid.uuid4().hex
    assert store.record(command_id) is True
    assert store.record(command_id) is False
    assert store.load_result(command_id) is None  # recorded, no result yet

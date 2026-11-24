# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The code's approval table is the checked-in table (docs/codebase/approvals.md).

``tests/snapshots/approval_table.json`` lists every built-in typed
operation with its class, risk level, approval need and never-covered kind.
A change of any of them must show up in the diff of that file. This test
fails when the code and the file differ, names the rows, and prints the
command that writes the file again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from tests.approval_table import (
    WRITE_COMMAND,
    WRITE_ENV,
    build_rows,
    describe_difference,
    register_all_descriptors,
    render,
)

_TABLE = Path(__file__).parent / "snapshots" / "approval_table.json"


def _use_built_in_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings of the table: the built-in defaults, with every operation registered.

    A local ``SERVICE_GRANT_DELETE_SHAPED_PATTERNS`` would change which rows
    no grant can cover, so it is removed. The policy operations register
    only while ``POLICY_CLASSIFICATION`` is on, so it is on.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _use_built_in_settings(monkeypatch)
    # The suite keeps the rows of its first registration pass, which may
    # have run with other switches. Run a fresh pass, and drop its rows after.
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


_FIELDS = {"op_id", "connector_id", "class", "risk_level", "needs_approval", "never_covered"}


@pytest.mark.asyncio
async def test_code_matches_the_checked_in_table() -> None:
    actual = build_rows(await register_all_descriptors())
    if os.environ.get(WRITE_ENV) == "1":
        _TABLE.write_text(render(actual), encoding="utf-8")
    expected = json.loads(_TABLE.read_text(encoding="utf-8"))
    if expected == actual:
        return
    lines = describe_difference(expected, actual)
    shown = "\n".join(lines[:50])
    more = f"\n... and {len(lines) - 50} more" if len(lines) > 50 else ""
    pytest.fail(
        f"The approval table of the code differs from {_TABLE.name} "
        f"({len(lines)} row(s)):\n{shown}{more}\n"
        f"Write the file again, from backend/, and commit it:\n  {WRITE_COMMAND}"
    )


@pytest.mark.asyncio
async def test_a_local_grant_rule_does_not_change_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "*")
    get_settings.cache_clear()
    descriptors = await register_all_descriptors()
    assert build_rows(descriptors) != json.loads(_TABLE.read_text(encoding="utf-8"))
    _use_built_in_settings(monkeypatch)
    assert build_rows(descriptors) == json.loads(_TABLE.read_text(encoding="utf-8"))


def test_the_checked_in_table_is_sorted_and_whole() -> None:
    rows = json.loads(_TABLE.read_text(encoding="utf-8"))
    assert len(rows) > 500
    assert all(set(row) == _FIELDS for row in rows)
    keys = [(row["op_id"], row["connector_id"]) for row in rows]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    assert _TABLE.read_text(encoding="utf-8") == render(rows)

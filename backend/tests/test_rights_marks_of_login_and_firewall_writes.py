# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Writes that change who may log in or reach something carry ``changes_rights``.

A call changes rights when it changes who or what may reach or use something
in the target system: a login, a group or role, an access list, or a
firewall rule. The operations below fit that sentence, so their stored rows
say ``changes_rights`` is true. Their ``hard_to_undo`` stays empty.

No rule of MEHO reads the mark yet. So, with the switch ``CUSTOM_POLICY`` on
as well as off, the approval answers of these operations stay the ones in
``tests/snapshots/approval_table.json``: whether a call needs approval, and
why no standing grant or run approval covers it.

The database backup keeps no ``hard_to_undo`` mark, and its text says that
it overwrites an older backup at the same path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from tests.approval_table import build_rows, register_all_descriptors

_TABLE = Path(__file__).parent / "snapshots" / "approval_table.json"

_RIGHTS_WRITES = frozenset(
    {
        "msad.user.create",
        "msad.user.enable",
        "msad.user.disable",
        "msad.group.create",
        "msad.computer.join-prestage",
        "msad.computer.unjoin",
        "winsrv.localuser.create",
        "winsrv.localuser.set",
        "pfsense.filter.rule.delete",
        "pfsense.nat.delete",
        "pfsense.alias.member.remove",
        "pfsense.openvpn.csc.sync",
    }
)

_BACKUP = "mssql.backup.database"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_the_stored_rows_say_the_call_changes_rights() -> None:
    rows = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id in _RIGHTS_WRITES
    ]
    assert sorted(row.op_id for row in rows) == sorted(_RIGHTS_WRITES)
    for row in rows:
        assert row.changes_rights is True, row.op_id
        assert row.hard_to_undo is None, row.op_id
        assert row.safety_level != "safe", row.op_id


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["false", "true"])
async def test_the_approval_answers_stay_the_same_with_the_switch_off_or_on(
    monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", switch)
    get_settings.cache_clear()
    expected = [
        row
        for row in json.loads(_TABLE.read_text(encoding="utf-8"))
        if row["op_id"] in _RIGHTS_WRITES
    ]
    actual = [
        row
        for row in build_rows(await register_all_descriptors())
        if row["op_id"] in _RIGHTS_WRITES
    ]
    assert len(expected) == len(_RIGHTS_WRITES)
    assert actual == expected


@pytest.mark.asyncio
async def test_the_backup_has_no_mark_and_says_it_overwrites_an_older_backup() -> None:
    (row,) = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id == _BACKUP
    ]
    assert row.hard_to_undo is None
    assert row.llm_instructions is not None
    assert "overwrites an older backup at the same path" in str(row.llm_instructions["when_to_use"])

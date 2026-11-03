# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The verdict table (#4622, "test 1" of ``docs/decisions/sandbox-mode.md``).

For every cell of one table -- principal kind (person, agent, agent run
hosted by MEHO, service principal, satellite runner) x safety level (safe,
caution, dangerous, destructive) x ``requires_approval`` (yes, no) x
delete sign at call time (none, HTTP ``DELETE``, tag ``destructive``,
delete name only) x grant (none, ordinary, delete grant) x gate (a direct
call, a step gate) -- the policy gate's verdict is recorded as ``runs``,
``waits`` or ``refused``.

* **Mode off:** every cell equals the checked-in table
  (``tests/data/sandbox_mode_verdict_table_off.json``), a delete grant
  counts as no grant, and the delete-grant table is never queried (the
  test counts the queries: zero).
* **Mode on:** only cells with "service principal, delete grant" may
  change, and only from ``waits`` to ``runs``. Every other cell is equal.
* **``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` (#5251):** with the setting on,
  every cell equals the same cell with it off, in both modes. The setting
  changes only who may create a delete grant, not what a grant allows.

A ``destructive`` direct call under a delete grant needs the right preview
hash and a valid blast radius to run; the gate alone, with no hash, keeps
it at ``waits`` (the park path then refuses it). The hash path is covered
by ``test_sandbox_mode_calls.py``.

The step gate is modelled as the in-memory record
``enforce_subop_policy`` feeds the gate: the op id and its declared level,
no method, no tags. So at a step gate only the name sign is visible.

To re-generate the mode-off table after a deliberate change of today's
rules: ``MEHO_WRITE_VERDICT_TABLE=1 uv run pytest tests/test_sandbox_mode_verdict_table.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_engine
from meho_backplane.db.models import (
    EndpointDescriptor,
    PermissionVerdict,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import (
    seed_grant_row,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004623")
_CONNECTOR = "vmware-rest-9.0"
_TABLE_FILE = Path(__file__).resolve().parent / "data" / "sandbox_mode_verdict_table_off.json"

_KINDS = ("person", "agent", "hosted_run", "service", "runner")
_LEVELS = ("safe", "caution", "dangerous", "destructive")
_REQUIRES_APPROVAL = ("no", "yes")
_SIGNS = ("none", "verb", "tag", "name")
_GRANTS = ("none", "ordinary", "delete")
_GATES = ("direct", "step")

#: One op id per delete sign.
_OP_IDS = {
    "none": "POST:/vcenter/vm/{vm}/plain",
    "verb": "DELETE:/vcenter/vm/{vm}",
    "tag": "vmware.vm.wipe",
    "name": "vmware.vm.delete",
}

_VERDICT_WORDS = {
    PermissionVerdict.AUTO_EXECUTE: "runs",
    PermissionVerdict.NEEDS_APPROVAL: "waits",
    PermissionVerdict.DENY: "refused",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sub(kind: str, grant: str) -> str:
    return f"{kind}:{grant}"


def _principal_kind(kind: str) -> PrincipalKind:
    return {
        "person": PrincipalKind.USER,
        "agent": PrincipalKind.AGENT,
        "hosted_run": PrincipalKind.USER,
        "service": PrincipalKind.SERVICE,
        "runner": PrincipalKind.RUNNER,
    }[kind]


def _operator(kind: str, grant: str) -> Operator:
    return Operator(
        sub=_sub(kind, grant),
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=_principal_kind(kind),
    )


def _descriptor(level: str, requires_approval: str, sign: str, gate: str) -> EndpointDescriptor:
    fields: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "op_id": _OP_IDS[sign],
        "safety_level": level,
        "requires_approval": requires_approval == "yes",
        "parameter_schema": {},
    }
    if gate == "step":
        # The in-memory record a step gate feeds the gate: no method, no tags.
        fields["source_kind"] = "composite"
    else:
        fields["source_kind"] = "typed" if sign in ("tag", "name") else "ingested"
        fields["method"] = "DELETE" if sign == "verb" else "POST"
        fields["tags"] = ["write", "destructive"] if sign == "tag" else ["write"]
    return EndpointDescriptor(**fields)


async def _seed_grants() -> None:
    for kind in _KINDS:
        for op_id in _OP_IDS.values():
            await seed_grant_row(
                model=ServicePrincipalGrant,
                tenant_id=_TENANT,
                principal_sub=_sub(kind, "ordinary"),
                op_id=op_id,
                connector_id=_CONNECTOR,
            )
            await seed_grant_row(
                model=SandboxDeleteGrant,
                tenant_id=_TENANT,
                principal_sub=_sub(kind, "delete"),
                op_id=op_id,
                connector_id=_CONNECTOR,
                expires_at=soon(),
            )


async def _verdict(kind: str, level: str, ra: str, sign: str, grant: str, gate: str) -> str:
    operator = _operator(kind, grant)
    descriptor = _descriptor(level, ra, sign, gate)
    if kind == "hosted_run":
        with actor_delegation("agent:hosted-run"):
            verdict, _reason = await policy_gate(
                operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
            )
    else:
        verdict, _reason = await policy_gate(
            operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
        )
    return _VERDICT_WORDS[verdict]


def _key(kind: str, level: str, ra: str, sign: str, grant: str, gate: str) -> str:
    return f"{kind}|{level}|requires_approval={ra}|sign={sign}|grant={grant}|{gate}"


async def _table() -> dict[str, str]:
    table: dict[str, str] = {}
    for kind in _KINDS:
        for level in _LEVELS:
            for ra in _REQUIRES_APPROVAL:
                for sign in _SIGNS:
                    for grant in _GRANTS:
                        for gate in _GATES:
                            table[_key(kind, level, ra, sign, grant, gate)] = await _verdict(
                                kind, level, ra, sign, grant, gate
                            )
    return table


class _QueryCounter:
    """Counts SQL statements that touch ``sandbox_delete_grant``."""

    def __init__(self) -> None:
        self.hits = 0

    def __call__(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if "sandbox_delete_grant" in statement:
            self.hits += 1


@pytest.mark.asyncio
async def test_verdict_table(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_grants()

    turn_sandbox_mode_off(monkeypatch)
    counter = _QueryCounter()
    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", counter)
    try:
        off = await _table()
    finally:
        event.remove(sync_engine, "before_cursor_execute", counter)
    assert counter.hits == 0, "with the mode off, the delete-grant table must never be queried"
    assert len(off) == 5 * 4 * 2 * 4 * 3 * 2

    if os.environ.get("MEHO_WRITE_VERDICT_TABLE") == "1":
        _TABLE_FILE.write_text(json.dumps(off, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(_TABLE_FILE.read_text(encoding="utf-8"))
    assert off == expected, "today's verdicts changed; see the module docstring"

    # Mode off: a delete grant counts as no grant, for every kind and cell.
    for kind in _KINDS:
        for level in _LEVELS:
            for ra in _REQUIRES_APPROVAL:
                for sign in _SIGNS:
                    for gate in _GATES:
                        assert (
                            off[_key(kind, level, ra, sign, "delete", gate)]
                            == off[_key(kind, level, ra, sign, "none", gate)]
                        )

    turn_sandbox_mode_on(monkeypatch)
    on = await _table()
    changed: list[str] = []
    for key, before in off.items():
        after = on[key]
        if after == before:
            continue
        changed.append(key)
        assert "|grant=delete|" in key and key.startswith("service|"), key
        assert (before, after) == ("waits", "runs"), (key, before, after)
    # The mode changes something: a delete below ``destructive`` under a
    # delete grant runs for a service principal. (A ``destructive`` direct
    # call needs a preview hash, which the gate alone does not have.)
    assert changed, "sandbox mode changed no cell"
    assert all("|destructive|" not in key for key in changed)
    for sign in ("verb", "tag", "name"):
        assert _key("service", "dangerous", "no", sign, "delete", "direct") in changed

    # The setting that lets REST create delete grants changes no verdict.
    monkeypatch.setenv("MEHO_SANDBOX_DELETE_GRANTS_FROM_API", "true")
    turn_sandbox_mode_on(monkeypatch)
    assert get_settings().sandbox_delete_grants_from_api is True
    assert await _table() == on
    turn_sandbox_mode_off(monkeypatch)
    assert get_settings().sandbox_delete_grants_from_api is True
    assert await _table() == off

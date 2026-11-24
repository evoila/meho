# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a caller reads of a Keycloak write in ``master`` or in the target's admin realm.

With ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` off, the agent's own
``preview_operation`` and the MCP approval reads
(``proposed_effect_for_agent``) show such a write as a write in another
realm: ``login_realm`` is ``False``, and the ``platform_admin`` fact, the
group members and the effective roles are the ones of a write outside the
login realm. The approval request a person reads keeps the stricter facts.
With the switch on, the caller reads the stricter facts too.

Fixture values are made up.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.keycloak.ops_write_grant import GRANT_OPS
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import _person
from tests.test_keycloak_admin_realm_every_write import (
    _PROTECTED,
    _Desc,
    _effect,
    _Keycloak,
    fake,  # noqa: F401 -- fixture: the fake Keycloak with an admin realm
)
from tests.test_keycloak_login_realm_every_write import _OTHER_REALM, _WRITES, _fill, _id, _Write

#: The keys of a preview that carry the facts of the login realm rule.
_FACT_KEYS = ("grant", "effective_roles")
_GRANT_WRITES = [write for write in _WRITES if write.op_id in GRANT_OPS]


@pytest.fixture
def keycloak(fake: _Keycloak) -> _Keycloak:  # noqa: F811 -- the fake of the admin realm tests
    return fake


def _cards(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true" if on else "false")
    get_settings.cache_clear()


def _facts(preview: dict[str, Any]) -> dict[str, Any]:
    return {key: preview.get(key) for key in _FACT_KEYS}


async def _agent_preview(keycloak: _Keycloak, write: _Write, realm: str) -> dict[str, Any]:
    keycloak.realm = write.target_realm or realm
    ctx = PreviewContext(
        descriptor=_Desc(op_id=write.op_id),  # type: ignore[arg-type]
        connector_instance=keycloak.connector(),
        operator=_person("requester", role=TenantRole.OPERATOR),
        target=keycloak.target(),
        params=_fill(copy.deepcopy(write.params), realm),
        connector_id="keycloak-1.x",
        agent_preview=True,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect.get("preview", {})
    assert isinstance(preview, dict)
    return preview


async def _agent_read(keycloak: _Keycloak, write: _Write, realm: str) -> dict[str, Any]:
    """The stored request as ``meho_approvals_get`` gives it back to a caller."""
    effect, params = await _effect(keycloak, write, realm)
    shown = proposed_effect_for_agent(
        write.op_id, effect, params=params, connector_id="keycloak-1.x", tenant=None
    )
    assert shown is not None
    preview = shown.get("preview", {})
    assert isinstance(preview, dict)
    return preview


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _WRITES, ids=[_id(write) for write in _WRITES])
async def test_switch_off_the_agent_preview_reads_as_in_another_realm(
    write: _Write, realm: str, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)

    protected = await _agent_preview(keycloak, write, realm)
    other = await _agent_preview(keycloak, write, _OTHER_REALM)

    assert _facts(protected) == _facts(other)
    if isinstance(protected.get("grant"), dict):
        assert protected["grant"]["login_realm"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _WRITES, ids=[_id(write) for write in _WRITES])
async def test_switch_off_the_mcp_approval_read_reads_as_in_another_realm(
    write: _Write, realm: str, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)

    protected = await _agent_read(keycloak, write, realm)
    other = await _agent_read(keycloak, write, _OTHER_REALM)

    assert _facts(protected) == _facts(other)
    assert _facts(protected) == _facts(await _agent_preview(keycloak, write, realm))


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _GRANT_WRITES, ids=[w.op_id for w in _GRANT_WRITES])
async def test_switch_off_the_person_keeps_the_stricter_facts(
    write: _Write, realm: str, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)

    effect, _params = await _effect(keycloak, write, realm)

    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is True
    assert grant["platform_admin"] is not False
    assert "agent_view" in grant


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _GRANT_WRITES, ids=[w.op_id for w in _GRANT_WRITES])
async def test_switch_on_the_caller_reads_the_stricter_facts(
    write: _Write, realm: str, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=True)

    read = await _agent_read(keycloak, write, realm)
    preview = await _agent_preview(keycloak, write, realm)

    for shown in (read, preview):
        assert shown["grant"]["login_realm"] is True
        assert "agent_view" not in shown["grant"]
    assert read["grant"]["platform_admin"] is not False

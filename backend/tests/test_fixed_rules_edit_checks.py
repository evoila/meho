# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The edit checks ask the fixed rules before approval is turned off.

Each case is a platform admin with a fresh sign-in who asks over REST
(``PATCH /api/v1/connectors/{id}/operations/{op_id}``) to turn approval
off for one made-up operation, with a reason. That person passes every
other check, so only the fixed rules decide.

* With the switch ``CUSTOM_POLICY`` on, the edit is refused for an
  operation that any fixed rule holds, and the error names the rule id.
  No override and no audit row of an edit is written.
* With the switch off, the edit is refused exactly where it was refused
  before (a ``destructive`` operation, an address the caller gives, a
  change of rights), and allowed everywhere else.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import router as connectors_ingest_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationPolicyOverride
from meho_backplane.op_classes import (
    _CREDENTIAL_CAPABLE,
    _PER_CALL_APPROVAL,
    _ROOT_SHELL,
    declare_credential_capable,
    declare_root_shell,
)
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.grant_approval import _RIGHTS_CHANGE_OPS
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.fixed_rules import FIXED_RULES, fixed_rule_of_operation
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TYPED = {"product": "rulepets", "version": "1.x", "impl_id": "rulepets"}
_CONNECTOR = "rulepets-1.x"
_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", CALLER_ADDRESS_MARKER: True}},
}

#: case -> (op id, how it is registered, the rule with the switch on,
#: the error code with the switch off or ``None`` when the edit is allowed).
_CASES: dict[str, tuple[str, dict[str, Any], str, str | None]] = {
    "credential": ("rulepets.key.rotate", {}, "credential_waits", None),
    "caller_address": (
        "rulepets.feed.fetch",
        {"parameter_schema": _ADDRESS_SCHEMA},
        "caller_address_waits",
        "approval_off_caller_address",
    ),
    "destructive": (
        "rulepets.pet.wipe",
        {"safety_level": "destructive"},
        "destructive_waits",
        "approval_off_destructive",
    ),
    "delete": ("rulepets.pet.delete", {}, "delete_waits", None),
    "rights_change": (
        "rulepets.role.give",
        {},
        "rights_change_waits",
        "approval_off_rights_change",
    ),
    "root_shell": ("rulepets.host.run", {}, "root_shell_waits", None),
    "target_rights": (
        "rulepets.member.add",
        {"changes_rights": True},
        "target_rights_waits",
        None,
    ),
}
_PLAIN_OP = "rulepets.pet.rename"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_registry()
    declare_credential_capable((_CASES["credential"][0],))
    declare_root_shell((_CASES["root_shell"][0],))
    # A change of rights with no per-call mark of its own.
    _RIGHTS_CHANGE_OPS.add(_CASES["rights_change"][0])
    yield
    _CREDENTIAL_CAPABLE.discard(_CASES["credential"][0])
    _ROOT_SHELL.discard(_CASES["root_shell"][0])
    _RIGHTS_CHANGE_OPS.discard(_CASES["rights_change"][0])
    _PER_CALL_APPROVAL.discard(_CASES["rights_change"][0])
    clear_registry()
    clear_jwks_cache()
    get_settings.cache_clear()


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    return {}


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


async def _register(op_id: str, **extra: Any) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.25] * 384
    embedding.encode.return_value = [[0.25] * 384]
    embedding.dimension = 384
    kwargs: dict[str, Any] = {
        "parameter_schema": {"type": "object"},
        "safety_level": "caution",
        **extra,
    }
    await register_typed_operation(
        **_TYPED,
        op_id=op_id,
        handler=_handler,
        summary="Change a pet.",
        description="Changes one pet of a made-up product.",
        when_to_use=None,
        requires_approval=True,
        embedding_service=embedding,
        **kwargs,
    )


def _turn_off(op_id: str) -> Any:
    """A platform admin with a fresh sign-in asks over REST to turn approval off."""
    clear_jwks_cache()
    key = make_rsa_keypair(f"kid-{uuid.uuid4()}")
    token = mint_token(
        key,
        sub="platform-admin",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(uuid.uuid4()),
        platform_admin=True,
        extra_claims={"auth_time": int(time.time()) - 5},
    )
    app = FastAPI()
    app.include_router(connectors_ingest_router)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return TestClient(app).patch(
            f"/api/v1/connectors/{_CONNECTOR}/operations/{op_id}",
            json={"requires_approval": False, "reason": "the team checks it by hand"},
            headers={"Authorization": f"Bearer {token}"},
        )


async def _stored(op_id: str) -> tuple[list[OperationPolicyOverride], list[AuditLog]]:
    async with get_sessionmaker()() as session:
        overrides = (
            await session.scalars(
                select(OperationPolicyOverride).where(OperationPolicyOverride.op_id == op_id)
            )
        ).all()
        audit = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "meho_connector_edit_op"))
        ).all()
    edits = [row for row in audit if row.payload.get("op_id") == op_id]
    return list(overrides), edits


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_with_the_switch_on_approval_off_is_refused_with_the_rule_id(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    op_id, extra, rule, _ = _CASES[case]
    _switch(monkeypatch, on=True)
    await _register(op_id, **extra)

    response = _turn_off(op_id)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert f"the fixed rule {rule} holds" in detail
    assert FIXED_RULES[rule] in detail  # type: ignore[index]
    assert await _stored(op_id) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_with_the_switch_off_approval_off_is_refused_only_where_it_was(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    op_id, extra, _, old_code = _CASES[case]
    _switch(monkeypatch, on=False)
    await _register(op_id, **extra)

    response = _turn_off(op_id)

    if old_code is None:
        assert response.status_code == 200, response.text
        overrides, edits = await _stored(op_id)
        assert [o.requires_approval for o in overrides] == [False]
        assert len(edits) == 1
        return
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail.startswith(f"{old_code}: ")
    assert "fixed rule" not in detail
    assert await _stored(op_id) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
async def test_an_operation_with_no_fixed_rule_may_still_lose_its_approval(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    _switch(monkeypatch, on=on)
    await _register(_PLAIN_OP)

    response = _turn_off(_PLAIN_OP)

    assert response.status_code == 200, response.text
    overrides, edits = await _stored(_PLAIN_OP)
    assert [o.requires_approval for o in overrides] == [False]
    assert len(edits) == 1


def test_a_lowered_destructive_operation_keeps_its_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code's own level counts: a row lowered to ``dangerous`` still names ``destructive``."""
    _switch(monkeypatch, on=True)
    row = EndpointDescriptor(
        op_id="rulepets.pet.wipe", safety_level="dangerous", parameter_schema={}, tags=[]
    )
    row.code_safety_level = "destructive"
    assert fixed_rule_of_operation(row) == "destructive_waits"

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``targets.update`` typed op on ``targets-registry-1.x``.

* **Registration** -- ``caution`` with ``requires_approval``, ``host`` and
  ``fqdn`` marked as addresses, with a ``tenant_admin`` role floor.
* **Change** -- only the sent fields change; ``null`` clears a nullable
  field; the target is found by id in the caller's tenant only, also when
  another target is named like that id. A deleted target is refused, and
  the change lands on the row found by id, never on a target found again
  by name or alias.
* **Checks of PATCH** -- a host check, a bad CA pin, a ``null`` product,
  a call with no field and an unknown id are errors, and nothing changes.
* **Role** -- a person with the ``operator`` or ``read_only`` role is
  refused through ``call_operation``, also after approval; the preview
  says ``deny``.
* **Approval** -- every call waits, also a ``tenant_admin``'s call from the
  web console; an ``operator`` approver is refused, a ``tenant_admin``
  approver runs it. The other tests run the call as MEHO runs it after
  approval.
* **Preview and audit** -- the preview names the target and the sent
  fields; every call writes an audit row with op id ``targets.update``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets import update as update_module
from meho_backplane.connectors.targets.ops import TARGETS_REGISTRY_CONNECTOR_ID
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    register_targets_update_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_OLD_REF = "tenants/old/path"
_NEW_REF = f"tenants/{DEFAULT_TENANT_ID}/new-path"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_update_operation(embedding_service=service)
    yield


def _operator(
    sub: str = "p-1",
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: uuid.UUID = _TENANT_ID,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
    )


def _admin() -> Operator:
    return _operator()


def _args(target_id: uuid.UUID | str, **fields: Any) -> dict[str, Any]:
    return {
        "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
        "op_id": TARGETS_UPDATE_OP_ID,
        "params": {"target_id": str(target_id), **fields},
    }


async def _seed(name: str, *, tenant_id: uuid.UUID = _TENANT_ID, **kwargs: Any) -> TargetORM:
    fields: dict[str, Any] = {
        "product": "ssh",
        "host": "203.0.113.5",
        "secret_ref": _OLD_REF,
        "notes": "old note",
        **kwargs,
    }
    return await _insert_target(tenant_id=tenant_id, name=name, **fields)


async def _row(target_id: uuid.UUID) -> TargetORM:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return row


async def _run(operator: Operator, target_id: uuid.UUID | str, **fields: Any) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_UPDATE_OP_ID,
        target=None,
        params={"target_id": str(target_id), **fields},
        _approved=True,
    )


async def _soft_delete(target_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session, session.begin():
        row = await session.get(TargetORM, target_id)
        assert row is not None
        row.deleted_at = datetime.now(UTC)


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if (row.payload or {}).get("op_id") == TARGETS_UPDATE_OP_ID]


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registered_as_a_caution_write_with_a_tenant_admin_floor() -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == TARGETS_UPDATE_OP_ID)
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert (row.product, row.version, row.impl_id) == ("targets", "1.x", "targets-registry")
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    properties = row.parameter_schema["properties"]
    assert properties["host"]["x-meho-caller-address"] is True
    assert properties["fqdn"]["x-meho-caller-address"] is True
    assert row.parameter_schema["required"] == ["target_id"]
    assert row.parameter_schema["additionalProperties"] is False
    assert "name" not in row.parameter_schema["properties"]
    assert "fingerprint" not in row.parameter_schema["properties"]
    assert role_floor_for(TARGETS_UPDATE_OP_ID) is TenantRole.TENANT_ADMIN


# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------


async def test_an_approved_call_changes_only_the_sent_fields() -> None:
    row = await _seed("fix-ref")

    result = await _run(_admin(), row.id, secret_ref=_NEW_REF, port=8443)

    assert result.status == "ok", result
    assert result.result == {
        "target_id": str(row.id),
        "name": "fix-ref",
        "product": "ssh",
        "version": None,
        "host": "203.0.113.5",
        "updated_fields": ["port", "secret_ref"],
    }
    stored = await _row(row.id)
    assert stored.secret_ref == _NEW_REF
    assert stored.port == 8443
    assert stored.notes == "old note"
    assert stored.host == "203.0.113.5"


async def test_an_explicit_null_clears_a_nullable_field() -> None:
    row = await _seed("clear-note")

    result = await _run(_admin(), row.id, notes=None)

    assert result.status == "ok", result
    assert result.result["updated_fields"] == ["notes"]
    assert (await _row(row.id)).notes is None


async def test_the_target_is_found_by_id_also_when_another_is_named_like_it() -> None:
    wanted = await _seed("wanted")
    decoy = await _seed(str(wanted.id))

    result = await _run(_admin(), wanted.id, notes="new note")

    assert result.status == "ok", result
    assert result.result["target_id"] == str(wanted.id)
    assert (await _row(wanted.id)).notes == "new note"
    assert (await _row(decoy.id)).notes == "old note"


async def test_a_deleted_target_is_refused_and_no_alias_holder_changes() -> None:
    gone = await _seed("gone")
    holder = await _seed("holder", aliases=["gone"])
    await _soft_delete(gone.id)

    result = await _run(_admin(), gone.id, notes="new note")

    assert result.status == "error", result
    assert "no live target" in str(result.extras.get("exception_message"))
    assert (await _row(holder.id)).notes == "old note"


async def test_the_change_lands_on_the_row_found_by_id_and_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target is deleted right after the id lookup; no name lookup follows."""
    first = await _seed("first")
    holder = await _seed("holder", aliases=["first"])
    real_lookup = update_module.resolve_target_by_id
    lock_flags: list[bool] = []

    async def lookup_then_delete(
        session: Any, tenant_id: uuid.UUID, target_id: uuid.UUID, *, for_update: bool = False
    ) -> TargetORM | None:
        row = await real_lookup(session, tenant_id, target_id, for_update=for_update)
        lock_flags.append(for_update)
        await _soft_delete(target_id)
        return row

    monkeypatch.setattr(update_module, "resolve_target_by_id", lookup_then_delete)

    result = await _run(_admin(), first.id, notes="new note")

    assert lock_flags == [True]
    assert result.status == "ok", result
    assert result.result["target_id"] == str(first.id)
    assert (await _row(holder.id)).notes == "old note"


@pytest.mark.parametrize("case", ["unknown_id", "other_tenant"])
async def test_a_target_outside_the_tenant_is_not_found(case: str) -> None:
    other = await _seed("elsewhere", tenant_id=uuid.uuid4())
    target_id = uuid.uuid4() if case == "unknown_id" else other.id

    result = await _run(_admin(), target_id, notes="new note")

    assert result.status == "error", result
    assert "no live target" in str(result.extras.get("exception_message"))
    assert (await _row(other.id)).notes == "old note"


# ---------------------------------------------------------------------------
# The checks of PATCH run, and a refused call changes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "needle"),
    [
        ({"secret_ref": _NEW_REF, "host": "169.254.169.254"}, "invalid target parameters"),
        ({"secret_ref": _NEW_REF, "tls_ca_pin": "not a pem"}, "invalid target parameters"),
        ({"secret_ref": "secret/meho/other-team/login"}, "secret_ref"),
        ({}, "at least one field"),
        ({"secret_ref": _NEW_REF, "product": None}, ""),
        ({"secret_ref": _NEW_REF, "name": "renamed"}, ""),
        ({"secret_ref": _NEW_REF, "fingerprint": {"product": "x"}}, ""),
    ],
    ids=[
        "host_check",
        "bad_ca_pin",
        "secret_ref_outside_tenant",
        "no_field",
        "null_product",
        "rename",
        "fingerprint",
    ],
)
async def test_a_refused_change_is_an_error_and_changes_nothing(
    fields: dict[str, Any], needle: str
) -> None:
    row = await _seed("refused", notes="keep me")
    before = await _row(row.id)

    result = await _run(_admin(), row.id, **fields)

    assert result.status in {"error", "invalid_params"}, result
    message = str(result.extras.get("exception_message") or result.error)
    assert needle in message
    after = await _row(row.id)
    assert after.secret_ref == _OLD_REF
    assert after.host == before.host
    assert after.product == before.product
    assert after.updated_at == before.updated_at


async def test_a_bad_target_id_is_an_error() -> None:
    result = await _run(_admin(), "not-a-uuid", notes="x")
    assert result.status in {"error", "invalid_params"}, result


# ---------------------------------------------------------------------------
# Role: tenant_admin on every path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
@pytest.mark.parametrize("web_session", [True, False])
async def test_a_person_below_tenant_admin_is_denied_through_call_operation(
    role: TenantRole, web_session: bool
) -> None:
    row = await _seed(f"denied-{role.value}-{web_session}")

    call = await call_operation(
        _operator(role=role, web_session=web_session), _args(row.id, secret_ref=_NEW_REF)
    )

    assert call["status"] == "denied", call
    assert "tenant_admin" in str(call.get("error"))
    assert (await _row(row.id)).secret_ref == _OLD_REF
    assert await _approval_rows() == []
    (audit,) = await _audit_rows()
    assert audit.payload["result_status"] == "denied"


async def test_the_preview_says_deny_for_an_operator() -> None:
    row = await _seed("preview-deny")

    preview = await preview_operation(
        _operator(role=TenantRole.OPERATOR), _args(row.id, secret_ref=_NEW_REF)
    )

    assert preview["policy_verdict"] == "deny", preview
    assert "tenant_admin" in str(preview["policy_reason"])
    assert (await _row(row.id)).secret_ref == _OLD_REF


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asker",
    [
        _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR),
        _operator("p-token"),
        _operator("p-console", web_session=True),
    ],
    ids=["agent", "admin_with_token", "admin_in_console"],
)
async def test_every_call_waits_for_approval(asker: Operator) -> None:
    row = await _seed(f"waits-{asker.sub}")

    call = await call_operation(asker, _args(row.id, secret_ref=_NEW_REF))

    assert call["status"] == "awaiting_approval", call
    assert len(await _approval_rows()) == 1
    assert (await _row(row.id)).secret_ref == _OLD_REF


async def _park_agent_call(target_id: uuid.UUID) -> uuid.UUID:
    agent = _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR)
    call = await call_operation(agent, _args(target_id, secret_ref=_NEW_REF))
    assert call["status"] == "awaiting_approval", call
    return uuid.UUID(str(call["extras"]["approval_request_id"]))


async def test_an_operator_cannot_approve_the_change() -> None:
    row = await _seed("operator-approves")
    request_id = await _park_agent_call(row.id)
    approver = _operator("p-op", role=TenantRole.OPERATOR, web_session=True)

    async with get_sessionmaker()() as session:
        with pytest.raises(ApprovalRoleFloorError):
            await approve_request(session, request_id, operator=approver)
        await session.rollback()

    assert (await _row(row.id)).secret_ref == _OLD_REF


async def test_a_tenant_admin_approves_and_the_change_runs() -> None:
    row = await _seed("admin-approves")
    request_id = await _park_agent_call(row.id)
    approver = _operator("p-admin", web_session=True)

    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    result = await resume_dispatch_after_approval(operator=approver, request=request)

    assert result.status == "ok", result
    assert result.result["updated_fields"] == ["secret_ref"]
    assert (await _row(row.id)).secret_ref == _NEW_REF


# ---------------------------------------------------------------------------
# Preview and audit
# ---------------------------------------------------------------------------


async def _parked_preview() -> dict[str, Any]:
    (request,) = await _approval_rows()
    assert request.proposed_effect is not None
    preview = request.proposed_effect["preview"]
    assert isinstance(preview, dict)
    return preview


async def test_the_approval_card_names_the_target_and_the_sent_fields() -> None:
    row = await _seed("preview-me")
    agent = _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR)

    call = await call_operation(agent, _args(row.id, secret_ref=_NEW_REF, port=22))

    assert call["status"] == "awaiting_approval", call
    effect = await _parked_preview()
    assert effect["target"] == {"id": str(row.id), "name": "preview-me", "host": row.host}
    assert effect["connector"]["product"] == "ssh"
    assert effect["fields"] == ["port", "secret_ref"]
    assert effect["result_if_run"] == "updated"
    assert (await _row(row.id)).secret_ref == _OLD_REF


async def test_the_approval_card_of_a_missing_target_says_it_fails() -> None:
    agent = _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR)

    call = await call_operation(agent, _args(uuid.uuid4(), notes="x"))

    assert call["status"] == "awaiting_approval", call
    effect = await _parked_preview()
    assert effect["target"] is None
    assert effect["result_if_run"] == "fails"


async def test_an_executed_call_writes_an_audit_row() -> None:
    row = await _seed("audited")

    result = await _run(_admin(), row.id, notes="new note")

    assert result.status == "ok", result
    (audit,) = await _audit_rows()
    assert audit.method == "DISPATCH"
    assert audit.path == TARGETS_UPDATE_OP_ID

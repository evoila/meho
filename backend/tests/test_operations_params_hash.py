# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keyed params hashes (:mod:`meho_backplane.operations._validate`).

Audit records store a keyed hash of each call's params, and an approval
request stores one to bind the exact call. These tests pin what the two
hashes must do:

* Both are keyed: they change when the server key changes, and neither is
  the plain SHA-256 of the params. The column format (64 hex characters)
  stays the same.
* The same params give the same hash under one key.
* A secret-named value does not change the audit hash; the other values do.
* The operations that take a password (the Vault userpass writes and the
  subscribed content-library create) write audit rows whose hash does not
  depend on the password.
* The approval hash still covers every value: a changed password does not
  match. A request stored with the earlier plain hash still matches its
  own params.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vault.ops_auth_write_schemas import (
    VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._validate import (
    compute_audit_params_hash,
    compute_params_hash,
    params_hash_candidates,
    params_hash_matches,
)
from meho_backplane.operations.approval_handoff import ApprovalHandoffError
from meho_backplane.operations.approval_queue import (
    ParamsMismatchError,
    approve_request,
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import _subop_resume_cleared, composite_resume_var
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a7")
_PARAMS: dict[str, Any] = {
    "username": "user-a",
    "password": "first-test-value",
    "mount": "userpass",
}
_UPDATE_PASSWORD = "vault.auth.userpass.update_password"


def _plain_sha256(params: dict[str, Any]) -> str:
    """The unkeyed hash earlier versions stored."""
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


# ---------------------------------------------------------------------------
# The hash functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hash_fn", [compute_params_hash, compute_audit_params_hash])
def test_hash_is_keyed_and_keeps_the_column_format(hash_fn: Any) -> None:
    digest = hash_fn(_PARAMS)
    assert len(digest) == 64
    int(digest, 16)  # hex, like the older values in the same column
    assert digest != _plain_sha256(_PARAMS)


@pytest.mark.parametrize("hash_fn", [compute_params_hash, compute_audit_params_hash])
def test_hash_changes_when_the_key_changes(
    hash_fn: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = hash_fn(_PARAMS)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    get_settings.cache_clear()
    after = hash_fn(_PARAMS)
    assert after != before
    assert after == hash_fn(dict(_PARAMS))


@pytest.mark.parametrize("hash_fn", [compute_params_hash, compute_audit_params_hash])
def test_identical_params_give_identical_hashes(hash_fn: Any) -> None:
    reordered = {"mount": "userpass", "password": "first-test-value", "username": "user-a"}
    assert hash_fn(_PARAMS) == hash_fn(reordered)
    marker = {"id": uuid.UUID(int=7), "when": datetime(2026, 1, 1, tzinfo=UTC)}
    assert hash_fn(marker) == hash_fn(dict(marker))
    assert hash_fn({"x": 1}) != hash_fn({"x": 2})


@pytest.mark.parametrize(
    "name", ["password", "admin_password", "client_secret", "apiKey", "private_key", "token"]
)
def test_secret_value_does_not_change_the_audit_hash(name: str) -> None:
    first = {"username": "user-a", name: "first-test-value"}
    second = {"username": "user-a", name: "second-test-value"}
    assert compute_audit_params_hash(first) == compute_audit_params_hash(second)
    # The parameter name still counts, and so do the other values.
    assert compute_audit_params_hash(first) != compute_audit_params_hash({"username": "user-a"})
    assert compute_audit_params_hash(first) != compute_audit_params_hash(
        {**first, "username": "user-b"}
    )


def test_nested_secret_value_does_not_change_the_audit_hash() -> None:
    def spec(password: str) -> dict[str, Any]:
        return {"spec": [{"fqdn": "host-a.example.com", "password": password}]}

    assert compute_audit_params_hash(spec("first-test-value")) == compute_audit_params_hash(
        spec("second-test-value")
    )


def test_audit_hash_equals_approval_hash_without_secret_values() -> None:
    params = {"host": "host-a.example.com", "token_policies": ["reader"], "count": 3}
    assert compute_audit_params_hash(params) == compute_params_hash(params)


def test_approval_hash_covers_secret_values() -> None:
    swapped = {**_PARAMS, "password": "second-test-value"}
    assert compute_params_hash(swapped) != compute_params_hash(_PARAMS)
    assert not params_hash_matches(swapped, compute_params_hash(_PARAMS))


def test_params_hash_matches_the_keyed_and_the_earlier_plain_hash() -> None:
    assert params_hash_candidates(_PARAMS) == (compute_params_hash(_PARAMS), _plain_sha256(_PARAMS))
    assert params_hash_matches(_PARAMS, compute_params_hash(_PARAMS))
    assert params_hash_matches(_PARAMS, _plain_sha256(_PARAMS))
    other = {**_PARAMS, "username": "user-b"}
    assert not params_hash_matches(other, _plain_sha256(_PARAMS))
    assert not params_hash_matches(other, compute_params_hash(_PARAMS))


# ---------------------------------------------------------------------------
# The audit rows of the password operations
# ---------------------------------------------------------------------------


async def _ack_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Return a value-free confirmation; the password stays in params."""
    return {"done": True}


class _HashCheckConnector(Connector):
    product = "hashcheck"
    version = "1.x"
    impl_id = "hashcheck"
    priority = 10

    async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
        return FingerprintResult(
            probe=ProbeResult(reachable=True, probe_method="none"),
            product="hashcheck",
            version="1.x",
        )

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


class _Target:
    product = "hashcheck"
    id = uuid.UUID("00000000-0000-0000-0000-0000000000b7")
    name = "hashcheck-target"


def _operator(sub: str = "agent-hashcheck", kind: PrincipalKind = PrincipalKind.AGENT) -> Operator:
    return Operator(
        sub=sub,
        name="Hash Check Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _register(
    op_id: str,
    schema: dict[str, Any],
    embedding_service: AsyncMock,
    *,
    requires_approval: bool = False,
) -> None:
    register_connector_v2(product="hashcheck", version="", impl_id="", cls=_HashCheckConnector)
    await register_typed_operation(
        product="hashcheck",
        version="1.x",
        impl_id="hashcheck",
        op_id=op_id,
        handler=_ack_handler,
        summary="Password-bearing write.",
        description="Test.",
        parameter_schema=schema,
        requires_approval=requires_approval,
        when_to_use=None,
        embedding_service=embedding_service,
    )


#: The operations that take a password in their params, with their real
#: parameter schemas and every other field fixed.
_PASSWORD_OPS = [
    pytest.param(
        _UPDATE_PASSWORD,
        VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA,
        {"username": "user-a", "mount": "userpass"},
        id="vault-userpass-update-password",
    ),
    pytest.param(
        "vault.auth.userpass.write",
        VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA,
        {"username": "user-a", "mount": "userpass", "token_policies": ["reader"]},
        id="vault-userpass-write",
    ),
    pytest.param(
        "vmware.composite.content_library.subscribed.create",
        CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
        {
            "name": "library-a",
            "subscription_url": "https://content.example.com/lib.json",
            "datastore": "datastore-a",
            "authentication_method": "BASIC",
            "username": "reader",
        },
        id="content-library-subscribed-create",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "schema", "other_params"), _PASSWORD_OPS)
async def test_password_operation_audit_hash_does_not_depend_on_the_password(
    op_id: str,
    schema: dict[str, Any],
    other_params: dict[str, Any],
    stub_embedding_service: AsyncMock,
) -> None:
    """Two calls that differ only in the password write the same audit hash."""
    await _register(op_id, schema, stub_embedding_service)

    for password in ("first-test-value", "second-test-value"):
        result = await dispatch(
            operator=_operator(),
            connector_id="hashcheck-1.x",
            op_id=op_id,
            target=_Target(),
            params={**other_params, "password": password},
        )
        assert result.status == "ok", result.error

    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    hashes = {row.payload["params_hash"] for row in rows}
    assert len(rows) == 2
    assert hashes == {compute_audit_params_hash({**other_params, "password": "first-test-value"})}
    assert _plain_sha256({**other_params, "password": "first-test-value"}) not in hashes


# ---------------------------------------------------------------------------
# Approval requests keep the full hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parked_password_operation_stores_the_full_approval_hash(
    stub_embedding_service: AsyncMock,
) -> None:
    """The approval row binds every value; the approve check refuses a changed password."""
    await _register(
        _UPDATE_PASSWORD,
        VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA,
        stub_embedding_service,
        requires_approval=True,
    )
    result = await dispatch(
        operator=_operator(),
        connector_id="hashcheck-1.x",
        op_id=_UPDATE_PASSWORD,
        target=_Target(),
        params=dict(_PARAMS),
    )
    assert result.status == "awaiting_approval", result.error
    request_id = uuid.UUID(result.extras["approval_request_id"])

    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    assert request.params_hash == compute_params_hash(_PARAMS)
    assert request.params_hash != compute_audit_params_hash(_PARAMS)

    reviewer = _operator(sub="reviewer-hashcheck", kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        with pytest.raises(ParamsMismatchError):
            await approve_request(
                session,
                request_id,
                operator=reviewer,
                params={**_PARAMS, "password": "second-test-value"},
            )
    async with get_sessionmaker()() as session:
        approved = await approve_request(
            session, request_id, operator=reviewer, params=dict(_PARAMS)
        )
        await session.commit()
    assert approved.status == ApprovalRequestStatus.APPROVED.value


async def _park(params_hash: str, *, idempotency_key: str | None = None) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator(),
            connector_id="hashcheck-1.x",
            op_id=_UPDATE_PASSWORD,
            target=None,
            params=dict(_PARAMS),
            params_hash=params_hash,
            idempotency_key=idempotency_key,
        )
        await session.commit()
    return request


@pytest.mark.asyncio
async def test_request_stored_with_the_plain_hash_can_still_be_approved() -> None:
    """A request that waited across the upgrade still matches its own params."""
    request = await _park(_plain_sha256(_PARAMS))
    reviewer = _operator(sub="reviewer-hashcheck", kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        with pytest.raises(ParamsMismatchError):
            await approve_request(
                session,
                request.id,
                operator=reviewer,
                params={**_PARAMS, "password": "second-test-value"},
            )
    async with get_sessionmaker()() as session:
        approved = await approve_request(
            session, request.id, operator=reviewer, params=dict(_PARAMS)
        )
        await session.commit()
    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_retry_of_a_request_stored_with_the_plain_hash_reuses_it() -> None:
    first = await _park(_plain_sha256(_PARAMS), idempotency_key="retry-key")
    second = await _park(compute_params_hash(_PARAMS), idempotency_key="retry-key")
    assert second.id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["keyed", "plain"])
async def test_resume_runs_the_stored_input_under_either_hash(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resumed call runs with the stored input; a hash that fits no input is refused."""
    stored_hash = compute_params_hash(_PARAMS) if stored == "keyed" else _plain_sha256(_PARAMS)
    calls: list[dict[str, Any]] = []

    async def _dispatch(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(dispatcher_module, "dispatch", _dispatch)
    reviewer = _operator(sub="reviewer-hashcheck", kind=PrincipalKind.USER)
    for params_hash in (stored_hash, compute_params_hash({**_PARAMS, "username": "user-b"})):
        request = await _park(params_hash)
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request.id)
            assert row is not None
            row.status = ApprovalRequestStatus.APPROVED.value
            row.decided_at = datetime.now(UTC)
            await session.commit()
        if params_hash == stored_hash:
            await resume_dispatch_after_approval(operator=reviewer, request=row)
        else:
            with pytest.raises(ApprovalHandoffError):
                await resume_dispatch_after_approval(operator=reviewer, request=row)
    assert [call["params"] for call in calls] == [_PARAMS]


@pytest.mark.parametrize("stored", ["keyed", "plain"])
def test_approved_composite_step_clears_under_either_hash(stored: str) -> None:
    stored_hash = compute_params_hash(_PARAMS) if stored == "keyed" else _plain_sha256(_PARAMS)
    token = composite_resume_var.set((_UPDATE_PASSWORD, stored_hash))
    try:
        changed = {**_PARAMS, "password": "second-test-value"}
        assert not _subop_resume_cleared(op_id=_UPDATE_PASSWORD, params=changed)
        assert not _subop_resume_cleared(op_id="vault.auth.userpass.write", params=dict(_PARAMS))
        assert _subop_resume_cleared(op_id=_UPDATE_PASSWORD, params=dict(_PARAMS))
        # Consumed: the same step does not clear twice.
        assert not _subop_resume_cleared(op_id=_UPDATE_PASSWORD, params=dict(_PARAMS))
    finally:
        composite_resume_var.reset(token)

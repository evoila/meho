# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Default key-name scrub for credential_read dispatch responses (#2467).

A ``credential_read``-classified op (``vault.kv.read`` et al.) returns the
raw secret in its handler payload -- that is the op's documented contract.
The connector-boundary redaction engine cannot mask it: it matches only
*labelled* secret shapes inside string leaves, so a ``{"password": "..."}``
dict value passes through verbatim and lands in the ``call_operation``
caller's response (and, for an agent caller, its model-API transcript).

These tests pin the contract:

* the caller-bound answer of ``vault.kv.read`` hides every value under
  ``data`` by default and keeps the key names and ``version``;
* ``params.reveal_secret=true`` from a person waits for another person's
  approval. The approve runs nothing; the read runs once when the person
  who asked fetches the value, within the fetch window. That answer holds
  the raw value, the request row never does, and the choice is stamped on
  the audit row (queryable), while ``reveal_secret``
  never reaches the op's ``parameter_schema`` (it is stripped as a
  dispatch control) nor the ``params_hash``;
* the audit row's ``raw_payload`` stores a keyed fingerprint in place of
  each hidden value in BOTH cases;
* a non-credential_read op answer is untouched and carries no
  ``reveal_secret`` stamp.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from meho_backplane.api.v1.approval_reveal import reveal_approved_request
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ResultAccessForbiddenError,
    approve_request,
    read_approval_result,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approved_reveal import (
    WAITING_FOR_REQUESTER,
    RevealFetchRefusedError,
    fetch_approved_reveal,
    reveal_fetch_window,
)
from meho_backplane.settings import get_settings

# Assembled from fragments so gitleaks' built-in rules don't false-positive
# on the test source.
_PASSWORD_SECRET = "hunter2" + "longenough"
_REDACTED = "[REDACTED:param_name]"
_HIDDEN = "[REDACTED:secret_value]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"


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


def _make_operator(*, principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="op-2467-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000002467"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _NoOpVaultConnector(Connector):
    """Connector class satisfying the resolver lookup for the typed op."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _module_vault_read_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level ``vault.kv.read`` stand-in returning a raw secret.

    Mirrors the real handler's ``{"data": <secret dict>, "version": N}``
    shape. ``reveal_secret`` must have been stripped by the dispatcher, so
    it is asserted absent from the handler-visible params.
    """
    assert "reveal_secret" not in params, "reveal_secret must be stripped before the handler"
    return {"data": {"password": _PASSWORD_SECRET, "username": "root"}, "version": 3}


async def _register_vault_read(embedding_service: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_vault_read_handler,
        summary="Read a KV v2 secret.",
        description="Read a secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _audit_row(op_id: str) -> AuditLog:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1, f"expected exactly one audit row for {op_id}, got {len(rows)}"
    return rows[0]


async def _ok_rows(op_id: str) -> list[AuditLog]:
    """The audit rows of the runs of *op_id*, without the rows of a wait for approval."""
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row for row in rows if row.payload.get("result_status") == "ok"]


_REVIEWER = _make_operator().model_copy(update={"sub": "reviewer-2467"})


async def _approved_reveal() -> UUID:
    """A person's reveal of ``vault.kv.read`` waits; another person approves it.

    The approve runs nothing: the approver's answer holds no value.
    """
    parked = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    assert parked.status == "awaiting_approval", parked.error
    assert _PASSWORD_SECRET not in parked.model_dump_json()
    request_id = UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=_REVIEWER)
        await session.commit()
    answer = await resume_dispatch_after_approval(operator=_REVIEWER, request=approved, params=None)
    assert answer.status == WAITING_FOR_REQUESTER, answer
    assert _PASSWORD_SECRET not in answer.model_dump_json()
    return request_id


async def _reveal_after_approval() -> OperationResult:
    """The approved reveal runs once, when the person who asked fetches the value."""
    return await fetch_approved_reveal(_make_operator(), await _approved_reveal())


async def _request_row(request_id: UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as fresh:
        row = await fresh.get(ApprovalRequest, request_id)
    assert row is not None
    return row


def _whole_row(row: ApprovalRequest) -> str:
    """Every column of *row*, as one string to search for the test secret."""
    return json.dumps(
        {column.name: getattr(row, column.key) for column in ApprovalRequest.__mapper__.columns},
        default=str,
    )


# ---------------------------------------------------------------------------
# Scrubbed by default
# ---------------------------------------------------------------------------


async def test_credential_read_response_scrubbed_by_default(
    stub_embedding_service: AsyncMock,
) -> None:
    """No ``reveal_secret`` -> every value under ``data`` is hidden; names stay."""
    await _register_vault_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    # Every value under data is hidden; the names and the version stay.
    assert result.result["data"] == {"password": _HIDDEN, "username": _HIDDEN}
    assert result.result["version"] == 3
    # The raw secret never reaches the caller-bound envelope.
    assert _PASSWORD_SECRET not in result.model_dump_json()

    # The audit row stores fingerprints, not the values, and stamps the
    # scrubbed (reveal_secret=False) choice.
    row = await _audit_row("vault.kv.read")
    assert isinstance(row.raw_payload, dict)
    assert row.raw_payload["data"]["password"].startswith(_FINGERPRINT)
    assert row.raw_payload["data"]["username"].startswith(_FINGERPRINT)
    assert row.raw_payload["version"] == 3
    assert _PASSWORD_SECRET not in str(row.raw_payload)
    assert row.payload["reveal_secret"] is False


async def test_credential_read_scrub_is_principal_independent(
    stub_embedding_service: AsyncMock,
) -> None:
    """The default hiding keys on the op, not the caller kind.

    A ``USER`` principal (default-allow) reaches execution and gets the
    names without the values, like every other caller.
    """
    await _register_vault_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.USER),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert result.result["data"]["password"] == _HIDDEN


# ---------------------------------------------------------------------------
# reveal_secret=true opt-in
# ---------------------------------------------------------------------------


async def test_reveal_secret_true_returns_raw_and_stamps_audit(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret=true`` -> after approval, raw value returned AND stamped on the audit row."""
    await _register_vault_read(stub_embedding_service)

    result = await _reveal_after_approval()
    assert result.status == "ok", result.error
    # The caller opted in: the raw value comes back.
    assert result.result["data"]["password"] == _PASSWORD_SECRET
    assert result.result["data"]["username"] == "root"

    (row,) = await _ok_rows("vault.kv.read")
    # The reveal is queryable on the audit row.
    assert row.payload["reveal_secret"] is True
    # raw_payload stores a fingerprint, also when the caller got the value.
    assert isinstance(row.raw_payload, dict)
    assert row.raw_payload["data"]["password"].startswith(_FINGERPRINT)
    assert _PASSWORD_SECRET not in str(row.raw_payload)


async def test_approving_a_reveal_runs_nothing(
    stub_embedding_service: AsyncMock,
) -> None:
    """The approve runs no read: no run, no stored result, no value anywhere."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()

    assert await _ok_rows("vault.kv.read") == []
    row = await _request_row(request_id)
    assert row.status == "approved"
    assert row.resumed_at is None
    assert row.resume_result is None
    assert _PASSWORD_SECRET not in _whole_row(row)


async def test_after_the_fetch_the_request_row_holds_no_secret_value(
    stub_embedding_service: AsyncMock,
) -> None:
    """The person who asked gets the value; the request row and its stored result do not."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    result = await fetch_approved_reveal(_make_operator(), request_id)
    assert result.status == "ok", result.error
    assert result.result["data"]["password"] == _PASSWORD_SECRET

    row = await _request_row(request_id)
    assert _PASSWORD_SECRET not in _whole_row(row)
    async with get_sessionmaker()() as session:
        stored = await read_approval_result(
            session, operator=_make_operator(), request_id=request_id
        )
        await session.commit()
    assert stored.resume_result is not None
    assert stored.resume_result["status"] == "ok"
    assert stored.resume_result["result"] is None
    assert _PASSWORD_SECRET not in json.dumps(stored.resume_result)
    async with get_sessionmaker()() as session:
        with pytest.raises(ResultAccessForbiddenError):
            await read_approval_result(session, operator=_REVIEWER, request_id=request_id)


async def test_a_second_fetch_is_refused(
    stub_embedding_service: AsyncMock,
) -> None:
    """The permission works once: the second fetch is refused and runs nothing."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    first = await fetch_approved_reveal(_make_operator(), request_id)
    assert first.status == "ok", first.error

    with pytest.raises(RevealFetchRefusedError) as refused:
        await fetch_approved_reveal(_make_operator(), request_id)
    assert refused.value.code == "already_used"
    assert "already fetched" in refused.value.message
    assert len(await _ok_rows("vault.kv.read")) == 1


async def test_a_fetch_after_the_time_limit_is_refused(
    stub_embedding_service: AsyncMock,
) -> None:
    """After the fetch window the permission is gone; the person asks again."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    late = datetime.now(UTC) + reveal_fetch_window() + timedelta(seconds=1)
    with pytest.raises(RevealFetchRefusedError) as refused:
        await fetch_approved_reveal(_make_operator(), request_id, now=late)
    assert refused.value.code == "window_passed"
    assert await _ok_rows("vault.kv.read") == []

    # Within the window the same request still works.
    soon = datetime.now(UTC) + reveal_fetch_window() - timedelta(seconds=30)
    result = await fetch_approved_reveal(_make_operator(), request_id, now=soon)
    assert result.status == "ok", result.error


async def test_a_fetch_before_the_approval_is_refused(
    stub_embedding_service: AsyncMock,
) -> None:
    """A waiting request gives no value; the fetch says it is not approved yet."""
    await _register_vault_read(stub_embedding_service)
    parked = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    with pytest.raises(RevealFetchRefusedError) as refused:
        await fetch_approved_reveal(_make_operator(), UUID(parked.extras["approval_request_id"]))
    assert refused.value.code == "not_approved"
    assert await _ok_rows("vault.kv.read") == []


async def test_another_person_or_tenant_cannot_fetch(
    stub_embedding_service: AsyncMock,
) -> None:
    """Only the person who asked fetches: the approver and another tenant get no value."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    with pytest.raises(ResultAccessForbiddenError):
        await fetch_approved_reveal(_REVIEWER, request_id)
    other_tenant = _make_operator().model_copy(
        update={"tenant_id": UUID("00000000-0000-0000-0000-000000009999")}
    )
    with pytest.raises(ApprovalNotFoundError):
        await fetch_approved_reveal(other_tenant, request_id)
    assert await _ok_rows("vault.kv.read") == []

    # The permission is still there for the person who asked.
    result = await fetch_approved_reveal(_make_operator(), request_id)
    assert result.status == "ok", result.error


async def test_a_fetch_with_changed_params_is_refused(
    stub_embedding_service: AsyncMock,
) -> None:
    """The permission is for the stored params: changed params run nothing."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(params={"path": "app/other"})
        )
        await session.commit()
    with pytest.raises(RevealFetchRefusedError) as refused:
        await fetch_approved_reveal(_make_operator(), request_id)
    assert refused.value.code == "params_changed"
    assert await _ok_rows("vault.kv.read") == []


async def test_each_fetch_is_audited_and_the_read_stores_fingerprints(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every fetch leaves an ``approval.reveal`` row; the read row has fingerprints only."""
    await _register_vault_read(stub_embedding_service)

    request_id = await _approved_reveal()
    await fetch_approved_reveal(_make_operator(), request_id)
    with pytest.raises(RevealFetchRefusedError):
        await fetch_approved_reveal(_make_operator(), request_id)

    async with get_sessionmaker()() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.reveal")))
            .scalars()
            .all()
        )
    assert sorted(row.payload["outcome"] for row in rows) == ["already_used", "run"]
    (read_row,) = await _ok_rows("vault.kv.read")
    assert read_row.payload["reveal_secret"] is True
    assert read_row.raw_payload["data"]["password"].startswith(_FINGERPRINT)
    every_row = json.dumps([r.payload for r in rows] + [read_row.raw_payload], default=str)
    assert _PASSWORD_SECRET not in every_row


async def test_preview_of_a_reveal_reports_the_wait_of_the_real_call(
    stub_embedding_service: AsyncMock,
) -> None:
    """Preview and the real call agree: a reveal waits, a hidden read runs at once."""
    await _register_vault_read(stub_embedding_service)
    call = {
        "operator": _make_operator(),
        "connector_id": "vault-1.x",
        "op_id": "vault.kv.read",
        "target": None,
    }

    hidden = await preview_dispatch(**call, params={"path": "app/db"})
    assert (hidden["safety_level"], hidden["requires_approval"]) == ("safe", False)
    assert hidden["policy_verdict"] == "auto-execute"

    revealed = await preview_dispatch(**call, params={"path": "app/db", "reveal_secret": True})
    assert (revealed["safety_level"], revealed["requires_approval"]) == ("caution", True)
    assert revealed["policy_verdict"] == "needs-approval"
    assert revealed["status"] != "error", revealed

    parked = await dispatch(**call, params={"path": "app/db", "reveal_secret": True})
    assert parked.status == "awaiting_approval", parked.error


async def test_reveal_secret_does_not_reach_op_schema_or_hash(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret`` is a dispatch control: stripped before schema + hash.

    The op schema is ``additionalProperties: false``; if ``reveal_secret``
    were forwarded, dispatch would 422 with ``invalid_params``. And the
    scrubbed read and its reveal must share one ``params_hash`` (the reveal
    choice is recorded separately on the audit row, not in the hash).
    """
    await _register_vault_read(stub_embedding_service)
    operator = _make_operator()

    scrubbed = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    revealed = await _reveal_after_approval()
    # Neither dispatch 422'd on the strict schema.
    assert scrubbed.status == "ok", scrubbed.error
    assert revealed.status == "ok", revealed.error

    # Both run rows share one params_hash despite the differing reveal.
    rows = await _ok_rows("vault.kv.read")
    assert len(rows) == 2
    hashes = {row.payload["params_hash"] for row in rows}
    assert len(hashes) == 1, "reveal choice must not perturb params_hash"


# ---------------------------------------------------------------------------
# Every other op hides its secret-named fields too
# ---------------------------------------------------------------------------


async def _module_metadata_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A plain ``read`` op that happens to echo a secret-named field."""
    return {"password": _PASSWORD_SECRET, "note": "not a credential_read op"}


async def _module_pair_fields_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A read whose answer lists the fields of a credential as name/value pairs."""
    return {
        "credentialInstances": [
            {
                "id": "<credential-id>",
                "name": "my-credential",
                "fields": [
                    {"name": "USER", "value": "root"},
                    {"name": "PASSWORD", "value": _PASSWORD_SECRET},
                ],
            }
        ]
    }


async def _module_pair_fields_http_error_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A read whose vendor answers with an error body that lists fields as pairs."""
    request = httpx.Request("GET", "https://vendor.test/api/credentials")
    response = httpx.Response(
        500,
        json=[{"name": "USER", "value": "root"}, {"name": "PASSWORD", "value": _PASSWORD_SECRET}],
        request=request,
    )
    raise httpx.HTTPStatusError("500 Server Error", request=request, response=response)


async def _module_escaped_pair_name_http_error_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A read whose vendor error body writes the name of a pair with a JSON escape."""
    request = httpx.Request("GET", "https://vendor.test/api/credentials")
    body = (
        '[{"name": "USER", "value": "root"}, '
        '{"name": "\\u0050ASSWORD", "value": "' + _PASSWORD_SECRET + '"}, '
        '{"value": "' + _PASSWORD_SECRET + '", "name": "\\u0050ASSWORD"}]'
    )
    response = httpx.Response(
        500, content=body.encode(), headers={"content-type": "application/json"}, request=request
    )
    raise httpx.HTTPStatusError("500 Server Error", request=request, response=response)


async def _register_pair_fields_read(
    stub_embedding_service: AsyncMock, handler: Any = _module_pair_fields_handler
) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.versions",
        handler=handler,
        summary="List credential fields.",
        description="Name/value pairs.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def _register_metadata_read(stub_embedding_service: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.versions",
        handler=_module_metadata_handler,
        summary="List KV metadata versions.",
        description="Metadata only.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def test_read_op_hides_secret_named_fields_and_carries_no_stamp(
    stub_embedding_service: AsyncMock,
) -> None:
    """A ``read``-class op hides a secret-named field, and its audit row has no reveal key.

    ``vault.kv.versions`` classifies ``read`` (metadata browse), not
    ``credential_read``. The answer still hides the ``password`` field: the
    name rule runs on every answer. The reveal stamp is for secret reads
    and reveals only, so the row of a plain read carries no key.
    """
    await _register_metadata_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert result.result["password"] == "[REDACTED:param_name]"
    assert result.result["note"] == "not a credential_read op"
    assert _PASSWORD_SECRET not in result.model_dump_json()

    row = await _audit_row("vault.kv.versions")
    assert "reveal_secret" not in row.payload
    assert _PASSWORD_SECRET not in json.dumps(row.raw_payload)


async def test_read_op_reveal_by_a_person_returns_the_field_and_stamps_audit(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret: true`` on a plain read works as on a secret read."""
    await _register_metadata_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    assert result.status == "ok", result.error
    assert result.result["password"] == _PASSWORD_SECRET

    row = await _audit_row("vault.kv.versions")
    assert row.payload["reveal_secret"] is True
    assert _PASSWORD_SECRET not in json.dumps(row.raw_payload)


# ---------------------------------------------------------------------------
# scrub_secret_named_values unit contract
# ---------------------------------------------------------------------------


def test_scrub_secret_named_values_replaces_nested_secret() -> None:
    scrubbed, found = scrub_secret_named_values(
        {"data": {"password": _PASSWORD_SECRET, "username": "root"}, "version": 3}
    )
    assert found is True
    assert scrubbed == {
        "data": {"password": _REDACTED, "username": "root"},
        "version": 3,
    }


def test_scrub_secret_named_values_passthrough_when_no_secret() -> None:
    payload = {"keys": ["app/db", "app/api"], "count": 2}
    scrubbed, found = scrub_secret_named_values(payload)
    assert found is False
    assert scrubbed == payload


# ---------------------------------------------------------------------------
# Name/value pairs
# ---------------------------------------------------------------------------


def test_scrub_hides_the_value_of_a_secret_named_pair() -> None:
    """A ``{"name": "PASSWORD", "value": ...}`` pair hides its value; the others stay."""
    scrubbed, found = scrub_secret_named_values(
        {
            "fields": [
                {"name": "USER", "value": "root"},
                {"name": "PASSWORD", "value": _PASSWORD_SECRET},
                {"name": "apiToken", "value": {"parts": [_PASSWORD_SECRET]}},
            ]
        }
    )
    assert found is True
    assert scrubbed == {
        "fields": [
            {"name": "USER", "value": "root"},
            {"name": "PASSWORD", "value": _REDACTED},
            {"name": "apiToken", "value": _REDACTED},
        ]
    }


@pytest.mark.parametrize(
    "payload",
    [
        # No ``value`` key: nothing to hide.
        {"fields": [{"name": "PASSWORD"}]},
        # A secret-shaped ``name`` on an object that is not a pair.
        {"name": "my-credential", "fields": []},
        # ``name`` is not a string.
        {"name": 7, "value": "kept"},
        # ``None`` and booleans under the pair stay.
        {"name": "PASSWORD", "value": None},
        {"name": "PASSWORD", "value": True},
    ],
)
def test_scrub_keeps_what_is_not_a_secret_named_pair(payload: dict[str, Any]) -> None:
    scrubbed, found = scrub_secret_named_values(payload)
    assert found is False
    assert scrubbed == payload


def test_scrub_hides_a_number_under_a_secret_named_pair_only_when_asked() -> None:
    payload = {"name": "pin_code_password", "value": 1234}
    assert scrub_secret_named_values(payload) == (payload, False)
    assert scrub_secret_named_values(payload, hide_numbers=True) == (
        {"name": "pin_code_password", "value": _REDACTED},
        True,
    )


def test_scrub_keeps_the_pair_of_a_declared_plain_name() -> None:
    payload = {"fields": [{"name": "token", "value": "mode 600, owner root"}]}
    assert scrub_secret_named_values(payload, plain_names=frozenset({"token"})) == (
        payload,
        False,
    )


async def test_read_op_hides_the_value_of_a_secret_named_pair_and_fingerprints_it(
    stub_embedding_service: AsyncMock,
) -> None:
    """The caller's view hides the pair value; the audit row stores its fingerprint."""
    await _register_pair_fields_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={},
    )
    assert result.status == "ok", result.error
    [instance] = result.result["credentialInstances"]
    assert instance["name"] == "my-credential"
    assert instance["fields"] == [
        {"name": "USER", "value": "root"},
        {"name": "PASSWORD", "value": _REDACTED},
    ]
    assert _PASSWORD_SECRET not in result.model_dump_json()

    row = await _audit_row("vault.kv.versions")
    raw = json.dumps(row.raw_payload)
    assert _PASSWORD_SECRET not in raw
    [stored] = row.raw_payload["credentialInstances"]
    assert stored["fields"][0] == {"name": "USER", "value": "root"}
    assert stored["fields"][1]["value"].startswith(_FINGERPRINT)


async def test_read_op_http_error_body_hides_the_value_of_a_secret_named_pair(
    stub_embedding_service: AsyncMock,
) -> None:
    """The vendor's error text keeps the USER pair and hides the PASSWORD pair value.

    The text reaches the caller in the error envelope and the audit row in
    the stored error; neither holds the value.
    """
    await _register_pair_fields_read(stub_embedding_service, _module_pair_fields_http_error_handler)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={},
    )
    assert result.status == "error"
    assert result.extras["http_status"] == 500
    assert json.loads(result.extras["upstream_message"]) == [
        {"name": "USER", "value": "root"},
        {"name": "PASSWORD", "value": _REDACTED},
    ]
    assert _PASSWORD_SECRET not in result.model_dump_json()

    row = await _audit_row("vault.kv.versions")
    stored = json.dumps(
        {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns},
        default=str,
    )
    assert _PASSWORD_SECRET not in stored
    assert row.payload["result_status"] == "error"
    assert _REDACTED in json.dumps(row.payload["error"])


async def test_read_op_http_error_body_hides_the_value_of_a_pair_with_an_escaped_name(
    stub_embedding_service: AsyncMock,
) -> None:
    """A pair name written with a JSON escape is read by what it says.

    The USER pair stays. Both PASSWORD pairs, name first and value first,
    lose their value in the error envelope and in the audit row.
    """
    await _register_pair_fields_read(
        stub_embedding_service, _module_escaped_pair_name_http_error_handler
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={},
    )
    assert result.status == "error"
    assert json.loads(result.extras["upstream_message"]) == [
        {"name": "USER", "value": "root"},
        {"name": "PASSWORD", "value": _REDACTED},
        {"value": _REDACTED, "name": "PASSWORD"},
    ]
    assert _PASSWORD_SECRET not in result.model_dump_json()

    row = await _audit_row("vault.kv.versions")
    stored = json.dumps(
        {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns},
        default=str,
    )
    assert _PASSWORD_SECRET not in stored
    assert row.payload["result_status"] == "error"
    assert _REDACTED in json.dumps(row.payload["error"])


async def test_rest_reveal_route_gives_the_value_once_to_the_person_who_asked(
    stub_embedding_service: AsyncMock,
) -> None:
    """``POST /api/v1/approvals/{id}/reveal``: 200 once, then 409; 403 and 404 for others."""
    await _register_vault_read(stub_embedding_service)
    request_id = await _approved_reveal()

    with pytest.raises(HTTPException) as forbidden:
        await reveal_approved_request(request_id, operator=_REVIEWER)
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        await reveal_approved_request(uuid4(), operator=_make_operator())
    assert missing.value.status_code == 404

    body = await reveal_approved_request(request_id, operator=_make_operator())
    assert body.status == "ok", body.error
    assert body.result is not None
    assert body.result["data"]["password"] == _PASSWORD_SECRET

    with pytest.raises(HTTPException) as again:
        await reveal_approved_request(request_id, operator=_make_operator())
    assert again.value.status_code == 409
    assert str(again.value.detail).startswith("reveal_already_used:")

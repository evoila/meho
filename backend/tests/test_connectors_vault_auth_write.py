# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Vault auth credential-lifecycle write ops (G3.15-T3, #1411).

Covers ``vault.auth.userpass.{write,update_password,delete}`` +
``vault.auth.approle.{write,delete,generate_secret_id}``: registration,
happy paths, mount parameterisation, value-free responses, the
backend-not-mounted structured error, schema-driven param validation,
and -- load-bearing for #1411 -- the **request- and response-side
secret redaction**.

Redaction is enforced at the classification layer (#1401's op-class
allowlists), not in the handlers: the password ops classify
``credential_write`` (secret in request params) and
``generate_secret_id`` classifies ``credential_mint`` (secret in
response). The redaction tests seed a distinctive sentinel and
positively assert the sentinel is ABSENT from the serialised broadcast
payload :func:`~meho_backplane.broadcast.events.redact_payload` would
ship -- mirroring the k8s.secret.create / vault.kv.put sentinel
pattern. The audit row never carries raw params (only a params_hash),
so the secret is structurally absent there by construction.

Mocking discipline mirrors ``test_connectors_vault_auth.py``: the
in-process fake hvac client (``install_fake_client`` ->
``_build_client`` monkeypatch), not an httpx/respx mock, because hvac's
transport is ``requests``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import register_vault_typed_operations
from meho_backplane.connectors.vault.ops_auth_write import (
    vault_auth_approle_generate_secret_id,
    vault_auth_userpass_write,
)
from meho_backplane.connectors.vault.ops_auth_write_schemas import (
    VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars needed by Settings / VaultConnector."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert every Vault typed-op descriptor row (incl. the six auth-write ops)."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_vault(
    op_id: str, params: dict[str, Any], *, jwt: str = "fake.jwt.value"
) -> OperationResult:
    """Dispatch a vault op, bypassing the approval gate.

    The write ops register ``requires_approval=True``; an ordinary
    dispatch would park them in the approval queue. ``_approved=True`` is
    the resume-path flag the approvals API sets after a human approves --
    here it lets the unit test drive the handler/audit/broadcast path
    that runs once a write is authorized.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=True,
    )


# ---------------------------------------------------------------------------
# Registration / dispatchability + requires_approval / safety_level
# ---------------------------------------------------------------------------

_WRITE_OP_IDS: list[str] = [
    "vault.auth.userpass.write",
    "vault.auth.userpass.update_password",
    "vault.auth.userpass.delete",
    "vault.auth.approle.write",
    "vault.auth.approle.delete",
    "vault.auth.approle.generate_secret_id",
]


async def test_auth_write_ops_register_idempotently(
    stub_embedding_service: AsyncMock,
) -> None:
    """All six auth-write ops upsert; a second run is a no-op (body-hash skip)."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_typed_operations(embedding_service=stub_embedding_service)


@pytest.mark.parametrize("op_id", _WRITE_OP_IDS)
async def test_write_ops_register_with_approval_and_expected_safety(
    op_id: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Each op registers requires_approval=True with the stated safety level."""
    expected_safety = {
        "vault.auth.userpass.write": "dangerous",
        "vault.auth.userpass.update_password": "caution",
        "vault.auth.userpass.delete": "dangerous",
        "vault.auth.approle.write": "dangerous",
        "vault.auth.approle.delete": "dangerous",
        "vault.auth.approle.generate_secret_id": "dangerous",
    }
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
        group_key = (
            await session.execute(
                select(OperationGroup.group_key).where(OperationGroup.id == row.group_id)
            )
        ).scalar_one()
    assert row.requires_approval is True, f"{op_id} must require approval"
    assert row.safety_level == expected_safety[op_id]
    assert group_key == "auth"


# ---------------------------------------------------------------------------
# userpass.write — happy path + value-free response + redaction
# ---------------------------------------------------------------------------


async def test_userpass_write_creates_user_value_free_response(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """userpass.write forwards password + token_policies to hvac; response omits the password."""
    fake = install_fake_client(monkeypatch)
    sentinel = "userpass-write-pw-sentinel-1411"

    result = await _dispatch_vault(
        "vault.auth.userpass.write",
        {"username": "ci", "password": sentinel, "token_policies": ["admin", "default"]},
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "username": "ci",
        "mount": "userpass",
        "written": True,
        "token_policies": ["admin", "default"],
    }
    # The password reaches Vault verbatim (the point of the write)...
    call = fake.auth.userpass.write_calls[0]
    assert call["password"] == sentinel
    assert call["token_policies"] == ["admin", "default"]
    assert call["mount_point"] == "userpass"
    # ...but never the op response.
    assert sentinel not in str(result.result)


async def test_userpass_write_honours_non_default_mount(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(
        "vault.auth.userpass.write",
        {"username": "svc", "password": "p", "mount": "userpass-prod"},
    )

    assert result.status == "ok", result.error
    assert result.result["mount"] == "userpass-prod"
    assert fake.auth.userpass.write_calls[0]["mount_point"] == "userpass-prod"


def test_userpass_write_classifies_credential_write_and_redacts_broadcast() -> None:
    """The password in params never reaches the broadcast feed (aggregate-only)."""
    assert classify_op("vault.auth.userpass.write") == "credential_write"
    sentinel = "userpass-write-pw-sentinel-1411"
    raw_params = {"params": {"username": "ci", "password": sentinel, "token_policies": ["x"]}}
    payload = redact_payload("credential_write", raw_params, "ok")
    assert payload == {"op_class": "credential_write", "result_status": "ok"}
    assert sentinel not in str(payload), "password leaked into broadcast payload"


# ---------------------------------------------------------------------------
# userpass.update_password — happy path + redaction
# ---------------------------------------------------------------------------


async def test_userpass_update_password_value_free_response(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    sentinel = "userpass-rotate-pw-sentinel-1411"

    result = await _dispatch_vault(
        "vault.auth.userpass.update_password",
        {"username": "ci", "password": sentinel},
    )

    assert result.status == "ok", result.error
    assert result.result == {"username": "ci", "mount": "userpass", "password_updated": True}
    assert fake.auth.userpass.update_password_calls[0]["password"] == sentinel
    assert sentinel not in str(result.result)


def test_update_password_classifies_credential_write_and_redacts_broadcast() -> None:
    assert classify_op("vault.auth.userpass.update_password") == "credential_write"
    sentinel = "userpass-rotate-pw-sentinel-1411"
    raw_params = {"params": {"username": "ci", "password": sentinel}}
    payload = redact_payload("credential_write", raw_params, "ok")
    assert payload == {"op_class": "credential_write", "result_status": "ok"}
    assert sentinel not in str(payload)


# ---------------------------------------------------------------------------
# userpass.delete — happy path + idempotency framing
# ---------------------------------------------------------------------------


async def test_userpass_delete_removes_user(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.userpass.users = {"ci": {}}

    result = await _dispatch_vault("vault.auth.userpass.delete", {"username": "ci"})

    assert result.status == "ok", result.error
    assert result.result == {"username": "ci", "mount": "userpass", "deleted": True}
    assert fake.auth.userpass.delete_calls == [{"username": "ci", "mount_point": "userpass"}]


# ---------------------------------------------------------------------------
# approle.write / delete — happy paths
# ---------------------------------------------------------------------------


async def test_approle_write_forwards_only_supplied_config(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """Only the config keys the caller supplied flow to hvac (others stay unchanged)."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(
        "vault.auth.approle.write",
        {"role_name": "deploy", "token_policies": ["default"], "token_ttl": 1200},
    )

    assert result.status == "ok", result.error
    assert result.result == {"role_name": "deploy", "mount": "approle", "written": True}
    call = fake.auth.approle.write_calls[0]
    assert call["role_name"] == "deploy"
    assert call["token_policies"] == ["default"]
    assert call["token_ttl"] == 1200
    # secret_id_ttl / bind_secret_id were not supplied -> not forwarded.
    assert "secret_id_ttl" not in call
    assert "bind_secret_id" not in call


async def test_approle_delete_removes_role(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.approle.roles = {"deploy": {}}

    result = await _dispatch_vault("vault.auth.approle.delete", {"role_name": "deploy"})

    assert result.status == "ok", result.error
    assert result.result == {"role_name": "deploy", "mount": "approle", "deleted": True}
    assert fake.auth.approle.delete_calls == [{"role_name": "deploy", "mount_point": "approle"}]


# ---------------------------------------------------------------------------
# approle.generate_secret_id — mints SecretID in response, redacted from broadcast
# ---------------------------------------------------------------------------


async def test_generate_secret_id_returns_minted_secret(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """The minted SecretID reaches the caller's OperationResult (the point of minting)."""
    fake = install_fake_client(monkeypatch)
    sentinel = "minted-secret-id-sentinel-1411"
    fake.auth.approle.secret_id = sentinel

    result = await _dispatch_vault(
        "vault.auth.approle.generate_secret_id",
        {"role_name": "deploy"},
    )

    assert result.status == "ok", result.error
    assert result.result["secret_id"] == sentinel
    assert result.result["role_name"] == "deploy"
    assert result.result["secret_id_accessor"] == "fake-secret-id-accessor"
    assert result.result["secret_id_ttl"] == 600
    assert fake.auth.approle.generate_secret_id_calls[0]["role_name"] == "deploy"


async def test_generate_secret_id_is_non_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """Two calls hit hvac twice — the op mints a fresh SecretID each time."""
    fake = install_fake_client(monkeypatch)

    await _dispatch_vault("vault.auth.approle.generate_secret_id", {"role_name": "deploy"})
    await _dispatch_vault("vault.auth.approle.generate_secret_id", {"role_name": "deploy"})

    assert len(fake.auth.approle.generate_secret_id_calls) == 2


def test_generate_secret_id_classifies_credential_mint_and_redacts_broadcast() -> None:
    """The minted SecretID (response-side) never reaches the broadcast feed."""
    assert classify_op("vault.auth.approle.generate_secret_id") == "credential_mint"
    sentinel = "minted-secret-id-sentinel-1411"
    # The publisher ships request params, but credential_mint collapses to
    # aggregate-only regardless — assert the response secret never appears
    # even if it were (defensively) merged into the publisher's params.
    raw_params = {"params": {"role_name": "deploy"}, "secret_id": sentinel}
    payload = redact_payload("credential_mint", raw_params, "ok")
    assert payload == {"op_class": "credential_mint", "result_status": "ok"}
    assert sentinel not in str(payload), "minted SecretID leaked into broadcast payload"


def test_generate_secret_id_llm_instructions_flag_non_idempotent_mint() -> None:
    """AC3: the llm_instructions flag the non-idempotent, secret-minting nature."""
    blob = str(VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS).lower()
    assert "non-idempotent" in blob
    assert "secret" in blob
    # The instruction explicitly tells the agent each call mints a new value.
    assert "each call" in blob or "distinct" in blob


# ---------------------------------------------------------------------------
# backend-not-mounted -> structured connector_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        (
            "vault.auth.userpass.write",
            {"username": "ci", "password": "p"},
            "userpass",
        ),
        ("vault.auth.userpass.delete", {"username": "ci"}, "userpass"),
        (
            "vault.auth.approle.write",
            {"role_name": "deploy"},
            "approle",
        ),
        ("vault.auth.approle.delete", {"role_name": "deploy"}, "approle"),
    ],
)
async def test_write_backend_not_mounted_surfaces_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """A 404 from an unmounted backend reclassifies to VaultAuthBackendNotMountedError."""
    fake = install_fake_client(monkeypatch)
    backend_fake = getattr(fake.auth, backend)
    backend_fake.write_exc = hvac.exceptions.InvalidPath("no handler for route")
    backend_fake.delete_exc = hvac.exceptions.InvalidPath("no handler for route")

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "VaultAuthBackendNotMountedError"


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        (
            "vault.auth.userpass.update_password",
            {"username": "ci", "password": "p"},
            "userpass",
        ),
        (
            "vault.auth.approle.generate_secret_id",
            {"role_name": "deploy"},
            "approle",
        ),
    ],
)
async def test_probe_reclassifies_backend_absent_on_404(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """update_password / generate_secret_id 404 + LIST-probe 404 -> backend-absent error."""
    fake = install_fake_client(monkeypatch)
    backend_fake = getattr(fake.auth, backend)
    backend_fake.update_password_exc = hvac.exceptions.InvalidPath("no handler")
    backend_fake.generate_secret_id_exc = hvac.exceptions.InvalidPath("no handler")
    backend_fake.list_exc = hvac.exceptions.InvalidPath("no handler")

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultAuthBackendNotMountedError"


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        (
            "vault.auth.userpass.update_password",
            {"username": "ghost", "password": "p"},
            "userpass",
        ),
        (
            "vault.auth.approle.generate_secret_id",
            {"role_name": "ghost"},
            "approle",
        ),
    ],
)
async def test_missing_entity_under_mounted_backend_keeps_invalid_path(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """404 on the op but the LIST probe succeeds (mounted) -> InvalidPath re-raised."""
    fake = install_fake_client(monkeypatch)
    backend_fake = getattr(fake.auth, backend)
    backend_fake.update_password_exc = hvac.exceptions.InvalidPath("no secret found")
    backend_fake.generate_secret_id_exc = hvac.exceptions.InvalidPath("no secret found")
    # list_exc stays None -> the probe LIST succeeds (backend mounted).

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("exception_class") == "InvalidPath"


# ---------------------------------------------------------------------------
# schema-driven param validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,params",
    [
        ("vault.auth.userpass.write", {"username": "ci"}),
        ("vault.auth.userpass.write", {"password": "p"}),
        ("vault.auth.userpass.write", {"username": "ci", "password": "  "}),
        ("vault.auth.userpass.update_password", {"username": "ci"}),
        ("vault.auth.userpass.delete", {}),
        ("vault.auth.approle.write", {}),
        ("vault.auth.approle.write", {"role_name": "r", "token_ttl": -1}),
        ("vault.auth.approle.delete", {"role_name": "  "}),
        ("vault.auth.approle.generate_secret_id", {}),
        ("vault.auth.approle.generate_secret_id", {"role_name": "r", "unexpected": "x"}),
    ],
    ids=[
        "userpass-write-missing-password",
        "userpass-write-missing-username",
        "userpass-write-whitespace-password",
        "update-password-missing-password",
        "userpass-delete-missing-username",
        "approle-write-missing-role",
        "approle-write-negative-ttl",
        "approle-delete-whitespace-role",
        "generate-secret-id-missing-role",
        "generate-secret-id-additional-property",
    ],
)
async def test_invalid_params_rejected_by_schema(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Bad params are rejected by the schema before the handler runs (no Vault call)."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"
    # No write reached the fake backend.
    assert fake.auth.userpass.write_calls == []
    assert fake.auth.approle.write_calls == []
    assert fake.auth.approle.generate_secret_id_calls == []


# ---------------------------------------------------------------------------
# handler-level guards (called directly, no dispatcher)
# ---------------------------------------------------------------------------


async def test_userpass_write_handler_omits_token_policies_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create without token_policies neither forwards the kwarg nor echoes the key."""
    fake = install_fake_client(monkeypatch)

    result = await vault_auth_userpass_write(
        _make_operator(), None, {"username": "ci", "password": "p"}
    )

    assert result == {"username": "ci", "mount": "userpass", "written": True}
    assert "token_policies" not in fake.auth.userpass.write_calls[0]


async def test_generate_secret_id_handler_omits_optional_response_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Vault omits accessor/ttl, the handler returns only the required keys."""
    fake = install_fake_client(monkeypatch)

    def _minimal(role_name: str, mount_point: str = "approle", **_kwargs: Any) -> dict[str, Any]:
        return {"data": {"secret_id": "only-the-id"}}

    monkeypatch.setattr(fake.auth.approle, "generate_secret_id", _minimal)

    result = await vault_auth_approle_generate_secret_id(
        _make_operator(), None, {"role_name": "deploy"}
    )

    assert result == {"secret_id": "only-the-id", "role_name": "deploy", "mount": "approle"}

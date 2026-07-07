# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Vault identity + token ops (G3.15-T4, #1412).

Covers the identity group (``vault.identity.entity.write`` /
``entity_alias.write`` / ``group.write`` / ``group.delete`` /
``entity.read`` / ``group.read`` / ``list``) and the token group
(``vault.token.create`` / ``revoke_accessor`` / ``list_accessors``):
registration with the stated safety_level + requires_approval +
group_key, happy paths, the value-free / normalised responses, and --
load-bearing for #1412 -- the **response-side token redaction** for
``vault.token.create``.

Redaction is enforced at the classification layer (#1397/#1401's
op-class allowlists), not the handlers: ``vault.token.create`` classifies
``credential_mint`` (the minted client token is in the response). The
redaction test seeds a distinctive sentinel token and positively asserts
the sentinel is ABSENT from the payload
:func:`~meho_backplane.broadcast.events.redact_payload` would ship --
mirroring the ``generate_secret_id`` / ``vault.kv.put`` sentinel pattern.
The audit row never carries raw params (only a params_hash), so the
token is structurally absent there by construction.

Mocking discipline mirrors ``test_connectors_vault_auth_write.py``: the
in-process fake hvac client (``install_fake_client`` -> ``_build_client``
monkeypatch), not an httpx/respx mock, because hvac's transport is
``requests``.
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
from meho_backplane.connectors.vault import (
    register_vault_identity_token_operations,
    register_vault_typed_operations,
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
    """Upsert every Vault typed-op descriptor row (incl. the identity + token ops).

    The identity/token ops ship their own registrar (queued from the
    package ``__init__`` alongside the KV / sys / auth registrars), so
    the test calls it explicitly in addition to
    ``register_vault_typed_operations`` to land all the rows the
    dispatch path resolves against.
    """
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_identity_token_operations(embedding_service=stub_embedding_service)
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
    """Dispatch a vault op, bypassing the approval gate for the write ops.

    The write ops register ``requires_approval=True``; an ordinary
    dispatch would park them in the approval queue. ``_approved=True`` is
    the resume-path flag the approvals API sets after a human approves --
    here it drives the handler/audit/broadcast path a write follows once
    authorized. The read ops (no approval) tolerate the flag too.
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
# Registration / dispatchability + requires_approval / safety_level / group
# ---------------------------------------------------------------------------

#: op_id -> (safety_level, requires_approval, group_key) the descriptor must carry.
_EXPECTED: dict[str, tuple[str, bool, str]] = {
    "vault.identity.entity.write": ("dangerous", True, "identity"),
    "vault.identity.entity_alias.write": ("dangerous", True, "identity"),
    "vault.identity.group.write": ("dangerous", True, "identity"),
    "vault.identity.group.delete": ("dangerous", True, "identity"),
    "vault.identity.entity.read": ("safe", False, "identity"),
    "vault.identity.group.read": ("safe", False, "identity"),
    "vault.identity.list": ("safe", False, "identity"),
    "vault.token.create": ("dangerous", True, "token"),
    "vault.token.revoke_accessor": ("dangerous", True, "token"),
    "vault.token.list_accessors": ("safe", False, "token"),
}


async def test_identity_token_ops_register_idempotently(
    stub_embedding_service: AsyncMock,
) -> None:
    """All ten ops upsert; a second run is a no-op (body-hash skip)."""
    await register_vault_identity_token_operations(embedding_service=stub_embedding_service)
    await register_vault_identity_token_operations(embedding_service=stub_embedding_service)


@pytest.mark.parametrize("op_id", list(_EXPECTED))
async def test_ops_register_with_expected_safety_approval_group(
    op_id: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Each op registers with the stated safety_level, approval flag, and group."""
    expected_safety, expected_approval, expected_group = _EXPECTED[op_id]
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
    assert row.safety_level == expected_safety
    assert row.requires_approval is expected_approval
    assert group_key == expected_group


def test_no_bulk_revoke_op_registered() -> None:
    """The vault skill's loudest Don't-rule: no bulk-revoke verb exists."""
    op_ids = set(_EXPECTED)
    assert not any("revoke" in op and "accessor" not in op for op in op_ids)
    assert "vault.token.revoke" not in op_ids
    assert "vault.token.revoke_all" not in op_ids


# ---------------------------------------------------------------------------
# identity.entity.write
# ---------------------------------------------------------------------------


async def test_entity_write_creates_and_returns_minted_id(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """entity.write forwards supplied fields to hvac; returns the minted entity_id on create."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.minted_id = "ent-123"

    result = await _dispatch_vault(
        "vault.identity.entity.write",
        {"name": "svc-meho", "policies": ["admin"], "metadata": {"team": "ops"}},
    )

    assert result.status == "ok", result.error
    assert result.result == {"name": "svc-meho", "entity_id": "ent-123", "written": True}
    call = fake.secrets.identity.entity_write_calls[0]
    assert call["name"] == "svc-meho"
    assert call["policies"] == ["admin"]
    assert call["metadata"] == {"team": "ops"}
    # Optional fields the caller omitted are not forwarded.
    assert "disabled" not in call


async def test_entity_write_update_returns_null_id(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A pure update (Vault 204 / no body) yields entity_id=None but written=True."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.minted_id = None

    result = await _dispatch_vault(
        "vault.identity.entity.write",
        {"name": "svc-meho", "entity_id": "ent-123"},
    )

    assert result.status == "ok", result.error
    assert result.result == {"name": "svc-meho", "entity_id": None, "written": True}
    assert fake.secrets.identity.entity_write_calls[0]["entity_id"] == "ent-123"


# ---------------------------------------------------------------------------
# identity.entity_alias.write
# ---------------------------------------------------------------------------


async def test_entity_alias_write_forwards_required_fields(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.minted_id = "alias-9"

    result = await _dispatch_vault(
        "vault.identity.entity_alias.write",
        {"name": "ci-operator", "canonical_id": "ent-123", "mount_accessor": "auth_userpass_abc"},
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "name": "ci-operator",
        "canonical_id": "ent-123",
        "alias_id": "alias-9",
        "written": True,
    }
    call = fake.secrets.identity.entity_alias_write_calls[0]
    assert call["name"] == "ci-operator"
    assert call["canonical_id"] == "ent-123"
    assert call["mount_accessor"] == "auth_userpass_abc"


# ---------------------------------------------------------------------------
# identity.group.write / delete
# ---------------------------------------------------------------------------


async def test_group_write_forwards_membership(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """group.write forwards policies + member ids (membership = privilege plumbing)."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.minted_id = "grp-7"

    result = await _dispatch_vault(
        "vault.identity.group.write",
        {
            "name": "ops-admins",
            "policies": ["admin"],
            "member_entity_ids": ["ent-123", "ent-456"],
        },
    )

    assert result.status == "ok", result.error
    assert result.result == {"name": "ops-admins", "group_id": "grp-7", "written": True}
    call = fake.secrets.identity.group_write_calls[0]
    assert call["policies"] == ["admin"]
    assert call["member_entity_ids"] == ["ent-123", "ent-456"]


async def test_group_delete_is_value_free(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault("vault.identity.group.delete", {"name": "ops-admins"})

    assert result.status == "ok", result.error
    assert result.result == {"name": "ops-admins", "deleted": True}
    assert fake.secrets.identity.group_delete_calls == ["ops-admins"]


# ---------------------------------------------------------------------------
# identity reads
# ---------------------------------------------------------------------------


async def test_entity_read_unwraps_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.entity_payload = {
        "data": {"name": "svc-meho", "policies": ["admin"], "aliases": []}
    }

    result = await _dispatch_vault("vault.identity.entity.read", {"entity_id": "ent-123"})

    assert result.status == "ok", result.error
    assert result.result == {"name": "svc-meho", "policies": ["admin"], "aliases": []}
    assert fake.secrets.identity.read_entity_calls == ["ent-123"]


async def test_group_read_unwraps_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.group_payload = {
        "data": {"name": "ops-admins", "type": "internal", "member_entity_ids": ["ent-123"]}
    }

    result = await _dispatch_vault("vault.identity.group.read", {"name": "ops-admins"})

    assert result.status == "ok", result.error
    assert result.result["member_entity_ids"] == ["ent-123"]
    assert fake.secrets.identity.read_group_calls == ["ops-admins"]


async def test_list_groups_default_kind(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.groups_payload = {"data": {"keys": ["grp-1", "grp-2"]}}

    result = await _dispatch_vault("vault.identity.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"kind": "groups", "keys": ["grp-1", "grp-2"]}


async def test_list_entities_kind(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.entities_payload = {"data": {"keys": ["ent-1"]}}

    result = await _dispatch_vault("vault.identity.list", {"kind": "entities"})

    assert result.status == "ok", result.error
    assert result.result == {"kind": "entities", "keys": ["ent-1"]}


async def test_list_empty_store_normalises_to_empty_keys(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """An identity store with zero groups LISTs as a 404 -> {'keys': []}, not an error."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_list_groups = hvac.exceptions.InvalidPath("no groups")

    result = await _dispatch_vault("vault.identity.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"kind": "groups", "keys": []}


# ---------------------------------------------------------------------------
# token.create — happy path + response-side redaction
# ---------------------------------------------------------------------------


async def test_token_create_returns_token_and_accessor(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """token.create forwards policies/ttl and returns the minted token + accessor."""
    fake = install_fake_client(monkeypatch)
    fake.auth.token.minted_client_token = "s.minted-token-1412"
    fake.auth.token.minted_accessor = "acc-1412"

    result = await _dispatch_vault(
        "vault.token.create",
        {"policies": ["default"], "ttl": "1h", "num_uses": 1},
    )

    assert result.status == "ok", result.error
    assert result.result["client_token"] == "s.minted-token-1412"
    assert result.result["accessor"] == "acc-1412"
    assert result.result["policies"] == ["default"]
    call = fake.auth.token.create_calls[0]
    assert call["policies"] == ["default"]
    assert call["ttl"] == "1h"
    assert call["num_uses"] == 1


async def test_token_create_maps_ttl_period_to_hvac_period(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """The schema's ``ttl_period`` maps onto hvac's ``period=`` (periodic token)."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault("vault.token.create", {"ttl_period": "24h"})

    assert result.status == "ok", result.error
    call = fake.auth.token.create_calls[0]
    assert call["period"] == "24h"
    assert "ttl_period" not in call


def test_token_create_classifies_credential_mint_and_redacts_broadcast() -> None:
    """The minted token in the response never reaches the broadcast feed (aggregate-only).

    The broadcast publisher ships the request params, not the response,
    but credential_mint collapses the whole event to aggregate-only as a
    belt-and-suspenders guard. classify_op must consult the allowlist
    BEFORE the ``.create`` write-suffix.
    """
    assert classify_op("vault.token.create") == "credential_mint"
    sentinel = "s.minted-token-1412-sentinel"
    raw_params = {"params": {"policies": ["default"], "token_echo": sentinel}}
    payload = redact_payload("credential_mint", raw_params, "ok")
    assert payload == {"op_class": "credential_mint", "result_status": "ok"}
    assert sentinel not in str(payload), "minted token leaked into broadcast payload"


async def test_token_create_token_absent_from_response_str_only_on_purpose(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """The token IS in the OperationResult (the point of minting) — sanity guard.

    Distinct from the redaction test: the caller's result must carry the
    secret so it can be stored. The redaction is the broadcast/audit
    layer's job, asserted in the dispatch-level integration test.
    """
    fake = install_fake_client(monkeypatch)
    fake.auth.token.minted_client_token = "s.must-reach-caller"

    result = await _dispatch_vault("vault.token.create", {})

    assert result.status == "ok", result.error
    assert result.result["client_token"] == "s.must-reach-caller"


# ---------------------------------------------------------------------------
# token.revoke_accessor / list_accessors
# ---------------------------------------------------------------------------


async def test_revoke_accessor_is_surgical(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """revoke_accessor revokes exactly the one accessor passed."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault("vault.token.revoke_accessor", {"accessor": "acc-1412"})

    assert result.status == "ok", result.error
    assert result.result == {"accessor": "acc-1412", "revoked": True}
    assert fake.auth.token.revoke_accessor_calls == ["acc-1412"]


async def test_list_accessors_unwraps_keys(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.accessors_payload = {"data": {"keys": ["acc-1", "acc-2"]}}

    result = await _dispatch_vault("vault.token.list_accessors", {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["acc-1", "acc-2"]}


async def test_list_accessors_empty_store_normalises(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.raise_on_list_accessors = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault("vault.token.list_accessors", {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": []}


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


async def test_entity_read_missing_surfaces_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A missing entity id surfaces as a connector_error naming InvalidPath."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_read_entity = hvac.exceptions.InvalidPath("nope")

    result = await _dispatch_vault("vault.identity.entity.read", {"entity_id": "ghost"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"


async def test_group_read_missing_surfaces_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A missing group name surfaces as a connector_error naming InvalidPath.

    Vault answers a read of an absent group with a 404, which hvac raises
    as InvalidPath; vault.identity.group.read deliberately lets that
    surface (mirrors the entity.read posture) rather than masking it as a
    structured found:false result.
    """
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_read_group = hvac.exceptions.InvalidPath("gone")

    result = await _dispatch_vault("vault.identity.group.read", {"name": "ghost-group"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"


async def test_token_create_failure_surfaces_connector_vault_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A Vault ACL denial surfaces as the structured ``connector_vault_forbidden``.

    #2091: :exc:`hvac.exceptions.Forbidden` is classified ahead of the
    generic ``connector_error`` flatten (pre-#2091 this asserted the bare
    ``connector_error: Forbidden`` shape). This dispatch carries no
    target (typed ``vault.*`` op), so the builder's target-less generic
    shape applies — no fabricated ``secret_ref`` diagnosis.
    """
    fake = install_fake_client(monkeypatch)
    fake.auth.token.raise_on_create = hvac.exceptions.Forbidden("denied")

    result = await _dispatch_vault("vault.token.create", {"policies": ["root"]})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_vault_forbidden"
    assert result.extras["exception_class"] == "Forbidden"
    assert result.extras["secret_ref"] is None
    assert result.error is not None
    assert result.error.startswith("connector_vault_forbidden:")

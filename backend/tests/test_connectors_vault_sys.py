# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Vault ``sys`` read op group — G3.3-T2 (#546).

Coverage matrix (one behaviour per test, table-driven where the shape
repeats):

* All four sys ops register into ``endpoint_descriptor`` under
  ``(product="vault", version="1.x", impl_id="vault")`` with
  ``safety_level='safe'``, the ``sys`` operation group, and an
  empty-object ``parameter_schema``.
* ``classify_op`` maps every sys op-id to ``"read"`` (DoD: the audit /
  broadcast ``op_class`` for these ops is ``read``).
* ``vault.sys.health`` happy path returns the shared probe-path
  classification (``ok`` / ``detail`` from
  :func:`~meho_backplane.auth.vault._classify_health_response`) plus
  the descriptive fields, and drives the same ``_build_client`` /
  ``read_health_status(method="GET")`` seam the connector ``probe``
  uses — proving it does not duplicate the health logic.
* ``vault.sys.health`` against a sealed Vault returns ``ok=False``.
* ``vault.sys.health`` against an unreachable Vault surfaces the
  dispatcher's structured ``connector_error`` (no raw traceback to
  the agent).
* ``vault.sys.seal_status`` / ``mounts.list`` / ``auth.list`` happy
  paths return the expected payload shape and forward the operator JWT
  via the ``vault_client_for_operator`` login/revoke seam.
* Login failure (Vault unreachable / role denied) on the three
  authenticated ops surfaces as ``connector_error`` with the
  :class:`VaultClientError` subclass name in
  ``extras["exception_class"]``.

Test isolation mirrors ``test_connectors_vault.py``: the production
code builds Vault clients through the single ``_build_client`` seam;
the shared ``tests/_vault_fakes.py`` ``install_fake_client`` helper
monkey-patches it to a controllable in-process fake — no real HTTP,
no Vault container. The ``_clean_vault_registry`` fixture re-registers
``VaultConnector`` (v2) and resets the dispatcher caches because
alphabetically-earlier test files clear both registry layers via their
own autouse fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.broadcast import classify_op
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.vault import (
    VaultConnector,
    VaultTarget,
    register_vault_sys_typed_operations,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    """Re-register VaultConnector (v2) + reset the dispatcher caches."""
    clear_registry()
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


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
async def _registered_vault_sys_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the sys-op descriptor rows for tests that drive ``execute``."""
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_target(jwt: str = "fake.jwt.value") -> VaultTarget:
    return VaultTarget(raw_jwt=jwt)


_SYS_OP_IDS = (
    "vault.sys.health",
    "vault.sys.seal_status",
    "vault.sys.mounts.list",
    "vault.sys.auth.list",
)


# ---------------------------------------------------------------------------
# Registration + classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _SYS_OP_IDS)
async def test_sys_op_registers_with_safe_level_and_sys_group(
    op_id: str,
    _registered_vault_sys_ops: None,
) -> None:
    """Each sys op lands an endpoint_descriptor row: safe, group 'sys', empty schema."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor, OperationGroup

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "vault",
                    EndpointDescriptor.version == "1.x",
                    EndpointDescriptor.impl_id == "vault",
                    EndpointDescriptor.op_id == op_id,
                )
            )
        ).scalar_one()

        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert row.parameter_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

        group = (
            await session.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
        ).scalar_one()
        assert group.group_key == "sys"


@pytest.mark.parametrize("op_id", _SYS_OP_IDS)
def test_sys_op_classifies_as_read(op_id: str) -> None:
    """DoD: op_class for every sys op is ``read`` (no secret content)."""
    assert classify_op(op_id) == "read"


# ---------------------------------------------------------------------------
# vault.sys.health — shares the probe-path implementation
# ---------------------------------------------------------------------------


async def test_health_happy_path_returns_classified_payload(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """Healthy Vault → ok=True with the shared classifier's detail + descriptive fields."""
    fake = install_fake_client(
        monkeypatch,
        health_payload={
            "initialized": True,
            "sealed": False,
            "version": "1.18.0",
            "cluster_name": "meho-vault",
        },
    )
    result = await VaultConnector().execute(_make_target(), "vault.sys.health", {})

    assert result.status == "ok", result.error
    assert result.result == {
        "ok": True,
        "detail": "sealed=False",
        "version": "1.18.0",
        "cluster_name": "meho-vault",
        "sealed": False,
        "initialized": True,
    }
    # Proves the op reuses the probe-path seam: the unauthenticated
    # _build_client + read_health_status(method="GET") path, NOT a
    # per-operator OIDC login.
    assert fake.sys.read_calls == [{"method": "GET"}]
    assert fake.auth.jwt.login_calls == []


async def test_health_sealed_vault_returns_ok_false(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """Sealed Vault → ok=False / detail='sealed' from the shared classifier."""
    install_fake_client(
        monkeypatch,
        health_payload={"initialized": True, "sealed": True},
    )
    result = await VaultConnector().execute(_make_target(), "vault.sys.health", {})

    assert result.status == "ok", result.error
    assert result.result["ok"] is False
    assert result.result["detail"] == "sealed"
    assert result.result["sealed"] is True


async def test_health_unreachable_vault_surfaces_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """Unreachable Vault → dispatcher connector_error, not a raw traceback."""
    install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.ConnectionError("dns failure"),
    )
    result = await VaultConnector().execute(_make_target(), "vault.sys.health", {})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "ConnectionError"


# ---------------------------------------------------------------------------
# vault.sys.seal_status / mounts.list / auth.list — authenticated reads
# ---------------------------------------------------------------------------


async def test_seal_status_happy_path_returns_raw_object(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """seal_status returns the raw seal-status object and forwards the operator JWT."""
    seal = {
        "type": "shamir",
        "initialized": True,
        "sealed": False,
        "t": 3,
        "n": 5,
        "progress": 0,
        "version": "1.18.0",
    }
    fake = install_fake_client(monkeypatch, seal_status_payload=seal)
    result = await VaultConnector().execute(_make_target(jwt="op-jwt"), "vault.sys.seal_status", {})

    assert result.status == "ok", result.error
    assert result.result == seal
    assert fake.sys.seal_status_calls == 1
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert fake.auth.token.revoke_calls == 1


async def test_mounts_list_unwraps_envelope_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """mounts.list returns the envelope's ``data`` under a ``mounts`` key."""
    mounts_data = {
        "secret/": {"type": "kv", "options": {"version": "2"}},
        "cubbyhole/": {"type": "cubbyhole"},
    }
    install_fake_client(
        monkeypatch,
        mounts_payload={"request_id": "abc", "data": mounts_data, "warnings": None},
    )
    result = await VaultConnector().execute(_make_target(), "vault.sys.mounts.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"mounts": mounts_data}


async def test_auth_list_unwraps_envelope_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """auth.list returns the envelope's ``data`` under an ``auth_methods`` key."""
    auth_data = {
        "token/": {"type": "token"},
        "userpass/": {"type": "userpass"},
    }
    install_fake_client(
        monkeypatch,
        auth_methods_payload={"request_id": "def", "data": auth_data, "warnings": None},
    )
    result = await VaultConnector().execute(_make_target(), "vault.sys.auth.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"auth_methods": auth_data}


@pytest.mark.parametrize(
    ("op_id", "kwargs"),
    [
        ("vault.sys.seal_status", {"seal_status_payload": {"sealed": False}}),
        ("vault.sys.mounts.list", {"mounts_payload": {"data": {}}}),
        ("vault.sys.auth.list", {"auth_methods_payload": {"data": {}}}),
    ],
)
@pytest.mark.parametrize(
    ("login_exc", "expected_exc_class"),
    [
        (requests.exceptions.ConnectionError("no route"), "VaultUnreachableError"),
        (hvac.exceptions.Forbidden("role denied"), "VaultRoleDeniedError"),
    ],
    ids=["unreachable", "role-denied"],
)
async def test_authenticated_sys_op_login_failure_surfaces_vault_client_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    kwargs: dict[str, Any],
    login_exc: Exception,
    expected_exc_class: str,
    _registered_vault_sys_ops: None,
) -> None:
    """Login failure on an authenticated sys op → connector_error w/ VaultClientError class."""
    install_fake_client(monkeypatch, login_exc=login_exc, **kwargs)
    result = await VaultConnector().execute(_make_target(), op_id, {})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == expected_exc_class

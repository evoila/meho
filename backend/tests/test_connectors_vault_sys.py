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

ACL-policy ops (G3.15-T2 #1410) add:

* ``policy.read`` / ``policy.list`` register safe + ``requires_approval``
  False; ``policy.write`` / ``policy.delete`` register dangerous +
  ``requires_approval`` True, all in the ``sys`` group.
* ``classify_op`` maps the reads to ``read`` and the writes to ``write``.
* ``policy.read`` unwraps the ``data.rules`` envelope (modern + legacy
  top-level shapes; null when absent); ``policy.list`` returns the
  policy-name array; the write/delete handlers forward to hvac and
  synthesize the 204-success payload.
* A ``policy.write`` / ``policy.delete`` *dispatch* is parked as
  ``awaiting_approval`` by the G11.7 policy gate before the handler runs;
  the handler's own logic is exercised by calling it directly.
* Schema rejects slash/blank names, empty/missing bodies, and stray keys
  (validation runs ahead of the approval gate). Vault-side / login
  failures surface structurally.

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
from uuid import UUID

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import classify_op
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_sys_typed_operations,
)
from meho_backplane.connectors.vault.ops_sys_bootstrap import (
    vault_sys_auth_enable,
    vault_sys_auth_tune,
    vault_sys_mounts_enable,
    vault_sys_mounts_tune,
)
from meho_backplane.connectors.vault.ops_sys_policy import (
    vault_sys_policy_delete,
    vault_sys_policy_write,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
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


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """Request-scoped operator carrying the bearer token the vault
    handlers forward to Vault's JWT/OIDC auth (G0.8-T3 #629). Replaces
    the pre-#224 ``VaultTarget(raw_jwt=...)`` stub.
    """
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
    """Dispatch a vault op through the real operator-aware path.

    Mirrors ``/api/v1/operations/call`` / MCP ``call_operation``: the
    dispatcher threads a real :class:`Operator`, resolves the connector
    by ``connector_id``, and ``target`` is ``None`` (vault connection
    params come from settings). The handler reads the JWT from
    ``operator.raw_jwt`` — the #629 contract.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


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
    result = await _dispatch_vault("vault.sys.health", {})

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
    result = await _dispatch_vault("vault.sys.health", {})

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
    result = await _dispatch_vault("vault.sys.health", {})

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
    result = await _dispatch_vault("vault.sys.seal_status", {}, jwt="op-jwt")

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
    result = await _dispatch_vault("vault.sys.mounts.list", {})

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
    result = await _dispatch_vault("vault.sys.auth.list", {})

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
    result = await _dispatch_vault(op_id, {})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == expected_exc_class


# ---------------------------------------------------------------------------
# vault.sys.policy.* — ACL-policy ops (G3.15-T2 #1410)
# ---------------------------------------------------------------------------


_POLICY_SAFE_OP_IDS = (
    "vault.sys.policy.read",
    "vault.sys.policy.list",
)
_POLICY_DANGEROUS_OP_IDS = (
    "vault.sys.policy.write",
    "vault.sys.policy.delete",
)


async def _policy_descriptor(op_id: str) -> Any:
    """Fetch the endpoint_descriptor + its group for a policy op."""
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
        group = (
            await session.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
        ).scalar_one()
        return row, group


@pytest.mark.parametrize("op_id", _POLICY_SAFE_OP_IDS)
async def test_policy_read_ops_register_safe_no_approval(
    op_id: str,
    _registered_vault_sys_ops: None,
) -> None:
    """policy.read / policy.list register safe, group 'sys', no approval."""
    row, group = await _policy_descriptor(op_id)
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert group.group_key == "sys"


@pytest.mark.parametrize("op_id", _POLICY_DANGEROUS_OP_IDS)
async def test_policy_write_ops_register_dangerous_with_approval(
    op_id: str,
    _registered_vault_sys_ops: None,
) -> None:
    """policy.write / policy.delete register dangerous + requires_approval."""
    row, group = await _policy_descriptor(op_id)
    assert row.source_kind == "typed"
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert group.group_key == "sys"


@pytest.mark.parametrize(
    ("op_id", "expected"),
    [
        # ``policy.read``'s only param is the policy name; ``.read`` is
        # deliberately not a read-suffix (would over-match vault.kv.read),
        # so it classifies ``other`` like the vault.auth.*.read ops.
        ("vault.sys.policy.read", "other"),
        ("vault.sys.policy.list", "read"),
        ("vault.sys.policy.write", "write"),
        ("vault.sys.policy.delete", "write"),
    ],
)
def test_policy_ops_classify(op_id: str, expected: str) -> None:
    """policy.list → read; policy.read → other; writes/deletes redact under ``write``."""
    assert classify_op(op_id) == expected


async def test_policy_read_returns_name_and_rules(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """policy.read unwraps the envelope ``data.rules`` and forwards the JWT."""
    rules = 'path "secret/data/*" {\n  capabilities = ["read"]\n}\n'
    fake = install_fake_client(
        monkeypatch,
        policy_read_payload={"data": {"name": "meho-mcp", "rules": rules}},
    )
    result = await _dispatch_vault("vault.sys.policy.read", {"name": "meho-mcp"}, jwt="op-jwt")

    assert result.status == "ok", result.error
    assert result.result == {"name": "meho-mcp", "rules": rules}
    assert fake.sys.policy_read_calls == [{"name": "meho-mcp"}]
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert fake.auth.token.revoke_calls == 1


async def test_policy_read_accepts_top_level_rules(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """Legacy Vault returns ``rules`` at the envelope top level (no ``data``)."""
    install_fake_client(
        monkeypatch,
        policy_read_payload={"name": "default", "rules": "# default policy\n"},
    )
    result = await _dispatch_vault("vault.sys.policy.read", {"name": "default"})

    assert result.status == "ok", result.error
    assert result.result == {"name": "default", "rules": "# default policy\n"}


async def test_policy_read_missing_body_yields_null_rules(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """A response carrying no rules surfaces ``rules=None`` (not a crash)."""
    install_fake_client(monkeypatch, policy_read_payload={"data": {"name": "empty"}})
    result = await _dispatch_vault("vault.sys.policy.read", {"name": "empty"})

    assert result.status == "ok", result.error
    assert result.result == {"name": "empty", "rules": None}


async def test_policy_list_unwraps_envelope_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """policy.list returns the policy names from the envelope ``data``."""
    names = ["default", "meho-mcp", "root"]
    fake = install_fake_client(
        monkeypatch,
        policy_list_payload={"data": {"policies": names}, "policies": names},
    )
    result = await _dispatch_vault("vault.sys.policy.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"policies": names}
    assert fake.sys.policy_list_calls == 1


# ``policy.write`` / ``policy.delete`` are ``requires_approval=True``, so a
# full ``dispatch()`` for a human/service principal is intercepted by the
# G11.7 policy gate and parked as ``awaiting_approval`` *before* the
# handler runs (see ``test_policy_write_ops_are_approval_gated_on_dispatch``).
# The handler's hvac-forwarding + payload-shape + error contract is
# therefore exercised by calling the handler directly with the fake
# client installed.


async def test_policy_write_handler_forwards_body_and_returns_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy.write handler forwards name+body to hvac and reports written=True."""
    body = 'path "secret/data/app/*" {\n  capabilities = ["read", "list"]\n}\n'
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_policy_write(
        _make_operator("op-jwt"), None, {"name": "app-ro", "policy": body}
    )

    assert result == {"name": "app-ro", "written": True}
    assert fake.sys.policy_write_calls == [{"name": "app-ro", "policy": body, "pretty_print": True}]
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert fake.auth.token.revoke_calls == 1


async def test_policy_delete_handler_forwards_name_and_returns_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy.delete handler forwards the name and reports deleted=True."""
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_policy_delete(_make_operator(), None, {"name": "app-ro"})

    assert result == {"name": "app-ro", "deleted": True}
    assert fake.sys.policy_delete_calls == [{"name": "app-ro"}]
    assert fake.auth.token.revoke_calls == 1


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vault.sys.policy.write", {"name": "app-ro", "policy": "# body\n"}),
        ("vault.sys.policy.delete", {"name": "app-ro"}),
    ],
)
async def test_policy_write_ops_are_approval_gated_on_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_sys_ops: None,
) -> None:
    """DoD: a write/delete dispatch parks as ``awaiting_approval`` (handler never runs)."""
    fake = install_fake_client(monkeypatch)
    result = await _dispatch_vault(op_id, params)

    assert result.status == "awaiting_approval", result.error
    assert result.extras.get("error_code") == "awaiting_approval"
    assert result.extras.get("approval_request_id")
    # The handler must not have reached Vault — the gate parks first.
    assert fake.sys.policy_write_calls == []
    assert fake.sys.policy_delete_calls == []


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vault.sys.policy.read", {"name": "secret/data"}),
        ("vault.sys.policy.read", {"name": "  "}),
        ("vault.sys.policy.read", {}),
        ("vault.sys.policy.write", {"name": "ok", "policy": ""}),
        ("vault.sys.policy.write", {"name": "ok"}),
        ("vault.sys.policy.write", {"name": "secret/x", "policy": "y"}),
        ("vault.sys.policy.delete", {"name": "secret/x"}),
        ("vault.sys.policy.list", {"name": "x"}),
    ],
)
async def test_policy_op_rejects_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_sys_ops: None,
) -> None:
    """Schema rejects slash/blank names, empty/missing bodies, stray keys.

    Validation runs ahead of the approval gate, so even the
    ``requires_approval`` write/delete ops surface ``invalid_params``
    rather than ``awaiting_approval`` for a malformed call.
    """
    install_fake_client(monkeypatch)
    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"


# --- read/list error + login-failure: these dispatch all the way to the
# handler (they are not approval-gated), so the dispatcher's connector_error
# branch is exercised end to end. ---


@pytest.mark.parametrize(
    ("op_id", "params", "exc_kwarg"),
    [
        ("vault.sys.policy.read", {"name": "x"}, "policy_read_exc"),
        ("vault.sys.policy.list", {}, "policy_list_exc"),
    ],
)
async def test_policy_read_op_vault_error_surfaces_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    exc_kwarg: str,
    _registered_vault_sys_ops: None,
) -> None:
    """A Vault-side error on a read op → connector_error, no traceback."""
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: hvac.exceptions.InvalidRequest("failed to parse policy")},
    )
    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "InvalidRequest"


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vault.sys.policy.read", {"name": "x"}),
        ("vault.sys.policy.list", {}),
    ],
)
async def test_policy_read_op_login_failure_surfaces_vault_client_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_sys_ops: None,
) -> None:
    """Login-phase failure on a read op → connector_error w/ VaultClientError class."""
    install_fake_client(monkeypatch, login_exc=hvac.exceptions.Forbidden("role denied"))
    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultRoleDeniedError"


# --- write/delete handler-level error propagation (the handler runs only
# after approval in production; here we drive it directly to prove it
# re-raises Vault errors for the dispatcher's connector_error branch). ---


@pytest.mark.parametrize(
    ("handler", "params", "exc_kwarg"),
    [
        (vault_sys_policy_write, {"name": "x", "policy": "bad"}, "policy_write_exc"),
        (vault_sys_policy_delete, {"name": "x"}, "policy_delete_exc"),
    ],
)
async def test_policy_write_handler_reraises_vault_error(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    params: dict[str, Any],
    exc_kwarg: str,
) -> None:
    """The write/delete handler propagates a Vault-side error to the caller.

    The dispatcher's ``connector_error`` branch (proven for the read ops
    above) then turns this into a structured result; the handler's
    contract is simply "raise on failure".
    """
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: hvac.exceptions.InvalidRequest("failed to parse policy")},
    )
    with pytest.raises(hvac.exceptions.InvalidRequest):
        await handler(_make_operator(), None, params)


# ---------------------------------------------------------------------------
# vault.sys.{auth,mounts}.{enable,tune} — sys bootstrap writes (G3.15-T5 #1413)
# ---------------------------------------------------------------------------


_BOOTSTRAP_DANGEROUS_OP_IDS = (
    "vault.sys.auth.enable",
    "vault.sys.mounts.enable",
)
_BOOTSTRAP_CAUTION_OP_IDS = (
    "vault.sys.auth.tune",
    "vault.sys.mounts.tune",
)
_BOOTSTRAP_OP_IDS = _BOOTSTRAP_DANGEROUS_OP_IDS + _BOOTSTRAP_CAUTION_OP_IDS


@pytest.mark.parametrize(
    ("op_id", "expected_level"),
    [
        ("vault.sys.auth.enable", "dangerous"),
        ("vault.sys.mounts.enable", "dangerous"),
        ("vault.sys.auth.tune", "caution"),
        ("vault.sys.mounts.tune", "caution"),
    ],
)
async def test_bootstrap_ops_register_with_safety_and_approval(
    op_id: str,
    expected_level: str,
    _registered_vault_sys_ops: None,
) -> None:
    """enable=dangerous, tune=caution; all four approval-gated, group 'sys'."""
    row, group = await _policy_descriptor(op_id)
    assert row.source_kind == "typed"
    assert row.safety_level == expected_level
    assert row.requires_approval is True
    assert group.group_key == "sys"


@pytest.mark.parametrize("op_id", _BOOTSTRAP_OP_IDS)
def test_bootstrap_ops_classify_as_other(op_id: str) -> None:
    """``.enable`` / ``.tune`` are not write/read suffixes → ``other``.

    Deliberate: adding ``.enable`` to the broadcast classifier's
    write-suffix tuple would reclassify the unrelated
    ``meho.connector.enable`` MCP admin tool from ``other`` to ``write``.
    None of these bootstrap ops carry secret material in their params, so
    the full-detail ``other`` broadcast leaks nothing — classifying them
    ``other`` is both the cleaner and the more scoped choice
    (see ``docs/codebase/connectors-vault.md``).
    """
    assert classify_op(op_id) == "other"


def test_adding_bootstrap_suffixes_would_not_reclassify_connector_admin() -> None:
    """Guard: ``meho.connector.enable`` stays ``other`` (collision sentinel).

    The broadcast op_class for the MCP connector-admin tools is derived
    from ``classify_op(op_id)`` on the tool name. This pins the decision
    NOT to add ``.enable`` to ``_WRITE_SUFFIXES`` — if a future change
    does, this assertion fails and forces a re-audit of the collision.
    """
    assert classify_op("meho.connector.enable") == "other"


async def test_auth_enable_handler_forwards_and_returns_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth.enable forwards type/path/description and reports created=True."""
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_auth_enable(
        _make_operator("op-jwt"),
        None,
        {"method_type": "userpass", "path": "userpass", "description": "ci login"},
    )

    assert result == {"path": "userpass", "method_type": "userpass", "created": True}
    assert fake.sys.auth_enable_calls == [
        {"method_type": "userpass", "path": "userpass", "description": "ci login"}
    ]
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert fake.auth.token.revoke_calls == 1


async def test_mounts_enable_handler_forwards_and_returns_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mounts.enable forwards type/path and reports created=True (no description)."""
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_mounts_enable(
        _make_operator(), None, {"backend_type": "transit", "path": "transit"}
    )

    assert result == {"path": "transit", "backend_type": "transit", "created": True}
    assert fake.sys.mount_enable_calls == [
        {"backend_type": "transit", "path": "transit", "description": None}
    ]


@pytest.mark.parametrize(
    ("handler", "params", "exc_kwarg", "expected"),
    [
        (
            vault_sys_auth_enable,
            {"method_type": "userpass", "path": "userpass"},
            "auth_enable_exc",
            {"path": "userpass", "method_type": "userpass", "created": False},
        ),
        (
            vault_sys_mounts_enable,
            {"backend_type": "kv", "path": "secret"},
            "mount_enable_exc",
            {"path": "secret", "backend_type": "kv", "created": False},
        ),
    ],
)
async def test_enable_handler_is_idempotent_on_path_already_in_use(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    params: dict[str, Any],
    exc_kwarg: str,
    expected: dict[str, Any],
) -> None:
    """A duplicate enable (400 'path is already in use') → created=False, not error."""
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: hvac.exceptions.InvalidRequest("path is already in use at userpass/")},
    )
    result = await handler(_make_operator(), None, params)
    assert result == expected


@pytest.mark.parametrize(
    ("handler", "params", "exc_kwarg"),
    [
        (vault_sys_auth_enable, {"method_type": "bogus", "path": "x"}, "auth_enable_exc"),
        (vault_sys_mounts_enable, {"backend_type": "bogus", "path": "x"}, "mount_enable_exc"),
    ],
)
async def test_enable_handler_reraises_other_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    params: dict[str, Any],
    exc_kwarg: str,
) -> None:
    """A non-idempotency 400 (unknown type) propagates for connector_error."""
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: hvac.exceptions.InvalidRequest("unknown backend type: bogus")},
    )
    with pytest.raises(hvac.exceptions.InvalidRequest):
        await handler(_make_operator(), None, params)


async def test_auth_tune_handler_forwards_supplied_knobs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth.tune forwards only the supplied knobs and reports tuned=True."""
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_auth_tune(
        _make_operator("op-jwt"),
        None,
        {"path": "userpass", "default_lease_ttl": "768h", "description": "tuned"},
    )

    assert result == {"path": "userpass", "tuned": True}
    # Only the two supplied knobs are forwarded — an omitted knob leaves
    # Vault's current value untouched (no max_lease_ttl in the call).
    assert fake.sys.auth_tune_calls == [
        {"path": "userpass", "default_lease_ttl": "768h", "description": "tuned"}
    ]
    assert fake.auth.token.revoke_calls == 1


async def test_mounts_tune_handler_forwards_supplied_knobs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mounts.tune forwards only the supplied knobs and reports tuned=True."""
    fake = install_fake_client(monkeypatch)
    result = await vault_sys_mounts_tune(
        _make_operator(), None, {"path": "secret", "max_lease_ttl": 3600}
    )

    assert result == {"path": "secret", "tuned": True}
    assert fake.sys.mount_tune_calls == [{"path": "secret", "max_lease_ttl": 3600}]


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vault.sys.auth.enable", {"method_type": "userpass", "path": "userpass"}),
        ("vault.sys.auth.tune", {"path": "userpass", "default_lease_ttl": "1h"}),
        ("vault.sys.mounts.enable", {"backend_type": "kv", "path": "kv-prod"}),
        ("vault.sys.mounts.tune", {"path": "secret", "description": "x"}),
    ],
)
async def test_bootstrap_ops_are_approval_gated_on_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_sys_ops: None,
) -> None:
    """DoD: a bootstrap dispatch parks as ``awaiting_approval`` (handler never runs)."""
    fake = install_fake_client(monkeypatch)
    result = await _dispatch_vault(op_id, params)

    assert result.status == "awaiting_approval", result.error
    assert result.extras.get("error_code") == "awaiting_approval"
    assert result.extras.get("approval_request_id")
    # The gate parks before any hvac call reaches Vault.
    assert fake.sys.auth_enable_calls == []
    assert fake.sys.auth_tune_calls == []
    assert fake.sys.mount_enable_calls == []
    assert fake.sys.mount_tune_calls == []


async def test_bootstrap_op_dispatches_with_approved_flag(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """With the approvals-API resume flag, the dispatch reaches the handler."""
    fake = install_fake_client(monkeypatch)
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.sys.auth.enable",
        target=None,
        params={"method_type": "userpass", "path": "userpass"},
        _approved=True,
    )

    assert result.status == "ok", result.error
    assert result.result == {"path": "userpass", "method_type": "userpass", "created": True}
    assert fake.sys.auth_enable_calls == [
        {"method_type": "userpass", "path": "userpass", "description": None}
    ]


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        # blank / slash-bearing paths
        ("vault.sys.auth.enable", {"method_type": "userpass", "path": "  "}),
        ("vault.sys.auth.enable", {"method_type": "userpass", "path": "a/b"}),
        # missing required field
        ("vault.sys.auth.enable", {"path": "userpass"}),
        ("vault.sys.mounts.enable", {"path": "secret"}),
        # blank type
        ("vault.sys.mounts.enable", {"backend_type": "  ", "path": "secret"}),
        # stray key
        ("vault.sys.auth.tune", {"path": "userpass", "bogus": "x"}),
        # tune with no path
        ("vault.sys.mounts.tune", {"default_lease_ttl": "1h"}),
        # bad enum on listing_visibility
        ("vault.sys.auth.tune", {"path": "userpass", "listing_visibility": "public"}),
    ],
)
async def test_bootstrap_op_rejects_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_sys_ops: None,
) -> None:
    """Schema rejects blank/slash paths, missing fields, stray keys, bad enums.

    Validation runs ahead of the approval gate, so a malformed call
    surfaces ``invalid_params`` rather than ``awaiting_approval``.
    """
    install_fake_client(monkeypatch)
    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"


@pytest.mark.parametrize(
    ("handler", "params", "exc_kwarg"),
    [
        (vault_sys_auth_tune, {"path": "userpass"}, "auth_tune_exc"),
        (vault_sys_mounts_tune, {"path": "secret"}, "mount_tune_exc"),
    ],
)
async def test_tune_handler_reraises_vault_error(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    params: dict[str, Any],
    exc_kwarg: str,
) -> None:
    """The tune handler propagates a Vault-side error for connector_error."""
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: hvac.exceptions.InvalidRequest("cannot fetch sysview for path")},
    )
    with pytest.raises(hvac.exceptions.InvalidRequest):
        await handler(_make_operator(), None, params)


async def test_bootstrap_op_login_failure_surfaces_vault_client_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_sys_ops: None,
) -> None:
    """Login-phase failure on an approved bootstrap dispatch → VaultClientError class."""
    install_fake_client(monkeypatch, login_exc=hvac.exceptions.Forbidden("role denied"))
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.sys.mounts.enable",
        target=None,
        params={"backend_type": "kv", "path": "kv-prod"},
        _approved=True,
    )

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "VaultRoleDeniedError"

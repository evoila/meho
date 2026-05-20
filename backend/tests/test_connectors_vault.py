# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for VaultConnector — G0.2-T5 reference implementation + G0.6 refactor.

Coverage matrix:

* Importing ``connectors.vault`` registers ``VaultConnector`` against
  the **v2** registry under ``(product="vault", version="1.x",
  impl_id="vault")`` (the G0.6-T-Refactor-Vault flip from v1).
* ``VaultConnector.fingerprint(target)`` returns a
  :class:`FingerprintResult` with ``vendor="hashicorp"`` and
  ``product="vault"``; the ``version`` field is populated from the
  ``/v1/sys/health`` payload.
* ``VaultConnector.probe(target)`` returns ``ok=True`` for a reachable
  unsealed Vault and ``ok=False`` for unreachable / sealed /
  uninitialised Vault; the ``reason`` field carries the same
  structured strings the existing ``vault_readiness_probe`` tests
  assert on.
* ``VaultConnector.execute(target, "vault.kv.read", {"path": "..."})``
  flows through the G0.6 dispatcher and returns
  ``OperationResult(status="ok")`` with ``result["data"]`` carrying
  the secret payload and ``result["version"]`` carrying the KV v2
  metadata version. The handler raises on read/login failure; the
  dispatcher wraps the exception into a structured
  ``connector_error`` :class:`OperationResult` with
  ``extras["exception_class"]`` naming the failure shape.
* ``VaultConnector.execute(target, "vault.nonexistent.op", ...)``
  returns the **dispatcher's** structured ``unknown_op`` error shape
  (``extras["error_code"]="unknown_op"``,
  ``extras["known_op_count"]``); the in-handler ``known_ops`` list
  shape from the pre-refactor connector is gone — that responsibility
  moved to the meta-tools (G0.6-T8 #399).
* ``VaultConnector.execute`` with a missing ``path`` param returns the
  dispatcher's ``invalid_params`` error from the
  ``parameter_schema`` validator (``minLength=1`` /
  ``pattern="\\S"``), not from the handler.
* Login failures (unreachable, role-denied) surface as
  ``connector_error`` with the :class:`VaultClientError` subclass name
  in ``extras["exception_class"]``.
* Read failures (login ok, secret read raises) surface as
  ``connector_error`` with the non-VaultClientError exception class in
  ``extras["exception_class"]`` so the health route's class-name match
  routes them to the read-phase failure path.
* Malformed hvac payload (missing metadata/version keys) is a
  structured read-phase error (``KeyError`` in
  ``extras["exception_class"]``), not an unhandled exception.

Test isolation: the production code builds hvac clients through the
private ``_build_client`` helper (single seam). Tests monkey-patch
that helper to return a controllable fake — no real HTTP, no Vault
container.

The ``_clean_vault_registry`` fixture re-registers ``VaultConnector``
via the v2 entry before each test because
``test_connectors_registry_v2.py`` (alphabetically earlier) has an
autouse ``clear_registry()`` that empties both registry layers after
its last test, **and** registers the typed op so the dispatcher can
look up the descriptor row at execute time.
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
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import all_connectors_v2
from meho_backplane.connectors.registry import (
    clear_registry,
    list_connector_impls,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_typed_operations,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# Shared fake for hvac's non-200 health response (standby / active-perf-standby).
# Defined once here to avoid duplicating the class body in every test that
# needs it.


class _StandbyResponse:
    status_code = 429


# ---------------------------------------------------------------------------
# Registry + dispatcher isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    """Re-register VaultConnector (v2) + reset the dispatcher caches.

    ``test_connectors_registry_v2.py`` and other earlier-alphabetised
    test files clear both registry layers after their tests via their
    own autouse fixtures. This fixture re-establishes the canonical
    v2 entry so the dispatcher's
    :func:`~meho_backplane.connectors.resolver.resolve_connector`
    finds :class:`VaultConnector` for vault targets. We also reset
    the dispatcher's handler-import and connector-instance caches so
    tests that re-register connector classes between functions don't
    inherit a stale instance.
    """
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


# ---------------------------------------------------------------------------
# Settings env
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Typed-op registration fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the Vault typed-op descriptor rows for tests that drive ``execute``.

    The autouse ``_default_database_url`` conftest fixture has already
    migrated the SQLite database to head, so the
    ``endpoint_descriptor`` and ``operation_group`` tables exist.
    """
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_target() -> None:
    """probe()/fingerprint() read Vault params from settings, not the
    target — G0.3 #224 dropped the per-connector target stub and G0.8-T3
    #629 typed these methods against ``Target | None``. Tests pass None.
    """
    return None


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """A request-scoped operator carrying the bearer token the vault
    handlers forward to Vault's JWT/OIDC auth (G0.8-T3 #629). Replaces
    the pre-#224 ``VaultTarget(raw_jwt=...)`` stub — the token is
    request-scoped operator context, never persisted target config.
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

    Mirrors how ``/api/v1/operations/call`` and the MCP
    ``call_operation`` meta-tool reach the vault handlers: a real
    :class:`Operator` is threaded by the dispatcher, the connector is
    resolved by ``connector_id``, ``target`` is ``None`` (vault
    connection params come from settings). The handler reads the JWT
    from ``operator.raw_jwt`` — exactly the contract #629 establishes.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# Registry acceptance criteria
# ---------------------------------------------------------------------------


def test_importing_vault_package_registers_vault_connector_v2() -> None:
    """Importing connectors.vault registers VaultConnector under the v2 natural key.

    The G0.6-T-Refactor-Vault flip moved the registration from the v1
    single-product surface to the v2 three-tuple key. The connector's
    class attributes (``product`` / ``version`` / ``impl_id``) match
    the registered key so the dispatcher's
    ``parse_connector_id("vault-1.x")`` lookup hits this row.
    """
    expected_key = ("vault", "1.x", "vault")
    assert expected_key in all_connectors_v2()
    assert all_connectors_v2()[expected_key] is VaultConnector
    assert expected_key in list_connector_impls()


def test_vault_connector_class_attributes_advertise_v2_key() -> None:
    """The connector class attributes match the v2 registry key.

    ``Connector._dispatcher_connector_id()`` reads these attributes
    to encode the dispatcher's ``connector_id`` string; the v2
    registry key must mirror them so the natural-key lookup hits
    the registered row.
    """
    assert VaultConnector.product == "vault"
    assert VaultConnector.version == "1.x"
    assert VaultConnector.impl_id == "vault"


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


async def test_fingerprint_returns_hashicorp_vault_with_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(
        monkeypatch,
        health_payload={
            "initialized": True,
            "sealed": False,
            "version": "1.18.0",
            "build_date": "2025-01-01",
            "cluster_id": "abc123",
            "cluster_name": "meho-vault",
        },
    )
    connector = VaultConnector()
    result = await connector.fingerprint(_make_target())

    assert result.vendor == "hashicorp"
    assert result.product == "vault"
    assert result.version == "1.18.0"
    assert result.build == "2025-01-01"
    assert result.reachable is True
    assert result.probe_method == "GET /v1/sys/health"
    assert result.extras["cluster_id"] == "abc123"
    assert result.extras["cluster_name"] == "meho-vault"
    assert fake.sys.read_calls == [{"method": "GET"}]


async def test_fingerprint_version_none_for_non_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict health payload (hvac standby response) → version=None."""
    install_fake_client(monkeypatch, health_payload=_StandbyResponse())
    result = await VaultConnector().fingerprint(_make_target())

    assert result.vendor == "hashicorp"
    assert result.version is None
    assert result.reachable is True


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


async def test_probe_returns_ok_for_unsealed_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(
        monkeypatch,
        health_payload={"initialized": True, "sealed": False, "standby": False},
    )
    result = await VaultConnector().probe(_make_target())

    assert result.ok is True
    assert result.reason == "sealed=False"


@pytest.mark.parametrize(
    "health_payload,expected_reason",
    [
        ({"initialized": True, "sealed": True}, "sealed"),
        ({"initialized": False, "sealed": False}, "uninitialized"),
    ],
    ids=["sealed", "uninitialized"],
)
async def test_probe_returns_ok_false_for_unhealthy_vault(
    monkeypatch: pytest.MonkeyPatch,
    health_payload: dict[str, Any],
    expected_reason: str,
) -> None:
    install_fake_client(monkeypatch, health_payload=health_payload)
    result = await VaultConnector().probe(_make_target())

    assert result.ok is False
    assert result.reason == expected_reason


async def test_probe_returns_ok_true_for_standby_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, health_payload=_StandbyResponse())
    result = await VaultConnector().probe(_make_target())

    assert result.ok is True
    assert result.reason == "http_429"


async def test_probe_returns_ok_false_when_vault_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.ConnectionError("dns failure"),
    )
    result = await VaultConnector().probe(_make_target())

    assert result.ok is False
    assert result.reason is not None
    assert result.reason.startswith("unreachable: ConnectionError")


# ---------------------------------------------------------------------------
# execute — delegates to the G0.6 dispatcher
# ---------------------------------------------------------------------------


async def test_execute_unknown_op_returns_dispatcher_unknown_op_shape(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """Unknown op_id surfaces the dispatcher's structured ``unknown_op``.

    Post-G0.6-T-Refactor-Vault, the in-connector ``known_ops`` listing
    moved to the meta-tools (G0.6-T8 #399); the dispatcher's
    ``unknown_op`` shape carries only the count, not the enumeration.
    """
    install_fake_client(monkeypatch)
    result = await _dispatch_vault("vault.nonexistent.op", {})

    assert result.status == "error"
    assert "vault.nonexistent.op" in (result.error or "")
    assert result.extras.get("error_code") == "unknown_op"
    assert "known_op_count" in result.extras
    # The known_op_count reflects the descriptors registered for the
    # (product="vault", version="1.x", impl_id="vault") triple -- the
    # full KV-v2 group (read, list, put, versions, delete) from the
    # G3.3-T1 typed-op upsert.
    assert result.extras["known_op_count"] >= 5


# ---------------------------------------------------------------------------
# execute — vault.kv.read happy path
# ---------------------------------------------------------------------------


async def test_execute_vault_kv_read_returns_secret_data(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(
        monkeypatch,
        secret={"username": "demo", "region": "eu-central-1"},
        kv_version=7,
    )
    result = await _dispatch_vault(
        "vault.kv.read",
        {"path": "secret/meho/test/federation"},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error
    # The handler returns ``{"data": <secret>, "version": <int|None>}``;
    # the dispatcher's PassThroughReducer lands it as result.result.
    assert isinstance(result.result, dict)
    assert result.result["data"] == {"username": "demo", "region": "eu-central-1"}
    assert result.result["version"] == 7
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"},
    ]
    # Mount defaults to "secret" when the caller omits it — the
    # path-only call site keeps working unchanged.
    assert fake.secrets.kv.v2.read_calls == [
        {"path": "secret/meho/test/federation", "mount_point": "secret"},
    ]
    assert fake.auth.token.revoke_calls == 1


async def test_execute_vault_kv_read_honors_explicit_mount(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """An explicit ``mount`` is forwarded as hvac's ``mount_point``.

    This is the load-bearing case for Initiative #366: the consumer
    wrappers address secrets as ``<mount> <path>`` so they can read
    arbitrary mounts (retiring ``scripts/_secret-read.sh``, which
    derived the mount from the path's first segment). Without mount
    forwarding on ``vault.kv.read`` the wrapper goal is unmet.
    """
    fake = install_fake_client(monkeypatch, secret={"k": "v"})
    result = await _dispatch_vault(
        "vault.kv.read",
        {"mount": "kv-prod", "path": "team/api"},
    )

    assert result.status == "ok", result.error
    assert fake.secrets.kv.v2.read_calls == [
        {"path": "team/api", "mount_point": "kv-prod"},
    ]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"path": ""},
        {"path": "   "},
        {"mount": "   ", "path": "p"},
        {"mount": "secret/data", "path": "p"},
    ],
    ids=[
        "missing",
        "empty-string",
        "whitespace-only",
        "mount-whitespace-only",
        "mount-with-slash",
    ],
)
async def test_execute_vault_kv_read_invalid_path_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Bad ``path`` or ``mount`` → dispatcher's ``invalid_params``.

    The pre-G0.6 handler ran the validation inline (``isinstance``
    + ``strip``); post-refactor, the dispatcher's
    :class:`Draft202012Validator` enforces ``minLength=1`` /
    ``pattern="\\S"`` for ``path`` from the registered
    parameter_schema. ``mount`` is the shared optional fragment whose
    ``pattern="^(?=.*\\S)[^/]+$"`` rejects an all-whitespace value
    (which would otherwise ``.strip()`` to an empty mount and degrade
    to a runtime ``connector_error``) and a slash-bearing value
    (``"secret/data"``) at validation time. The non-string ``path``
    case (``{"path": 123}``) is covered by the schema's
    ``"type": "string"`` constraint and lands in the same
    ``invalid_params`` shape.
    """
    install_fake_client(monkeypatch)
    result = await _dispatch_vault("vault.kv.read", params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"
    assert isinstance(result.extras.get("validation_errors"), list)
    assert result.extras["validation_errors"]


async def test_execute_vault_kv_read_non_string_path_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A non-string ``path`` hits the schema's ``"type": "string"`` constraint."""
    install_fake_client(monkeypatch)
    result = await _dispatch_vault("vault.kv.read", {"path": 123})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"


# ---------------------------------------------------------------------------
# execute — login failures (VaultClientError subclass)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "login_exc,expected_exc_class",
    [
        (requests.exceptions.ConnectionError("no route"), "VaultUnreachableError"),
        (hvac.exceptions.Forbidden("role denied"), "VaultRoleDeniedError"),
    ],
    ids=["unreachable", "role-denied"],
)
async def test_execute_login_failure_surfaces_vault_client_error_class(
    monkeypatch: pytest.MonkeyPatch,
    login_exc: Exception,
    expected_exc_class: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Login failure → dispatcher's ``connector_error`` with the VaultClientError class name.

    The pre-G0.6 handler caught :class:`VaultClientError` and surfaced
    ``extras["phase"]="login"`` + ``extras["exc_type"]``; post-refactor
    the handler raises and the dispatcher's ``connector_error`` branch
    records ``extras["exception_class"]``. Callers that need to render
    a login-vs-read distinction string-match the class name against
    the known VaultClientError subclass set.
    """
    install_fake_client(monkeypatch, login_exc=login_exc)
    result = await _dispatch_vault(
        "vault.kv.read",
        {"path": "some/path"},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == expected_exc_class


# ---------------------------------------------------------------------------
# execute — read failures (non-VaultClientError exception)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "read_exc,expected_exc_class",
    [
        (RuntimeError("secret missing"), "RuntimeError"),
        (KeyError("data"), "KeyError"),
    ],
    ids=["runtime-error", "malformed-payload"],
)
async def test_execute_read_failure_surfaces_non_vault_client_error_class(
    monkeypatch: pytest.MonkeyPatch,
    read_exc: Exception,
    expected_exc_class: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Read-phase exception → ``connector_error`` with the raised class name.

    The health route distinguishes "login phase" (VaultClientError
    subclass name) from "read phase" (anything else); this test pins
    the contract by exercising both common read-phase failure shapes.
    """
    install_fake_client(monkeypatch, read_exc=read_exc)
    result = await _dispatch_vault(
        "vault.kv.read",
        {"path": "some/path"},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == expected_exc_class


# ---------------------------------------------------------------------------
# G3.3-T1 — vault.kv.list / put / versions / delete
# ---------------------------------------------------------------------------


async def test_execute_vault_kv_list_returns_key_names(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch, keys=["api-key", "db/"])
    result = await _dispatch_vault(
        "vault.kv.list",
        {"path": "meho/test"},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["keys"] == ["api-key", "db/"]
    # Mount defaults to "secret" when the caller omits it.
    assert fake.secrets.kv.v2.list_calls == [
        {"path": "meho/test", "mount_point": "secret"},
    ]


async def test_execute_vault_kv_list_honors_explicit_mount(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch, keys=["x"])
    result = await _dispatch_vault(
        "vault.kv.list",
        {"mount": "kv-prod", "path": "team"},
    )

    assert result.status == "ok", result.error
    assert fake.secrets.kv.v2.list_calls == [
        {"path": "team", "mount_point": "kv-prod"},
    ]


async def test_execute_vault_kv_put_writes_new_version(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch, kv_version=4)
    result = await _dispatch_vault(
        "vault.kv.put",
        {"path": "meho/test", "data": {"token": "s3cr3t"}, "cas": 4},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["version"] == 5
    assert fake.secrets.kv.v2.put_calls == [
        {
            "path": "meho/test",
            "secret": {"token": "s3cr3t"},
            "cas": 4,
            "mount_point": "secret",
        },
    ]


async def test_execute_vault_kv_put_cas_omitted_passes_none(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """No ``cas`` ⇒ hvac called with ``cas=None`` (unconditional write)."""
    fake = install_fake_client(monkeypatch)
    result = await _dispatch_vault(
        "vault.kv.put",
        {"path": "meho/test", "data": {"k": "v"}},
    )

    assert result.status == "ok", result.error
    assert fake.secrets.kv.v2.put_calls[0]["cas"] is None


@pytest.mark.parametrize(
    "params",
    [{"path": "p"}, {"data": {"k": "v"}}, {"path": "p", "data": {}}],
    ids=["missing-data", "missing-path", "empty-data"],
)
async def test_execute_vault_kv_put_invalid_params_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Schema enforces ``required`` path+data and ``minProperties`` on data."""
    install_fake_client(monkeypatch)
    result = await _dispatch_vault("vault.kv.put", params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"


async def test_execute_vault_kv_versions_returns_metadata(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(
        monkeypatch,
        kv_version=3,
        versions_meta={
            "1": {"created_time": "2026-01-01T00:00:00Z", "destroyed": False},
            "3": {"created_time": "2026-03-01T00:00:00Z", "destroyed": False},
        },
    )
    result = await _dispatch_vault(
        "vault.kv.versions",
        {"path": "meho/test"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["current_version"] == 3
    assert set(result.result["versions"]) == {"1", "3"}
    assert fake.secrets.kv.v2.versions_calls == [
        {"path": "meho/test", "mount_point": "secret"},
    ]


async def test_execute_vault_kv_delete_soft_deletes_versions(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    result = await _dispatch_vault(
        "vault.kv.delete",
        {"path": "meho/test", "versions": [2, 3]},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["deleted_versions"] == [2, 3]
    assert fake.secrets.kv.v2.delete_calls == [
        {"path": "meho/test", "versions": [2, 3], "mount_point": "secret"},
    ]


@pytest.mark.parametrize(
    "params",
    [{"path": "p"}, {"path": "p", "versions": []}, {"versions": [1]}],
    ids=["missing-versions", "empty-versions", "missing-path"],
)
async def test_execute_vault_kv_delete_invalid_params_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Schema enforces ``required`` path+versions and ``minItems`` on versions."""
    install_fake_client(monkeypatch)
    result = await _dispatch_vault("vault.kv.delete", params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"


@pytest.mark.parametrize(
    "op_id,params,exc_kwarg",
    [
        ("vault.kv.list", {"path": "p"}, "list_exc"),
        ("vault.kv.put", {"path": "p", "data": {"k": "v"}}, "put_exc"),
        ("vault.kv.versions", {"path": "p"}, "versions_exc"),
        ("vault.kv.delete", {"path": "p", "versions": [1]}, "delete_exc"),
    ],
    ids=["list", "put", "versions", "delete"],
)
async def test_execute_kv_ops_vault_error_envelope_surfaces_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    exc_kwarg: str,
    _registered_vault_typed_ops: None,
) -> None:
    """A Vault-side raise on any KV-v2 verb → structured connector_error.

    Mirrors the ``vault.kv.read`` read-phase contract: the handler
    raises, the dispatcher wraps it with ``extras.exception_class``.
    """
    install_fake_client(
        monkeypatch,
        **{exc_kwarg: RuntimeError("permission denied")},
    )
    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "RuntimeError"


async def test_execute_vault_kv_list_malformed_payload_is_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A malformed hvac list payload → KeyError → structured connector_error."""
    fake = install_fake_client(monkeypatch)

    def _bad_list(path: str, mount_point: str = "secret", **_kw: Any) -> dict[str, Any]:
        return {"data": {}}  # missing "keys"

    monkeypatch.setattr(fake.secrets.kv.v2, "list_secrets", _bad_list)
    result = await _dispatch_vault("vault.kv.list", {"path": "p"})

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "KeyError"


@pytest.mark.parametrize(
    "op_id,expected_class",
    [
        ("vault.kv.read", "credential_read"),
        ("vault.kv.list", "credential_read"),
        ("vault.kv.versions", "read"),
        ("vault.kv.put", "write"),
        ("vault.kv.delete", "write"),
    ],
)
def test_kv_op_ids_classify_per_decision_3(op_id: str, expected_class: str) -> None:
    """The G6 broadcast classifier (op-id based, decision #3) tags the KV-v2 group.

    The shipped G0.6 substrate has no per-row ``op_class`` column on
    ``endpoint_descriptor``; decision #3 locks the sensitivity
    classifier on the op-id via ``_CREDENTIAL_READ_OPS``. This pins
    the register-time contract the DoD asks for: ``vault.kv.read`` and
    ``vault.kv.list`` are ``credential_read`` (aggregate-only
    broadcast); ``vault.kv.versions`` is a plain metadata ``read``;
    the mutating verbs are ``write``.
    """
    assert classify_op(op_id) == expected_class

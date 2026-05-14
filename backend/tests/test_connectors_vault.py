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

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.connectors import all_connectors_v2
from meho_backplane.connectors.registry import (
    clear_registry,
    list_connector_impls,
    register_connector_v2,
)
from meho_backplane.connectors.vault import (
    VaultConnector,
    VaultTarget,
    register_vault_typed_operations,
)
from meho_backplane.operations import reset_dispatcher_caches
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


def _make_target(jwt: str = "fake.jwt.value") -> VaultTarget:
    return VaultTarget(raw_jwt=jwt)


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
    connector = VaultConnector()
    result = await connector.execute(_make_target(), "vault.nonexistent.op", {})

    assert result.status == "error"
    assert "vault.nonexistent.op" in (result.error or "")
    assert result.extras.get("error_code") == "unknown_op"
    assert "known_op_count" in result.extras
    # The known_op_count reflects the descriptors registered for the
    # (product="vault", version="1.x", impl_id="vault") triple --
    # exactly one (vault.kv.read) from the typed-op upsert.
    assert result.extras["known_op_count"] >= 1


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
    connector = VaultConnector()
    result = await connector.execute(
        _make_target(jwt="op-jwt"),
        "vault.kv.read",
        {"path": "secret/meho/test/federation"},
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
    assert fake.secrets.kv.v2.read_calls == [{"path": "secret/meho/test/federation"}]
    assert fake.auth.token.revoke_calls == 1


@pytest.mark.parametrize(
    "params",
    [{}, {"path": ""}, {"path": "   "}],
    ids=["missing", "empty-string", "whitespace-only"],
)
async def test_execute_vault_kv_read_invalid_path_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Missing, empty, or whitespace-only ``path`` → dispatcher's ``invalid_params``.

    The pre-G0.6 handler ran the validation inline (``isinstance``
    + ``strip``); post-refactor, the dispatcher's
    :class:`Draft202012Validator` enforces ``minLength=1`` /
    ``pattern="\\S"`` from the registered parameter_schema.
    The non-string ``path`` case (``{"path": 123}``) is covered by
    the schema's ``"type": "string"`` constraint and lands in the
    same ``invalid_params`` shape.
    """
    install_fake_client(monkeypatch)
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", params)

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
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", {"path": 123})

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
    result = await VaultConnector().execute(
        _make_target(),
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
    result = await VaultConnector().execute(
        _make_target(),
        "vault.kv.read",
        {"path": "some/path"},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == expected_exc_class

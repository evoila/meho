# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A target's ``secret_ref`` with no secret in Vault gives a clear error.

* The shared credential loader turns hvac ``InvalidPath`` on the target's
  own ``secret_ref`` into :class:`TargetSecretNotFoundError`, which names
  the target and the ref and carries no Vault URL or hvac text.
* A ref taken from an op param keeps its own ``vault_credentials_read_error``
  path, with ``InvalidPath`` as the cause class.
* The dispatcher answers ``target_secret_not_found``: target, ref, whether
  MEHO built the ref from the target name, and how to fix it. The audit
  row gets the same error code.
* The error passes the connector handlers for ``CredentialsReadError``.
  The SSH connectors fold a failed credential read into
  ``reachable=False``, and their ``about`` ops then raise a generic
  connector error. ``linux.about`` with the real connector answers
  ``target_secret_not_found`` all the same.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import (
    SecretRefReadError,
    TargetSecretNotFoundError,
    load_basic_credentials,
    load_param_secret_data,
)
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vault.tenant_paths import is_derived_secret_ref
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors_target_secret import result_target_secret_not_found
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_TENANT: UUID = UUID("00000000-0000-0000-0000-000000000398")
_VAULT_URL = "https://vault.test/v1/secret/data"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


def _operator() -> Operator:
    return Operator(
        sub="op-missing-secret",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _invalid_path(ref: str) -> hvac.exceptions.InvalidPath:
    """The hvac error a KV-v2 read of a missing path raises."""
    return hvac.exceptions.InvalidPath(None, method="get", url=f"{_VAULT_URL}/{ref}")


class _Target:
    def __init__(
        self,
        *,
        name: str,
        secret_ref: str | None,
        product: str = "msf",
        version: str = "1",
    ) -> None:
        self.id: UUID = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = name
        self.host = "linux-01.example.test"
        self.port = 443
        self.product = product
        self.fingerprint: dict[str, Any] | None = None
        self.version = version
        self.preferred_impl_id: str | None = None
        self.auth_model = "shared_service_account"
        self.secret_ref = secret_ref
        self.secret_field_map: dict[str, str] = {}


# ---------------------------------------------------------------------------
# The shared loader
# ---------------------------------------------------------------------------


async def test_loader_names_target_and_ref_when_vault_has_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = f"tenants/{_TENANT}/linux-01"
    install_fake_client(monkeypatch, read_exc=_invalid_path(ref))

    with pytest.raises(TargetSecretNotFoundError) as exc:
        await load_basic_credentials(_Target(name="linux-01", secret_ref=ref), _operator())

    assert exc.value.target_name == "linux-01"
    assert exc.value.secret_ref == ref
    message = str(exc.value)
    assert "linux-01" in message
    assert ref in message
    assert "https://" not in message
    assert "on get" not in message


async def test_loader_keeps_other_vault_errors_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = f"tenants/{_TENANT}/linux-01"
    install_fake_client(
        monkeypatch, read_exc=hvac.exceptions.InternalServerError("boom", url=_VAULT_URL)
    )

    with pytest.raises(hvac.exceptions.InternalServerError):
        await load_basic_credentials(_Target(name="linux-01", secret_ref=ref), _operator())


async def test_param_ref_with_no_secret_stays_a_param_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = f"tenants/{_TENANT}/db-password"
    install_fake_client(monkeypatch, read_exc=_invalid_path(ref))

    with pytest.raises(SecretRefReadError) as exc:
        await load_param_secret_data(
            _Target(name="param:password_secret_ref", secret_ref=ref),
            _operator(),
            param="password_secret_ref",
        )

    assert not isinstance(exc.value, TargetSecretNotFoundError)
    assert exc.value.param == "password_secret_ref"
    assert exc.value.cause_class == "InvalidPath"
    assert ref not in str(exc.value)


# ---------------------------------------------------------------------------
# The result builder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("secret_ref", "name", "expected"),
    [
        (f"tenants/{_TENANT}/linux-01", "linux-01", True),
        (f"  tenants/{_TENANT}/linux-01  ", "linux-01", True),
        (f"tenants/{_TENANT}/linux", "linux-01", False),
        (f"tenants/{uuid.uuid4()}/linux-01", "linux-01", False),
        ("LINUX-01", "linux-01", False),
        (None, "linux-01", False),
        ("", "linux-01", False),
        (f"tenants/{_TENANT}/x", "  /  ", False),
    ],
    ids=[
        "derived",
        "derived-with-spaces",
        "other-leaf",
        "other-tenant",
        "other-case",
        "none",
        "empty",
        "no-leaf",
    ],
)
def test_is_derived_secret_ref(secret_ref: str | None, name: str, expected: bool) -> None:
    assert is_derived_secret_ref(secret_ref, tenant_id=_TENANT, target_name=name) is expected


def test_result_for_a_derived_ref_says_meho_built_it() -> None:
    ref = f"tenants/{_TENANT}/linux-01"
    exc = TargetSecretNotFoundError(target_name="linux-01", secret_ref=ref)

    out = result_target_secret_not_found("linux.about", exc, tenant_id=_TENANT, duration_ms=1.0)

    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("target_secret_not_found:")
    assert "default path MEHO builds from the target name" in out.error
    assert "meho targets update linux-01 --secret-ref <path>" in out.error
    assert "did not run" in out.error
    assert out.extras == {
        "error_code": "target_secret_not_found",
        "target": "linux-01",
        "secret_ref": ref,
        "secret_ref_derived": True,
        "remediation": out.extras["remediation"],
    }
    assert ref in out.extras["remediation"]


def test_result_for_a_set_ref_says_it_was_set() -> None:
    ref = f"tenants/{_TENANT}/shared-linux"
    exc = TargetSecretNotFoundError(target_name="linux-01", secret_ref=ref)

    out = result_target_secret_not_found("linux.about", exc, tenant_id=_TENANT, duration_ms=1.0)

    assert out.error is not None
    assert "set on the target" in out.error
    assert "default path" not in out.error
    assert out.extras["secret_ref_derived"] is False


# ---------------------------------------------------------------------------
# Dispatch end to end
# ---------------------------------------------------------------------------


class _CredentialReadingConnector(HttpConnector):
    """Reads the target's credential from Vault the way real connectors do."""

    product = "msf"
    accepts_ingested_operations = True
    version = "1"
    impl_id = "msf-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await load_basic_credentials(target, operator)
        return {}

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await load_basic_credentials(target, operator)
        return {}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def _seed_descriptor(session: AsyncSession, op_id: str) -> None:
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="msf",
            version="1",
            impl_id="msf-rest",
            op_id=op_id,
            source_kind="ingested",
            method="GET",
            path="/api/v1/about",
            handler_ref=None,
            summary="About.",
            description="Ingested read test op.",
            tags=[],
            parameter_schema={"type": "object", "properties": {}},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=[0.1] * 384,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


@pytest.mark.parametrize(
    ("secret_ref", "derived"),
    [(f"tenants/{_TENANT}/linux-01", True), (f"tenants/{_TENANT}/linux", False)],
    ids=["derived-ref", "set-ref"],
)
async def test_dispatch_answers_target_secret_not_found(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    secret_ref: str,
    derived: bool,
) -> None:
    op_id = "GET:/api/v1/about"
    install_fake_client(monkeypatch, read_exc=_invalid_path(secret_ref))
    register_connector_v2(
        product="msf", version="1", impl_id="msf-rest", cls=_CredentialReadingConnector
    )
    await _seed_descriptor(session, op_id)
    result = await dispatch(
        operator=_operator(),
        connector_id="msf-rest-1",
        op_id=op_id,
        target=_Target(name="linux-01", secret_ref=secret_ref),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("target_secret_not_found:")
    assert "'linux-01'" in result.error
    assert secret_ref in result.error
    assert "https://" not in result.error
    assert "on get" not in result.error
    assert result.extras["error_code"] == "target_secret_not_found"
    assert result.extras["target"] == "linux-01"
    assert result.extras["secret_ref"] == secret_ref
    assert result.extras["secret_ref_derived"] is derived

    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["error"]["error_code"] == "target_secret_not_found"


# ---------------------------------------------------------------------------
# The real Linux connector: about -> fingerprint -> credential read
# ---------------------------------------------------------------------------


async def test_linux_about_raises_the_missing_secret_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fingerprint`` folds a failed credential read into ``reachable=False``.

    The missing-secret error is not a credential read error, so it passes
    that handler and ``about`` raises it, not ``ConnectorUnreachableError``.
    """
    ref = f"tenants/{_TENANT}/linux-01"
    install_fake_client(monkeypatch, read_exc=_invalid_path(ref))
    target = _Target(name="linux-01", secret_ref=ref, product="linux", version="1.x")

    with pytest.raises(TargetSecretNotFoundError) as exc:
        await LinuxSshConnector().about(target, {}, _operator())  # type: ignore[arg-type]

    assert exc.value.target_name == "linux-01"
    assert exc.value.secret_ref == ref


async def test_linux_about_dispatch_answers_target_secret_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = f"tenants/{_TENANT}/linux-01"
    install_fake_client(monkeypatch, read_exc=_invalid_path(ref))
    register_connector_v2(
        product="linux", version="1.x", impl_id="linux-ssh", cls=LinuxSshConnector
    )
    await LinuxSshConnector.register_operations()
    target = _Target(name="linux-01", secret_ref=ref, product="linux", version="1.x")
    target.fingerprint = {"version": "1.x"}

    result = await dispatch(
        operator=_operator(),
        connector_id="linux-ssh-1.x",
        op_id="linux.about",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("target_secret_not_found:")
    assert "unreachable" not in result.error
    assert "https://" not in result.error
    assert result.extras["error_code"] == "target_secret_not_found"
    assert result.extras["target"] == "linux-01"
    assert result.extras["secret_ref"] == ref
    assert result.extras["secret_ref_derived"] is True

    async with get_sessionmaker()() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "linux.about")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["error"]["error_code"] == "target_secret_not_found"

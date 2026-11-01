# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The gcloud write operations through MEHO's normal path.

A registered target is a database row. It has no ``gcp_project`` column:
the project and the service account live in its ``extras``. These tests
seed such a row, register the gcloud operations and run two of the write
operations through ``preview_operation`` and ``call_operation``, the same
path the CLI and the MCP tools use:

* the call waits for approval; nothing is sent to Google;
* a second person approves, the operation runs, and Google receives the
  call for the project saved in ``extras``;
* the audit row of the run names the approver.

Google is never called: every answer comes from ``respx``. The target has
no ``secret_ref``, so the default credential loader answers ``{}`` without a
Vault read; the bearer token comes from a mocked ADC chain.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud import workload_identity as wif
from meho_backplane.connectors.gcloud.session import gcp_impersonate_sa, gcp_project
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "gcloud-rest-1.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000005438")
_TARGET_NAME = "test-gcloud"
_PROJECT = "test-project-123"
_SA = "meho-svc@test-project-123.iam.gserviceaccount.com"
_IAM = "https://iam.googleapis.com/v1"
_POOLS_URL = f"{_IAM}/projects/{_PROJECT}/locations/global/workloadIdentityPools"
_POOL_NAME = f"projects/{_PROJECT}/locations/global/workloadIdentityPools/cluster-pool"
_POOL_OP = f"{_POOL_NAME}/operations/op-1"
_PROJECT_TEST_URL = (
    f"https://cloudresourcemanager.googleapis.com/v1/projects/{_PROJECT}:testIamPermissions"
)
_SA_URL = f"{_IAM}/projects/{_PROJECT}/serviceAccounts/{_SA}"
_WIF_ROLE = "roles/iam.workloadIdentityUser"
_MEMBER = (
    "principalSet://iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/cluster-pool/*"
)
_POOL_CREATE = "gcloud.iam.workload_identity_pools.create"
_BINDING_ADD = "gcloud.iam.service_accounts.binding.add"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the poll sleep so the tests do not wait for Google's operation."""

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(wif, "_sleep", _fake_sleep)


def _fake_impersonated(**_kwargs: Any) -> MagicMock:
    creds = MagicMock()
    creds.token = None
    creds.valid = False

    def _refresh(_request: Any) -> None:
        creds.token = "test-bearer-token"
        creds.valid = True

    creds.refresh = _refresh
    return creds


def _adc_loader(scopes: list[str] | None = None) -> tuple[Any, str | None]:
    return MagicMock(), _PROJECT


@pytest.fixture
async def connector() -> AsyncIterator[GcloudConnector]:
    """A connector with the default credential loader and a mocked ADC chain, ready for dispatch."""
    conn = GcloudConnector(adc_loader=_adc_loader)
    _CONNECTOR_INSTANCE_CACHE[GcloudConnector] = conn  # type: ignore[assignment]
    with patch("google.auth.impersonated_credentials.Credentials", side_effect=_fake_impersonated):
        yield conn
    await conn.aclose()


def _make_operator(*, sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="gcloud call_operation test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _seed_target(extras: dict[str, Any]) -> UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="gcloud",
                version="1.0",
                host="cloudresourcemanager.googleapis.com",
                port=None,
                fqdn=None,
                secret_ref=None,
                auth_model=AuthModel.IMPERSONATION.value,
                vpn_required=False,
                extras=extras,
                fingerprint={"version": "1.0"},
                preferred_impl_id="gcloud-rest",
                notes="seeded by test_connectors_gcloud_call_operation",
            )
        )
        await s.commit()
    return target_id


async def _bootstrap(extras: dict[str, Any] | None = None) -> None:
    await GcloudConnector.register_gcloud_typed_operations()
    await _seed_target(
        {"gcp_project": _PROJECT, "gcp_impersonate_sa": _SA} if extras is None else extras
    )


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


async def _run_governed(args: dict[str, Any]) -> Any:
    """Preview, call (waits for approval), approve as a second person, run. Returns the run."""
    requester = _make_operator(sub="op-requester")
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


async def _audit_subjects(op_id: str) -> list[str]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row.operator_sub for row in rows]


# ---------------------------------------------------------------------------
# Through call_operation with a registered target
# ---------------------------------------------------------------------------


async def test_pool_create_runs_through_call_operation_with_approval(
    connector: GcloudConnector, no_wait: None
) -> None:
    """Pool create: waits for approval, runs for the project saved in extras, is audited."""
    await _bootstrap()
    with respx.mock() as mock:
        mock.post(_PROJECT_TEST_URL).respond(
            200, json={"permissions": ["iam.workloadIdentityPools.create"]}
        )
        create = mock.post(_POOLS_URL).respond(200, json={"name": _POOL_OP, "done": False})
        mock.get(f"{_IAM}/{_POOL_OP}").respond(200, json={"name": _POOL_OP, "done": True})
        mock.get(f"{_IAM}/{_POOL_NAME}").respond(
            200, json={"name": _POOL_NAME, "state": "ACTIVE", "displayName": "Cluster"}
        )
        requester = _make_operator(sub="op-requester")
        args = _args(_POOL_CREATE, {"pool_id": "cluster-pool", "display_name": "Cluster"})
        preview = await preview_operation(requester, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert create.call_count == 0

        request_id = UUID(call["extras"]["approval_request_id"])
        approver = _make_operator(sub="op-approver")
        async with get_sessionmaker()() as s:
            approved = await approve_request(s, request_id, operator=approver, params=None)
            await s.commit()
        assert approved.status == "approved"
        async with get_sessionmaker()() as s:
            row = await s.get(ApprovalRequest, request_id)
            assert row is not None
            run = await resume_dispatch_after_approval(operator=approver, request=row, params=None)

    assert run.status == "ok", run.error
    assert run.result["state"] == "ACTIVE"
    assert run.result["name"] == _POOL_NAME
    assert create.call_count == 1
    request = create.calls[0].request
    assert request.url.params["workloadIdentityPoolId"] == "cluster-pool"
    assert request.headers["Authorization"] == "Bearer test-bearer-token"
    assert await _audit_subjects(_POOL_CREATE) == ["op-approver"]


async def test_binding_add_runs_through_call_operation_with_approval(
    connector: GcloudConnector,
) -> None:
    """Binding add: the policy write goes to the service account of the project in extras."""
    await _bootstrap()
    policy = {"version": 3, "etag": "BwYread=", "bindings": []}
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:testIamPermissions").respond(
            200,
            json={
                "permissions": [
                    "iam.serviceAccounts.getIamPolicy",
                    "iam.serviceAccounts.setIamPolicy",
                ]
            },
        )
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=policy)
        write = mock.post(f"{_SA_URL}:setIamPolicy").respond(
            200,
            json={
                **policy,
                "etag": "BwYnew=",
                "bindings": [{"role": _WIF_ROLE, "members": [_MEMBER]}],
            },
        )
        run = await _run_governed(
            _args(
                _BINDING_ADD, {"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER}
            )
        )

    assert run.status == "ok", run.error
    assert run.result["added"] is True
    assert run.result["etag"] == "BwYnew="
    assert write.call_count == 1
    body = json.loads(write.calls[0].request.content)
    assert body["policy"]["etag"] == "BwYread="
    assert body["policy"]["bindings"] == [{"role": _WIF_ROLE, "members": [_MEMBER]}]
    assert await _audit_subjects(_BINDING_ADD) == ["op-approver"]


# ---------------------------------------------------------------------------
# The target settings
# ---------------------------------------------------------------------------


class _Registered:
    """The shape of a database target: no project attribute, settings in extras."""

    def __init__(self, extras: dict[str, Any] | None) -> None:
        self.name = _TARGET_NAME
        self.extras = extras


def test_settings_come_from_extras_on_a_registered_target() -> None:
    target = _Registered({"gcp_project": f" {_PROJECT} ", "gcp_impersonate_sa": _SA})
    assert gcp_project(target) == _PROJECT  # type: ignore[arg-type]
    assert gcp_impersonate_sa(target) == _SA  # type: ignore[arg-type]


@pytest.mark.parametrize("extras", [None, {}, {"gcp_project": ""}, {"gcp_project": 7}])
def test_missing_project_setting_is_a_clear_error(extras: dict[str, Any] | None) -> None:
    with pytest.raises(ValueError, match=r"Set extras\.gcp_project on the target"):
        gcp_project(_Registered(extras))  # type: ignore[arg-type]

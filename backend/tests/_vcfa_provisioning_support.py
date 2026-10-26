# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared fixtures-as-helpers for the VCFA provisioning-op tests (#3890).

Used by ``test_connectors_vcf_automation_provisioning.py`` and
``test_connectors_vcf_automation_api_token.py``. Seeds one VCFA target,
wires the connector singleton with a stub target-credential loader, and
dispatches through the real dispatcher with ``_approved=True`` (the
resume-path flag the approvals API sets once a human approved) so the
handler / audit / broadcast path runs. All names are synthetic.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VCFA_IMPL_ID,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target

TENANT_ID = UUID("00000000-0000-0000-0000-0000000038f0")
HOST = "10.20.30.7"
BASE_URL = f"https://{HOST}"
FQDN = "vcfa-prov.test.invalid"
TARGET_NAME = "vcfa-prov-target"

PROVIDER_JWT = "prov-jwt-sentinel"
TENANT_TOKEN = "tenant-bearer-sentinel"
USER_JWT = "user-session-jwt-sentinel"
#: The user password the stubbed Vault read returns.
PASSWORD = "Pw-sentinel-9f3c"
#: The refresh token the mocked OAuth grant mints.
MINTED_TOKEN = "RT-sentinel-7b1d2e44aa90c3f1"

ORG_ID = "urn:vcloud:org:11111111-2222-3333-4444-555555555555"
ORG_UUID = "11111111-2222-3333-4444-555555555555"
ORG = {"id": ORG_ID, "name": "example-org", "displayName": "Example", "isClassicTenant": True}

OPERATOR = Operator(
    sub="vcfa-prov-test",
    name="VCFA Provisioning Test",
    email=None,
    raw_jwt="<vcfa-prov-raw-jwt>",
    tenant_id=TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def stub_target_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """The target secret: the provider admin (tenant login uses the same pair)."""
    return {"username": "admin", "password": "target-admin-pw"}


async def seed_target() -> None:
    """Insert the provisioning-test VCFA target."""
    fingerprint = FingerprintResult(
        vendor="vmware",
        product="vcfa",
        version="9.0",
        reachable=True,
        probed_at=datetime(2026, 9, 1, tzinfo=UTC),
        probe_method="GET /api/versions + GET /iaas/api/about",
        extras={},
    ).model_dump(mode="json")
    async with get_sessionmaker()() as session:
        session.add(
            Target(
                tenant_id=TENANT_ID,
                name=TARGET_NAME,
                aliases=[],
                product=VcfAutomationConnector.product,
                host=HOST,
                port=443,
                fqdn=FQDN,
                secret_ref="vcfa/prov",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint=fingerprint,
                notes="seeded by tests/_vcfa_provisioning_support.py",
            )
        )
        await session.commit()


def wire_connector() -> VcfAutomationConnector:
    """Resolve the connector singleton and stub its target-credential loader."""
    cls = all_connectors_v2()[(VcfAutomationConnector.product, VCFA_VERSION, VCFA_IMPL_ID)]
    instance = get_or_create_connector_instance(cls)
    assert isinstance(instance, VcfAutomationConnector)
    instance._credentials_loader = stub_target_loader
    return instance


async def resolved_target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, TENANT_ID, TARGET_NAME)


async def run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    """Dispatch *op_id* (approval already granted unless ``approved=False``)."""
    result = await dispatch(
        operator=OPERATOR,
        connector_id=VCFA_CONNECTOR_ID,
        op_id=op_id,
        target=await resolved_target(),
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def mount_logins(mock: Any) -> None:
    """Provider + tenant logins of the connector's own (target) sessions."""
    mock.post("/cloudapi/1.0.0/sessions/provider").respond(
        200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": PROVIDER_JWT}
    )
    mock.post("/iaas/api/login").respond(200, json={"token": TENANT_TOKEN})


def page(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"values": values, "resultTotal": len(values), "page": 1, "pageCount": 1}


def by_filter(table: dict[str, list[dict[str, Any]]], default: list[dict[str, Any]] | None = None):
    """A respx side effect answering a cloudapi list by its ``filter`` query."""

    def _respond(request: httpx.Request) -> httpx.Response:
        key = request.url.params.get("filter", "")
        rows = table.get(key, default if default is not None else [])
        return httpx.Response(200, json=page(rows))

    return _respond


def basic_user(request: httpx.Request) -> str:
    """The user half of an HTTP Basic ``Authorization`` header."""
    raw = request.headers["Authorization"].removeprefix("Basic ")
    return base64.b64decode(raw).decode().split(":", 1)[0]

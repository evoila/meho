# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Helpers for the tests of the shared approval, policy, ABX and subscription ops.

The VCF Automation cases use the seeded target of
``tests/_vcfa_provisioning_support.py``. The vRA 8 cases use an in-memory
target that resolves to the vRA 8 connector. All names and ids are made up.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
)
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

VRA_HOST = "vra-governance.test.invalid"
VRA_BASE_URL = f"https://{VRA_HOST}"
VRA_BEARER = "vra-governance-bearer"

VRA_OPERATOR = Operator(
    sub="vra-governance-test",
    name="vRA Governance Test",
    email=None,
    raw_jwt="vra.governance.jwt",
    tenant_id=UUID("00000000-0000-0000-0000-0000000045a0"),
    tenant_role=TenantRole.OPERATOR,
)


class Vra8Target:
    """In-memory vRA 8 target; version 8.18 resolves to the vRA 8 connector."""

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = VRA_OPERATOR.tenant_id
        self.name = "vra-governance"
        self.host = VRA_HOST
        self.port = 443
        self.secret_ref = "targets/vra-governance"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


async def _vra_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<vra-user>", "password": "<vra-password>"}  # NOSONAR


def wire_vra8() -> Vra8Connector:
    """Resolve the vRA 8 connector singleton and stub its credential loader."""
    cls = all_connectors_v2()[(VRA8_PRODUCT, VRA8_VERSION, VRA8_IMPL_ID)]
    instance = get_or_create_connector_instance(cls)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _vra_loader
    return instance


def mount_vra_logins(mock: respx.MockRouter) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "rt"})
    mock.post("/iaas/api/login").respond(200, json={"token": VRA_BEARER})


async def run_vra8(op_id: str, params: dict[str, Any], *, approved: bool = True) -> Any:
    """Dispatch *op_id* on the vRA 8 connector (approval already granted by default)."""
    return await dispatch(
        operator=VRA_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=Vra8Target(),
        params=params,
        _approved=approved,
    )

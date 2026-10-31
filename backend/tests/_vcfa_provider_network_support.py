# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared made-up objects and helpers for the VCFA provider networking tests.

Used by ``test_connectors_vcf_automation_provider_network.py``,
``test_connectors_vcf_automation_provider_network_delete.py`` and
``test_connectors_vcf_automation_provider_org_settings.py``. The objects follow
the field names of the ``go-vcloud-director`` v3 types (``types/v56/tm.go``).
All names and ids are made up; the address range is a documentation range.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Target
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from tests._vcfa_provisioning_support import (
    OPERATOR,
    ORG,
    ORG_ID,
    TARGET_NAME,
    TENANT_ID,
    resolved_target,
)

REGION_ID = "urn:vcloud:region:00000000-0000-0000-0000-00000000a001"
IP_SPACE_ID = "urn:vcloud:ipSpace:00000000-0000-0000-0000-00000000a002"
GATEWAY_ID = "urn:vcloud:providerGateway:00000000-0000-0000-0000-00000000a003"
LINK_ID = "urn:vcloud:ipSpaceAssociation:00000000-0000-0000-0000-00000000a004"
SETTING_ID = "urn:vcloud:regionalNetworkingSetting:00000000-0000-0000-0000-00000000a005"
EDGE_ID = "urn:vcloud:edgeCluster:00000000-0000-0000-0000-00000000a006"
TIER0_ID = "00000000-0000-0000-0000-00000000a007"
CIDR = "192.0.2.0/24"
TASK_ID = "00000000-0000-0000-0000-00000000a0f1"
TASK_LOCATION = f"https://vcfa-prov.test.invalid/api/task/{TASK_ID}"

V90 = "/cloudapi/vcf"
V91 = "/cloudapi/v1"
ORG_PATH = f"/cloudapi/1.0.0/orgs/{quote(ORG_ID, safe='')}"
ORG_NETWORKING_PATH = f"{ORG_PATH}/networkingSettings"
TIER0_PATH = "/cloudapi/1.0.0/nsxTResources/importableTier0Routers"

__all__ = [
    "CIDR",
    "EDGE_ID",
    "GATEWAY_ID",
    "IP_SPACE_ID",
    "LINK_ID",
    "ORG",
    "ORG_ID",
    "ORG_NETWORKING_PATH",
    "ORG_PATH",
    "REGION_ID",
    "SETTING_ID",
    "TASK_ID",
    "TASK_LOCATION",
    "TIER0_ID",
    "TIER0_PATH",
    "V90",
    "V91",
    "card",
    "gateway",
    "ip_space",
    "item",
    "link",
    "org",
    "regional",
    "set_target_version",
    "tier0",
]


def item(prefix: str, collection: str, obj_id: str) -> str:
    """The path of one object, with its id percent-encoded."""
    return f"{prefix}/{collection}/{quote(obj_id, safe='')}"


def ip_space(*, status: str = "REALIZED", cidrs: tuple[str, ...] = (CIDR,)) -> dict[str, Any]:
    return {
        "id": IP_SPACE_ID,
        "name": "example-ip-space",
        "description": "made up",
        "regionRef": {"id": REGION_ID, "name": "example-region"},
        "defaultQuota": {"maxCidrCount": 5, "maxIpCount": 5, "maxSubnetSize": 24},
        "internalScopeCidrBlocks": [{"id": "blk-1", "name": "blk", "cidr": c} for c in cidrs],
        "status": status,
    }


def gateway(*, status: str = "REALIZED") -> dict[str, Any]:
    return {
        "id": GATEWAY_ID,
        "name": "example-gateway",
        "regionRef": {"id": REGION_ID, "name": "example-region"},
        "backingType": "NSX_TIER0",
        "backingRef": {"id": TIER0_ID, "name": "example-tier0"},
        "status": status,
    }


def link(ip_space_id: str = IP_SPACE_ID, *, link_id: str = LINK_ID) -> dict[str, Any]:
    return {
        "id": link_id,
        "name": "example-link",
        "ipSpaceRef": {"id": ip_space_id, "name": "example-ip-space"},
        "providerGatewayRef": {"id": GATEWAY_ID, "name": "example-gateway"},
        "status": "REALIZED",
    }


def regional(*, status: str = "REALIZED") -> dict[str, Any]:
    return {
        "id": SETTING_ID,
        "name": "example-org-example-region",
        "orgRef": {"id": ORG_ID, "name": "example-org"},
        "regionRef": {"id": REGION_ID, "name": "example-region"},
        "providerGatewayRef": {"id": GATEWAY_ID, "name": "example-gateway"},
        "serviceEdgeClusterRef": {"id": EDGE_ID, "name": "example-edge"},
        "status": status,
    }


def tier0(*, imported: bool = False, parent: str = "") -> dict[str, Any]:
    return {
        "id": TIER0_ID,
        "displayName": "example-tier0",
        "description": "",
        "parentTier0Id": parent,
        "alreadyImported": imported,
    }


def org(**overrides: Any) -> dict[str, Any]:
    return {
        **ORG,
        "description": "made up",
        "isEnabled": True,
        "canPublish": False,
        "userCount": 3,
        **overrides,
    }


async def set_target_version(version: str) -> None:
    """Record *version* as the probed product version of the test target."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Target)
            .where(Target.tenant_id == TENANT_ID, Target.name == TARGET_NAME)
            .values(fingerprint={"vendor": "vmware", "product": "vcfa", "version": version})
        )
        await session.commit()


async def card(op_id: str, params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    """The approval card (proposed effect) *op_id* would wait with."""
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    return await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id="vcfa-rest-9.0",
        )
    )

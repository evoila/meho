# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS VPC Handlers.

Handlers for VPC operations: VPCs and subnets.
Note: Security groups are in ec2_handlers.py (per decision D-10).
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import (
    serialize_subnet,
    serialize_vpc,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class VPCHandlerMixin:
    """Mixin providing VPC operation handlers."""

    # =========================================================================
    # VPC OPERATIONS
    # =========================================================================

    async def _handle_list_vpcs(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List VPCs.

        Args:
            params: Optional keys: region.

        Returns:
            List of serialized VPCs.
        """
        client = self._get_client("ec2", params.get("region"))

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_vpcs")
            results: list[dict[str, Any]] = []
            for page in paginator.paginate():
                results.extend(page.get("Vpcs", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_vpc(v) for v in raw]

    async def _handle_get_vpc(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific VPC.

        Args:
            params: Required keys: vpc_id. Optional keys: region.

        Returns:
            Serialized VPC.
        """
        client = self._get_client("ec2", params.get("region"))
        vpc_id = params["vpc_id"]

        def _get_vpc() -> dict[str, Any]:
            response = client.describe_vpcs(VpcIds=[vpc_id])
            vpcs = response.get("Vpcs", [])
            return vpcs[0] if vpcs else {}

        raw = await asyncio.to_thread(_get_vpc)
        return serialize_vpc(raw)

    # =========================================================================
    # SUBNET OPERATIONS
    # =========================================================================

    async def _handle_list_subnets(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List subnets, optionally filtered by VPC.

        Args:
            params: Optional keys: vpc_id, region.

        Returns:
            List of serialized subnets.
        """
        client = self._get_client("ec2", params.get("region"))

        filters: list[dict[str, Any]] = []
        vpc_id = params.get("vpc_id")
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_subnets")
            paginate_kwargs: dict[str, Any] = {}
            if filters:
                paginate_kwargs["Filters"] = filters

            results: list[dict[str, Any]] = []
            for page in paginator.paginate(**paginate_kwargs):
                results.extend(page.get("Subnets", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_subnet(s) for s in raw]

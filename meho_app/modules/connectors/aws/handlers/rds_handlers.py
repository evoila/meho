# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS RDS Handlers.

Handlers for RDS operations: instance listing and details.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import serialize_rds_instance

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class RDSHandlerMixin:
    """Mixin providing RDS operation handlers."""

    async def _handle_list_rds_instances(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List RDS database instances.

        Args:
            params: Optional keys: region.

        Returns:
            List of serialized RDS instances.
        """
        client = self._get_client("rds", params.get("region"))

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_db_instances")
            results: list[dict[str, Any]] = []
            for page in paginator.paginate():
                results.extend(page.get("DBInstances", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_rds_instance(i) for i in raw]

    async def _handle_get_rds_instance(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific RDS instance.

        Args:
            params: Required keys: db_instance_identifier. Optional keys: region.

        Returns:
            Serialized RDS instance.
        """
        client = self._get_client("rds", params.get("region"))
        db_instance_identifier = params["db_instance_identifier"]

        def _describe() -> dict[str, Any]:
            response = client.describe_db_instances(
                DBInstanceIdentifier=db_instance_identifier
            )
            instances = response.get("DBInstances", [])
            return instances[0] if instances else {}

        raw = await asyncio.to_thread(_describe)
        return serialize_rds_instance(raw)

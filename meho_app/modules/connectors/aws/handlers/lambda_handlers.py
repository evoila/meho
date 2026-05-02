# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Lambda Handlers.

Handlers for Lambda operations: function listing and details.
Note: This file is named lambda_handlers.py (NOT lambda.py) to avoid
Python keyword collision (Pitfall 5).
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import serialize_lambda_function

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class LambdaHandlerMixin:
    """Mixin providing Lambda operation handlers."""

    async def _handle_list_functions(  # type: ignore[misc]
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List Lambda functions.

        Args:
            params: Optional keys: region.

        Returns:
            List of serialized Lambda functions.
        """
        client = self._get_client("lambda", params.get("region"))

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_functions")
            results: list[dict[str, Any]] = []
            for page in paginator.paginate():
                results.extend(page.get("Functions", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_lambda_function(f) for f in raw]

    async def _handle_get_function(self: "AWSConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """
        Get details for a specific Lambda function.

        Args:
            params: Required keys: function_name. Optional keys: region.

        Returns:
            Serialized Lambda function (from Configuration key).
        """
        client = self._get_client("lambda", params.get("region"))
        function_name = params["function_name"]

        def _get_fn() -> dict[str, Any]:
            response = client.get_function(FunctionName=function_name)
            result: dict[str, Any] = response.get("Configuration", {})
            return result

        raw = await asyncio.to_thread(_get_fn)
        return serialize_lambda_function(raw)

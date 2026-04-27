# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS S3 Handlers.

Handlers for S3 operations: bucket listing with public access status.
Note: S3 list_buckets is a global operation (Pitfall 4) -- returns all
buckets regardless of region.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import serialize_s3_bucket

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class S3HandlerMixin:
    """Mixin providing S3 operation handlers."""

    async def _handle_list_buckets(  # type: ignore[misc]
        self: "AWSConnector", _params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List all S3 buckets with public access block status.

        S3 list_buckets is a global operation -- returns all buckets
        regardless of region (Pitfall 4). Public access checks are
        limited to 50 buckets to avoid rate limiting.

        Args:
            params: Optional keys: (none currently used).

        Returns:
            List of serialized S3 buckets with public access status.
        """
        client = self._get_client("s3")

        def _list_with_public_access() -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
            response = client.list_buckets()
            buckets = response.get("Buckets", [])

            results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            for idx, bucket in enumerate(buckets):
                public_access = None
                # Limit public access checks to avoid rate limiting
                if idx < 50:
                    try:
                        pa_response = client.get_public_access_block(Bucket=bucket.get("Name", ""))
                        public_access = pa_response.get("PublicAccessBlockConfiguration")
                    except Exception:
                        # Public access block may not exist for this bucket
                        pass

                results.append((bucket, public_access))
            return results

        raw = await asyncio.to_thread(_list_with_public_access)
        return [serialize_s3_bucket(bucket, pa) for bucket, pa in raw]

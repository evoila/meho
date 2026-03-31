# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS S3 Operation Definitions.

Operations for S3 bucket management.
"""

from meho_app.modules.connectors.base import OperationDefinition

S3_OPERATIONS = [
    OperationDefinition(
        operation_id="list_buckets",
        name="List S3 Buckets",
        description=(
            "List all S3 buckets (global operation -- returns all buckets "
            "regardless of region). Includes public access block status "
            "for each bucket (limited to first 50 buckets to avoid rate limiting)."
        ),
        category="storage",
        parameters=[],
        example="list_buckets",
    ),
]

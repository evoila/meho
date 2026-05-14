# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.7 spec-ingestion pipeline subpackage.

This subpackage hosts the operator-facing review-queue state machine
(:class:`ReviewService`) plus the parsers, bulk-upsert helper, and
LLM-grouping pass that sibling tasks will land beside it. The
public surface is re-exported here so consumers (CLI verbs at T5,
REST routes at T6, admin MCP tools at T7) import from
``meho_backplane.operations.ingest`` rather than reaching into the
private module layout.
"""

from meho_backplane.operations.ingest.exceptions import (
    ConnectorNotFoundError,
    InvalidStateTransitionError,
)
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "ConnectorNotFoundError",
    "ConnectorReviewGroup",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "InvalidStateTransitionError",
    "ReviewService",
    "parse_connector_id",
]

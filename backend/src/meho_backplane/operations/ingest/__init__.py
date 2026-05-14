# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.7 spec-ingestion pipeline subpackage.

This subpackage hosts the OpenAPI parser (:func:`parse_openapi`,
T1 #401), the operator-facing review-queue state machine
(:class:`ReviewService`, T4 #402), plus the bulk-upsert helper and
LLM-grouping pass that sibling tasks will land beside them. The
public surface is re-exported here so consumers (CLI verbs at T5,
REST routes at T6, admin MCP tools at T7) import from
``meho_backplane.operations.ingest`` rather than reaching into the
private module layout.
"""

from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    ensure_connector_class_registered,
)
from meho_backplane.operations.ingest.exceptions import (
    ConnectorNotFoundError,
    InvalidSchemaError,
    InvalidSpecError,
    InvalidStateTransitionError,
    OpIdCollision,
    UnsupportedSpecError,
)
from meho_backplane.operations.ingest.openapi import (
    detect_spec_format,
    parse_openapi,
)
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)
from meho_backplane.operations.ingest.register_ingested import (
    IngestionResult,
    register_ingested_operations,
)
from meho_backplane.operations.ingest.schemas import (
    EndpointDescriptorProto,
    SafetyLevel,
)
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "ConnectorNotFoundError",
    "ConnectorReviewGroup",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "EndpointDescriptorProto",
    "GenericRestConnector",
    "IngestionResult",
    "InvalidSchemaError",
    "InvalidSpecError",
    "InvalidStateTransitionError",
    "OpIdCollision",
    "ReviewService",
    "SafetyLevel",
    "UnsupportedSpecError",
    "detect_spec_format",
    "ensure_connector_class_registered",
    "parse_connector_id",
    "parse_openapi",
    "register_ingested_operations",
]

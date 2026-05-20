# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.7 spec-ingestion pipeline subpackage.

This subpackage hosts the OpenAPI parser (:func:`parse_openapi`,
T1 #401), the operator-facing review-queue state machine
(:class:`ReviewService`, T4 #402), the bulk-upsert helper (T2 #403),
the LLM-grouping pass (T3 #404), the end-to-end ingestion pipeline
service (:class:`IngestionPipelineService`), the shared REST / MCP
request / response models (:mod:`api_schemas`), and the list-helper
the REST router exposes at ``GET /api/v1/connectors``. The public
surface is re-exported here so consumers (CLI verbs at T5, REST
routes at T6, admin MCP tools at T7) import from
``meho_backplane.operations.ingest`` rather than reaching into the
private module layout.
"""

from meho_backplane.operations.ingest.api_schemas import (
    ConnectorListItem,
    ConnectorListResponse,
    ConnectorStatusFilter,
    EditGroupBody,
    EditOpBody,
    GroupingResultModel,
    IngestionResultModel,
    IngestRequest,
    IngestResponse,
    SpecSource,
)
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    check_version_covered_by_registered_class,
    ensure_connector_class_registered,
)
from meho_backplane.operations.ingest.exceptions import (
    ConnectorNotFoundError,
    InvalidSchemaError,
    InvalidSpecError,
    InvalidStateTransitionError,
    LlmOutputInvalid,
    OpIdCollision,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    VersionMismatchError,
)
from meho_backplane.operations.ingest.list_connectors import (
    list_ingested_connectors,
)
from meho_backplane.operations.ingest.llm_groups import (
    DEFAULT_GROUPING_BATCH_SIZE,
    GroupingResult,
    GroupProposal,
    LlmClient,
    run_llm_grouping,
)
from meho_backplane.operations.ingest.openapi import (
    detect_spec_format,
    parse_openapi,
    read_spec_info_version,
)
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)
from meho_backplane.operations.ingest.pipeline import (
    IngestionPipelineResult,
    IngestionPipelineService,
    LlmClientFactory,
    LlmClientUnavailable,
    default_llm_client_factory,
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
    "DEFAULT_GROUPING_BATCH_SIZE",
    "ConnectorListItem",
    "ConnectorListResponse",
    "ConnectorNotFoundError",
    "ConnectorReviewGroup",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "ConnectorStatusFilter",
    "EditGroupBody",
    "EditOpBody",
    "EndpointDescriptorProto",
    "GenericRestConnector",
    "GroupProposal",
    "GroupingResult",
    "GroupingResultModel",
    "IngestRequest",
    "IngestResponse",
    "IngestionPipelineResult",
    "IngestionPipelineService",
    "IngestionResult",
    "IngestionResultModel",
    "InvalidSchemaError",
    "InvalidSpecError",
    "InvalidStateTransitionError",
    "LlmClient",
    "LlmClientFactory",
    "LlmClientUnavailable",
    "LlmOutputInvalid",
    "OpIdCollision",
    "ReviewService",
    "SafetyLevel",
    "SpecSource",
    "UncoveredVersionLabel",
    "UnsupportedSpecError",
    "VersionMismatchError",
    "check_version_covered_by_registered_class",
    "default_llm_client_factory",
    "detect_spec_format",
    "ensure_connector_class_registered",
    "list_ingested_connectors",
    "parse_connector_id",
    "parse_openapi",
    "read_spec_info_version",
    "register_ingested_operations",
    "run_llm_grouping",
]

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

from meho_backplane.operations.ingest.anthropic_client import (
    AnthropicMessagesLlmClient,
    build_anthropic_ingest_llm_client,
)
from meho_backplane.operations.ingest.api_schemas import (
    ConnectorListItem,
    ConnectorListResponse,
    ConnectorStatusFilter,
    EditGroupBody,
    EditOpBody,
    EditOpResponse,
    EditOpWarning,
    EnableReadsResponse,
    GroupingResultModel,
    IngestionResultModel,
    IngestJobHandle,
    IngestJobStatusResponse,
    IngestRequest,
    IngestResponse,
    SpecSource,
)
from meho_backplane.operations.ingest.catalog import (
    CatalogError,
    CatalogListResponse,
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_catalog,
    load_profile_resource,
    load_spec_resource,
    parse_catalog,
    validate_catalog_registry_coverage,
    validate_shipped_artifacts,
)
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    check_version_covered_by_registered_class,
    ensure_connector_class_registered,
)
from meho_backplane.operations.ingest.delete_connector import (
    DeleteConnectorResult,
    DeleteConnectorWarning,
)
from meho_backplane.operations.ingest.error_envelopes import (
    build_catalog_entry_malformed_detail,
    build_catalog_entry_not_found_detail,
    build_catalog_entry_typed_connector_detail,
    build_catalog_entry_upstream_not_spec_detail,
    build_connector_scope_ambiguous_detail,
    build_invalid_schema_detail,
    build_invalid_spec_detail,
    build_llm_output_invalid_detail,
    build_op_id_collision_detail,
    build_product_impl_id_mismatch_detail,
    build_uncovered_version_label_detail,
    build_unsupported_spec_detail,
    build_upstream_not_spec_detail,
    build_version_mismatch_detail,
)
from meho_backplane.operations.ingest.exceptions import (
    AmbiguousConnectorScopeError,
    ConnectorNotFoundError,
    ConnectorScopeCandidate,
    InvalidSchemaError,
    InvalidSpecError,
    InvalidStateTransitionError,
    LlmOutputInvalid,
    OpIdCollision,
    ProductImplIdMismatch,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    UpstreamNotSpecError,
    VersionMismatchError,
)
from meho_backplane.operations.ingest.jobs import (
    IngestJob,
    IngestJobNotFoundError,
    IngestJobRegistry,
    IngestJobStatus,
    get_job_registry,
    reset_job_registry_for_tests,
    run_ingest_job,
)
from meho_backplane.operations.ingest.list_connectors import (
    list_ingested_connectors,
)
from meho_backplane.operations.ingest.llm_groups import (
    DEFAULT_GROUPING_BATCH_SIZE,
    GroupingResult,
    GroupProposal,
    LlmClient,
    LlmJsonResult,
    StructuredJsonLlmClient,
    extract_json_object,
    run_llm_grouping,
    strip_code_fences,
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
    "AmbiguousConnectorScopeError",
    "AnthropicMessagesLlmClient",
    "CatalogError",
    "CatalogListResponse",
    "ConnectorListItem",
    "ConnectorListResponse",
    "ConnectorNotFoundError",
    "ConnectorReviewGroup",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "ConnectorScopeCandidate",
    "ConnectorSpecCatalog",
    "ConnectorSpecEntry",
    "ConnectorStatusFilter",
    "DeleteConnectorResult",
    "DeleteConnectorWarning",
    "EditGroupBody",
    "EditOpBody",
    "EditOpResponse",
    "EditOpWarning",
    "EnableReadsResponse",
    "EndpointDescriptorProto",
    "GenericRestConnector",
    "GroupProposal",
    "GroupingResult",
    "GroupingResultModel",
    "IngestJob",
    "IngestJobHandle",
    "IngestJobNotFoundError",
    "IngestJobRegistry",
    "IngestJobStatus",
    "IngestJobStatusResponse",
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
    "LlmJsonResult",
    "LlmOutputInvalid",
    "OpIdCollision",
    "ProductImplIdMismatch",
    "ReviewService",
    "SafetyLevel",
    "SpecSource",
    "StructuredJsonLlmClient",
    "UncoveredVersionLabel",
    "UnsupportedSpecError",
    "UpstreamNotSpecError",
    "VersionMismatchError",
    "build_anthropic_ingest_llm_client",
    "build_catalog_entry_malformed_detail",
    "build_catalog_entry_not_found_detail",
    "build_catalog_entry_typed_connector_detail",
    "build_catalog_entry_upstream_not_spec_detail",
    "build_connector_scope_ambiguous_detail",
    "build_invalid_schema_detail",
    "build_invalid_spec_detail",
    "build_llm_output_invalid_detail",
    "build_op_id_collision_detail",
    "build_product_impl_id_mismatch_detail",
    "build_uncovered_version_label_detail",
    "build_unsupported_spec_detail",
    "build_upstream_not_spec_detail",
    "build_version_mismatch_detail",
    "check_version_covered_by_registered_class",
    "default_llm_client_factory",
    "detect_spec_format",
    "ensure_connector_class_registered",
    "extract_json_object",
    "get_job_registry",
    "list_ingested_connectors",
    "load_catalog",
    "load_profile_resource",
    "load_spec_resource",
    "parse_catalog",
    "parse_connector_id",
    "parse_openapi",
    "read_spec_info_version",
    "register_ingested_operations",
    "reset_job_registry_for_tests",
    "run_ingest_job",
    "run_llm_grouping",
    "strip_code_fences",
    "validate_catalog_registry_coverage",
    "validate_shipped_artifacts",
]

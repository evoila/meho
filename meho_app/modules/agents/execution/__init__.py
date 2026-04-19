# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent execution module.

Contains components for API response processing, caching, and schema handling.
"""

from meho_app.modules.agents.execution.analysis import (
    ResponseAnalysis,
    analyze_response,
)
from meho_app.modules.agents.execution.cache import (
    CachedResponse,
    CachedTable,
    SchemaSummary,
)
from meho_app.modules.agents.execution.json_extraction import (
    extract_preferred_keywords,
    extract_verbatim_snippet,
    find_json_snippets,
    requires_verbatim_example,
    score_snippet,
)
from meho_app.modules.agents.execution.schema_helpers import (
    format_optional_params,
    format_required_params,
    generate_usage_example,
    summarize_request_body_schema,
    summarize_response_schema,
)
from meho_app.modules.agents.execution.search_utils import (
    boost_code_containing_chunks,
    build_metadata_filters,
    detect_metadata_filters,
    estimate_size,
    format_result,
    is_example_request,
)

__all__ = [
    "CachedResponse",
    # Caching
    "CachedTable",
    # Analysis
    "ResponseAnalysis",
    "SchemaSummary",
    "analyze_response",
    "boost_code_containing_chunks",
    "build_metadata_filters",
    # Search utilities
    "detect_metadata_filters",
    "estimate_size",
    "extract_preferred_keywords",
    "extract_verbatim_snippet",
    "find_json_snippets",
    "format_optional_params",
    # Schema helpers
    "format_required_params",
    "format_result",
    "generate_usage_example",
    "is_example_request",
    # JSON extraction
    "requires_verbatim_example",
    "score_snippet",
    "summarize_request_body_schema",
    "summarize_response_schema",
]

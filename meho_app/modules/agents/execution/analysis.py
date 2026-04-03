# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Response analysis for API responses.

Analyzes API responses to determine if data reduction is needed.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


@dataclass
class ResponseAnalysis:
    """
    Analysis of an API response to determine processing strategy.

    Contains metrics about the response size and structure,
    along with a recommendation on whether reduction is needed.
    """

    # Size metrics
    total_records: int = 0
    estimated_size_bytes: int = 0

    # Structure info
    source_path: str = ""
    detected_fields: list[str] = field(default_factory=list)

    # Processing recommendation
    needs_reduction: bool = False
    reason: str = ""

    @property
    def size_kb(self) -> float:
        """Get the estimated size in kilobytes."""
        return self.estimated_size_bytes / 1024

    @property
    def is_large(self) -> bool:
        """Whether response is considered large (>100KB or >100 records)."""
        return self.estimated_size_bytes > 100 * 1024 or self.total_records > 100


def analyze_response(data: Any) -> ResponseAnalysis:
    """
    Analyze an API response to determine processing needs.

    Examines the response structure to:
    1. Estimate the total size
    2. Count the number of records
    3. Detect the data path (for nested responses)
    4. Recommend if reduction is needed

    Args:
        data: The raw API response

    Returns:
        ResponseAnalysis with metrics and recommendations
    """
    analysis = ResponseAnalysis()

    # Estimate size
    try:
        json_str = json.dumps(data)
        analysis.estimated_size_bytes = len(json_str.encode("utf-8"))
    except (TypeError, ValueError):
        analysis.estimated_size_bytes = 0

    # Find the data source path and count records
    if isinstance(data, list):
        analysis.source_path = ""
        analysis.total_records = len(data)
        if data and isinstance(data[0], dict):
            analysis.detected_fields = list(data[0].keys())
    elif isinstance(data, dict):
        # Dynamic shape detection: find the first key whose value is a list-of-dicts.
        # This replaces the old hardcoded key list ["items", "data", "results", ...].
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                analysis.source_path = key
                analysis.total_records = len(value)
                analysis.detected_fields = list(value[0].keys())
                break

    # Determine if reduction is needed
    if analysis.total_records > 50:
        analysis.needs_reduction = True
        analysis.reason = f"Large dataset ({analysis.total_records} records)"
    elif analysis.estimated_size_bytes > 50 * 1024:
        analysis.needs_reduction = True
        analysis.reason = f"Large response ({analysis.size_kb:.1f}KB)"

    return analysis

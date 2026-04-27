# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Quality scoring for connector operation metadata.

Computes a 1-5 star rating based on how well-documented the connector's
operations are. Higher quality metadata leads to better generated skills.

Scoring criteria (weighted):
- Description coverage (40%): % of operations with descriptions > 10 chars
- Parameter documentation (25%): % of operations with documented parameters
- Response schema presence (20%): % of operations with response schemas
- Category/tag coverage (15%): % of operations with categories or tags
"""

from __future__ import annotations

from pydantic import BaseModel


class OperationData(BaseModel):
    """Unified operation representation for skill generation.

    Normalizes data from both REST endpoints (EndpointDescriptor) and typed
    connector operations (ConnectorOperationDescriptor) into a common format.
    """

    operation_id: str
    name: str
    description: str | None = None
    category: str | None = None
    parameters: list[dict] | None = None
    response_schema: dict | None = None
    tags: list[str] | None = None
    summary: str | None = None


def _has_documented_params(op: OperationData) -> bool:
    """Check if any parameter in the operation has a description field.

    Returns True if at least one parameter dict contains a non-empty
    'description' key.
    """
    if not op.parameters:
        return False
    return any(isinstance(p, dict) and p.get("description") for p in op.parameters)


def compute_quality_score(operations: list[OperationData]) -> int:
    """Compute 1-5 star quality score based on operation metadata completeness.

    The score reflects how well-documented the connector's operations are,
    which directly impacts the quality of the generated skill.

    Args:
        operations: List of normalized operation data.

    Returns:
        Integer 1-5 star rating. Returns 1 for empty operations list.
    """
    if not operations:
        return 1

    total = len(operations)

    # Description coverage: operations with meaningful descriptions (> 10 chars)
    desc_count = sum(1 for op in operations if op.description and len(op.description) > 10)

    # Parameter documentation: operations where at least one param has a description
    param_doc_count = sum(1 for op in operations if _has_documented_params(op))

    # Response schema presence: operations with non-empty response schemas
    response_count = sum(1 for op in operations if op.response_schema)

    # Category/tag coverage: operations with category or tags
    category_count = sum(1 for op in operations if op.category or op.tags)

    # Weighted average
    desc_pct = desc_count / total
    param_pct = param_doc_count / total
    response_pct = response_count / total
    category_pct = category_count / total

    weighted = (
        (desc_pct * 0.40) + (param_pct * 0.25) + (response_pct * 0.20) + (category_pct * 0.15)
    )

    # Map to 1-5 stars
    if weighted >= 0.8:
        return 5
    elif weighted >= 0.6:
        return 4
    elif weighted >= 0.4:
        return 3
    elif weighted >= 0.2:
        return 2
    else:
        return 1

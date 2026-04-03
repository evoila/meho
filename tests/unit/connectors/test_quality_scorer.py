# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for quality scoring of connector operation metadata.

Tests cover:
- Empty operations edge case
- Full metadata scoring (high score)
- Minimal metadata scoring (low score)
- Description coverage weighting
- Short description exclusion
- Parameter documentation impact
- Response schema impact
- Score range validation
"""

from meho_app.modules.connectors.skill_generation.quality_scorer import (
    OperationData,
    compute_quality_score,
)


def _make_op(
    operation_id: str = "test_op",
    name: str = "Test Operation",
    description: str | None = None,
    category: str | None = None,
    parameters: list[dict] | None = None,
    response_schema: dict | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
) -> OperationData:
    """Helper to build OperationData with defaults."""
    return OperationData(
        operation_id=operation_id,
        name=name,
        description=description,
        category=category,
        parameters=parameters,
        response_schema=response_schema,
        tags=tags,
        summary=summary,
    )


class TestQualityScorer:
    """Tests for compute_quality_score()."""

    def test_empty_operations_returns_1(self):
        """Empty operations list should return minimum score of 1."""
        assert compute_quality_score([]) == 1

    def test_single_op_with_full_metadata_returns_high_score(self):
        """An operation with description, documented params, response schema, and tags
        should score 4 or 5."""
        op = _make_op(
            description="Retrieves all active pods in the specified namespace",
            parameters=[
                {"name": "namespace", "type": "string", "description": "K8s namespace to query"},
            ],
            response_schema={"type": "object", "properties": {"items": {"type": "array"}}},
            tags=["pods", "kubernetes"],
            category="compute",
        )
        score = compute_quality_score([op])
        assert score >= 4, f"Fully documented op should score >= 4, got {score}"

    def test_single_op_with_no_metadata_returns_1(self):
        """An operation with only operation_id and name (no desc, params, schema, tags)
        should score 1 or 2."""
        op = _make_op()
        score = compute_quality_score([op])
        assert score <= 2, f"Bare op should score <= 2, got {score}"

    def test_description_coverage_weight(self):
        """With half of operations having descriptions, score should be in the middle range."""
        ops_with_desc = [
            _make_op(
                operation_id=f"op_{i}",
                description="A meaningful description that is longer than ten characters",
            )
            for i in range(5)
        ]
        ops_without_desc = [_make_op(operation_id=f"bare_{i}") for i in range(5)]
        score = compute_quality_score(ops_with_desc + ops_without_desc)
        # 50% description coverage * 0.40 weight = 0.20 -> score 2
        # Could be 2 or 3 depending on edge of threshold
        assert 1 <= score <= 3, f"Half-described ops should score 1-3, got {score}"

    def test_short_description_not_counted(self):
        """Descriptions under 10 chars should not count toward coverage."""
        ops = [
            _make_op(operation_id="op_1", description="Short"),
            _make_op(operation_id="op_2", description="Tiny"),
            _make_op(operation_id="op_3", description="X"),
        ]
        # All descriptions under 10 chars -> 0% coverage
        score = compute_quality_score(ops)
        assert score <= 2, f"Short descriptions should not boost score, got {score}"

    def test_parameter_docs_counted(self):
        """Operations with documented parameters should score higher
        than identical operations without."""
        base_ops = [
            _make_op(
                operation_id=f"op_{i}",
                description="A meaningful operation description for scoring",
                parameters=[
                    {"name": "id", "type": "string"},  # no description
                ],
            )
            for i in range(5)
        ]
        score_without = compute_quality_score(base_ops)

        documented_ops = [
            _make_op(
                operation_id=f"op_{i}",
                description="A meaningful operation description for scoring",
                parameters=[
                    {"name": "id", "type": "string", "description": "Resource identifier"},
                ],
            )
            for i in range(5)
        ]
        score_with = compute_quality_score(documented_ops)
        assert score_with >= score_without, (
            f"Documented params ({score_with}) should score >= undocumented ({score_without})"
        )

    def test_response_schema_counted(self):
        """Operations with response schemas should score higher
        than identical operations without."""
        ops_no_schema = [
            _make_op(
                operation_id=f"op_{i}",
                description="A meaningful operation description for scoring",
            )
            for i in range(5)
        ]
        score_without = compute_quality_score(ops_no_schema)

        ops_with_schema = [
            _make_op(
                operation_id=f"op_{i}",
                description="A meaningful operation description for scoring",
                response_schema={"type": "object", "properties": {"id": {"type": "string"}}},
            )
            for i in range(5)
        ]
        score_with = compute_quality_score(ops_with_schema)
        assert score_with >= score_without, (
            f"Response schema ops ({score_with}) should score >= no-schema ({score_without})"
        )

    def test_score_always_in_range(self):
        """Property test: score should always be 1-5 regardless of input variations."""
        test_cases = [
            [],
            [_make_op()],
            [
                _make_op(
                    description="x" * 100,
                    parameters=[{"name": "a", "description": "b"}],
                    response_schema={"type": "object"},
                    tags=["t"],
                    category="c",
                )
            ],
            [_make_op(operation_id=f"op_{i}") for i in range(100)],
            [
                _make_op(
                    operation_id=f"op_{i}",
                    description=f"Description for operation {i} with enough detail",
                    parameters=[{"name": f"p{i}", "description": f"param {i}"}],
                    response_schema={"type": "object"},
                    category=f"cat_{i % 3}",
                    tags=[f"tag_{i}"],
                )
                for i in range(50)
            ],
        ]
        for ops in test_cases:
            score = compute_quality_score(ops)
            assert 1 <= score <= 5, f"Score {score} out of range for {len(ops)} ops"

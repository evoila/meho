# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression coverage for captured-JSON result catalog and admission bounds."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard, build_catalog
from meho_backplane.settings import get_settings


def test_catalog_is_immutable_all_row_and_permutation_stable() -> None:
    """Late fields and generated ids derive from lexical external-key order."""
    rows = [{"stable": index} for index in range(201)]
    rows.extend([{"late": "row-201"}, {"final": True}])
    guard = AdmissionGuard()

    catalog = build_catalog(rows, guard=guard)
    permuted = build_catalog(list(reversed(rows)), guard=guard)

    observed = [
        (field.name, field.column, field.kinds, field.queryable) for field in catalog.fields
    ]
    assert observed == [
        ("final", "_rr_v_0000", frozenset({"boolean"}), True),
        ("late", "_rr_v_0001", frozenset({"string"}), True),
        ("stable", "_rr_v_0002", frozenset({"integer"}), True),
    ]
    assert catalog == permuted
    with pytest.raises(FrozenInstanceError):
        catalog.fields[0].name = "changed"  # type: ignore[misc]


def test_catalog_preserves_null_missing_containers_and_case_distinct_keys() -> None:
    """Null and missing remain distinct, with no key normalization or projection."""
    catalog = build_catalog(
        [
            {"Foo": None, "foo": [], "nested": {"a": 1}, "number": 2**63},
            {"Foo": "set", "number": 1.5},
            {"foo": ["later"]},
        ],
        guard=AdmissionGuard(),
    )
    by_name = {field.name: field for field in catalog.fields}

    assert by_name["Foo"].kinds == frozenset({"null", "string"})
    assert by_name["Foo"].nullable is True
    assert by_name["foo"].nullable is True
    assert by_name["foo"].queryable is False
    assert by_name["foo"].scalar_family is None
    assert by_name["nested"].reason == "container_value"
    assert by_name["number"].reason == "mixed_non_null_kinds"
    assert catalog.to_json_schema()["items"]["properties"]["Foo"]["type"] == ["string", "null"]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([{"value": None}], "null_only"),
        ([{"value": 2**63}], "integer_out_of_int64_range"),
        ([{"value": 1}, {"value": 1.5}], "mixed_non_null_kinds"),
    ],
)
def test_catalog_marks_unsafe_families_raw_only(rows: list[dict[str, object]], reason: str) -> None:
    """Unsafe scalar families receive stable catalog reasons without coercion."""
    field = build_catalog(rows, guard=AdmissionGuard()).fields[0]
    assert field.queryable is False
    assert field.reason == reason


def test_admission_guard_counts_depth_nodes_unicode_and_active_cycles() -> None:
    """The iterator walk rejects bounds/cycles and permits shared acyclic values."""
    guard = AdmissionGuard(max_decoded_bytes=64, max_depth=2, max_nodes=8)
    assert guard.check({"snowman": "☃"}) is True
    assert guard.check({"a": {"b": {"c": 1}}}) is False
    assert guard.check({"a": 1, "b": 2, "c": 3, "d": 4}) is False
    assert guard.check({"bad": "\ud800"}) is False
    assert guard.check({"\ud800": "value"}) is False

    shared = {"value": 1}
    assert AdmissionGuard().check([shared, shared]) is True
    cycle: list[object] = []
    cycle.append(cycle)
    assert AdmissionGuard().check(cycle) is False


def test_admission_guard_enforces_exact_and_one_over_work_boundaries() -> None:
    """Byte, node, depth, and UTF-8 key accounting stop at their exact limits."""
    assert AdmissionGuard(max_decoded_bytes=2, max_depth=1, max_nodes=2).check([""]) is True
    assert AdmissionGuard(max_decoded_bytes=1, max_depth=1, max_nodes=2).check([""]) is False
    assert AdmissionGuard(max_decoded_bytes=64, max_depth=1, max_nodes=2).check([None]) is True
    assert AdmissionGuard(max_decoded_bytes=64, max_depth=1, max_nodes=1).check([None]) is False
    assert AdmissionGuard(max_decoded_bytes=64, max_depth=1, max_nodes=2).check([[]]) is True
    assert AdmissionGuard(max_decoded_bytes=64, max_depth=1, max_nodes=3).check([[[]]]) is False
    assert AdmissionGuard(max_decoded_bytes=5, max_depth=1, max_nodes=3).check({"☃": ""}) is True
    assert AdmissionGuard(max_decoded_bytes=4, max_depth=1, max_nodes=3).check({"☃": ""}) is False


def test_default_admission_depth_allows_64_levels_and_rejects_65() -> None:
    """Catalog admission, unlike Analyzer, permits the configured 64 levels."""
    accepted: object = None
    for _ in range(64):
        accepted = [accepted]
    rejected: object = None
    for _ in range(65):
        rejected = [rejected]

    assert AdmissionGuard().check(accepted) is True
    assert AdmissionGuard().check(rejected) is False


def test_admission_limits_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three reducer admission limits use explicit positive env settings."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("RESULT_REDUCTION_MAX_DECODED_BYTES", "123")
    monkeypatch.setenv("RESULT_REDUCTION_MAX_DEPTH", "45")
    monkeypatch.setenv("RESULT_REDUCTION_MAX_NODES", "678")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.result_reduction_max_decoded_bytes == 123
        assert settings.result_reduction_max_depth == 45
        assert settings.result_reduction_max_nodes == 678
    finally:
        get_settings.cache_clear()

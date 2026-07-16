# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the bounded assertion evaluator (#2504).

Initiative #2416, parent goal #221. The evaluator is a pure ``select ->
compare`` function; this file is a pure-module test (no DB / app fixtures,
mirroring ``test_operations_composite_invariant.py``). Coverage is
table-driven across:

* the dotted-path grammar (accept / reject, single-wildcard bound, root);
* strict navigation (missing key, out-of-range index, type mismatch, ``[*]``
  projection);
* every aggregate incl. empty-list identity semantics;
* every comparator's state matrix, boolean-vs-int strictness, and the
  ``freshness`` timestamp forms (``Z``, offset, epoch, naive, garbage, future);
* the never-raises / never-emits-``skip`` contract over a degenerate-payload
  sweep;
* the single permitted exception (naive ``now``).
"""

from __future__ import annotations

import json
import typing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.checks import (
    AssertionOutcome,
    AssertionSpec,
    CheckState,
    evaluate_assertion,
)
from meho_backplane.checks.assertions import PathSegment, parse_path

#: A fixed, timezone-aware instant so freshness cases are deterministic.
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _eval(
    select: dict[str, Any],
    compare: dict[str, Any],
    payload: object,
    *,
    now: datetime = NOW,
) -> AssertionOutcome:
    spec = AssertionSpec.model_validate({"select": select, "compare": compare})
    return evaluate_assertion(spec, payload, now=now)


# --------------------------------------------------------------------------- #
# Path grammar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", ()),
        ("$.a", (PathSegment("key", "a"),)),
        ("$.a-b_9", (PathSegment("key", "a-b_9"),)),
        ("$[0]", (PathSegment("index", None, 0),)),
        (
            "$.disks[*].free_pct",
            (
                PathSegment("key", "disks"),
                PathSegment("wildcard"),
                PathSegment("key", "free_pct"),
            ),
        ),
        (
            "$.a[2][3]",
            (PathSegment("key", "a"), PathSegment("index", None, 2), PathSegment("index", None, 3)),
        ),
    ],
)
def test_path_grammar_accepts(path: str, expected: tuple[PathSegment, ...]) -> None:
    assert parse_path(path) == expected


@pytest.mark.parametrize(
    "path",
    ["", "a.b", "$.", "$.a b", "$[x]", "$..a", "$.a.", "$[-1]", "$.a[]", "$ .a"],
)
def test_path_grammar_rejects(path: str) -> None:
    with pytest.raises(ValueError):
        parse_path(path)


def test_path_grammar_rejects_second_wildcard() -> None:
    with pytest.raises(ValueError, match="at most one"):
        parse_path("$.a[*].b[*].c")
    # A single wildcard is fine.
    assert PathSegment("wildcard") in parse_path("$.a[*].b")


def test_bad_path_is_rejected_at_spec_parse_time() -> None:
    # #2503 relies on this being a 422 (ValidationError) at Sensor create.
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {"select": {"path": "$.a[*].b[*]"}, "compare": {"type": "bool"}}
        )


# --------------------------------------------------------------------------- #
# Navigation / select
# --------------------------------------------------------------------------- #


def test_path_missing_key_yields_unknown() -> None:
    out = _eval({"path": "$.disks"}, {"type": "bool"}, {"other": 1})
    assert out.state == "unknown"
    assert "missing key 'disks'" in out.evidence["reason"]
    assert out.value is None


def test_index_out_of_range_yields_unknown() -> None:
    out = _eval({"path": "$.xs[3]"}, {"type": "bool"}, {"xs": [True]})
    assert out.state == "unknown"
    assert "out of range" in out.evidence["reason"]


def test_type_mismatch_yields_unknown() -> None:
    out = _eval({"path": "$.a.b"}, {"type": "bool"}, {"a": 5})
    assert out.state == "unknown"
    assert "expected object at '.b'" in out.evidence["reason"]


def test_wildcard_projection_selects_list() -> None:
    out = _eval(
        {"path": "$.disks[*].free_pct", "aggregate": "min"},
        {"type": "threshold", "op": "lt", "degraded": 20, "critical": 5},
        {"disks": [{"free_pct": 3.1}, {"free_pct": 42.0}]},
    )
    assert (out.state, out.value) == ("critical", 3.1)


def test_wildcard_on_non_list_yields_unknown() -> None:
    out = _eval(
        {"path": "$.disks[*].v", "aggregate": "count"},
        {"type": "threshold", "op": "gte", "critical": 0},
        {"disks": {"not": "a list"}},
    )
    assert out.state == "unknown"
    assert "expected array before '[*]'" in out.evidence["reason"]


def test_wildcard_element_failure_names_element() -> None:
    out = _eval(
        {"path": "$.disks[*].free_pct", "aggregate": "min"},
        {"type": "threshold", "op": "lt", "critical": 5},
        {"disks": [{"free_pct": 3.1}, {"missing": True}]},
    )
    assert out.state == "unknown"
    assert "'[*]' element 1" in out.evidence["reason"]


def test_scalar_required_without_aggregate() -> None:
    out = _eval({"path": "$.disks"}, {"type": "bool"}, {"disks": [1, 2, 3]})
    assert out.state == "unknown"
    assert "not a scalar" in out.evidence["reason"]


def test_list_required_with_aggregate() -> None:
    out = _eval(
        {"path": "$.n", "aggregate": "sum"},
        {"type": "threshold", "op": "gt", "critical": 0},
        {"n": 5},
    )
    assert out.state == "unknown"
    assert "requires an array" in out.evidence["reason"]


def test_root_selection() -> None:
    out = _eval(
        {"path": "$", "aggregate": "sum"},
        {"type": "threshold", "op": "gt", "critical": 10},
        [1, 2, 3],
    )
    assert (out.state, out.value) == ("ok", 6)


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #


def test_aggregate_empty_list_semantics() -> None:
    # count -> 0, sum -> 0, any -> False, all -> True (Python identity elements).
    count = _eval(
        {"path": "$.xs[*].v", "aggregate": "count"},
        {"type": "threshold", "op": "gt", "critical": 100},
        {"xs": []},
    )
    assert (count.state, count.value) == ("ok", 0)
    total = _eval(
        {"path": "$.xs[*].v", "aggregate": "sum"},
        {"type": "threshold", "op": "gt", "critical": 100},
        {"xs": []},
    )
    assert (total.state, total.value) == ("ok", 0)
    any_ = _eval(
        {"path": "$.xs[*]", "aggregate": "any"}, {"type": "bool", "expect": False}, {"xs": []}
    )
    assert (any_.state, any_.value) == ("ok", False)
    all_ = _eval(
        {"path": "$.xs[*]", "aggregate": "all"}, {"type": "bool", "expect": True}, {"xs": []}
    )
    assert (all_.state, all_.value) == ("ok", True)
    # max / min on an empty list are undefined -> unknown.
    for agg in ("max", "min"):
        out = _eval(
            {"path": "$.xs[*].v", "aggregate": agg},
            {"type": "threshold", "op": "gt", "critical": 0},
            {"xs": []},
        )
        assert out.state == "unknown", agg
        assert "empty list" in out.evidence["reason"]


@pytest.mark.parametrize(
    ("aggregate", "elements", "expected"),
    [
        ("count", [10, 20, 30], 3),
        ("sum", [1, 2, 3.5], 6.5),
        ("max", [3.1, 42.0, 7], 42.0),
        ("min", [3.1, 42.0, 7], 3.1),
    ],
)
def test_numeric_aggregates(aggregate: str, elements: list[Any], expected: Any) -> None:
    out = _eval(
        {"path": "$.xs[*]", "aggregate": aggregate},
        {"type": "threshold", "op": "gt", "critical": 1000},
        {"xs": elements},
    )
    assert (out.state, out.value) == ("ok", expected)


def test_numeric_aggregate_rejects_bool_and_nonnumber() -> None:
    for elements in ([1, True], [1, "x"], [1, None]):
        out = _eval(
            {"path": "$.xs[*]", "aggregate": "sum"},
            {"type": "threshold", "op": "gt", "critical": 0},
            {"xs": elements},
        )
        assert out.state == "unknown", elements
        assert "requires numbers" in out.evidence["reason"]


@pytest.mark.parametrize(
    ("aggregate", "elements", "value"),
    [("any", [False, True], True), ("all", [True, True], True), ("all", [True, False], False)],
)
def test_boolean_aggregates(aggregate: str, elements: list[Any], value: bool) -> None:
    out = _eval(
        {"path": "$.xs[*]", "aggregate": aggregate},
        {"type": "bool", "expect": value},
        {"xs": elements},
    )
    assert (out.state, out.value) == ("ok", value)


def test_boolean_aggregate_rejects_non_bool() -> None:
    out = _eval({"path": "$.xs[*]", "aggregate": "all"}, {"type": "bool"}, {"xs": [True, 1]})
    assert out.state == "unknown"
    assert "requires booleans" in out.evidence["reason"]


# --------------------------------------------------------------------------- #
# Threshold comparator
# --------------------------------------------------------------------------- #


def test_threshold_degraded_then_critical_ordering() -> None:
    # lt: critical band (< 5) wins over the degraded band (< 20) it also enters.
    lt = {"type": "threshold", "op": "lt", "degraded": 20, "critical": 5}
    assert _eval({"path": "$.v"}, lt, {"v": 3.1}).state == "critical"
    assert _eval({"path": "$.v"}, lt, {"v": 12}).state == "degraded"
    assert _eval({"path": "$.v"}, lt, {"v": 50}).state == "ok"
    # gt: symmetric in the other direction.
    gt = {"type": "threshold", "op": "gt", "degraded": 80, "critical": 95}
    assert _eval({"path": "$.v"}, gt, {"v": 97}).state == "critical"
    assert _eval({"path": "$.v"}, gt, {"v": 85}).state == "degraded"
    assert _eval({"path": "$.v"}, gt, {"v": 50}).state == "ok"


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte"])
def test_threshold_boundary_inclusive_exclusive(op: str) -> None:
    single = {"type": "threshold", "op": op, "critical": 10}
    at_bound = _eval({"path": "$.v"}, single, {"v": 10}).state
    # gte/lte include the bound (critical at 10); gt/lt exclude it (ok at 10).
    assert at_bound == ("critical" if op in ("gte", "lte") else "ok")


def test_threshold_non_numeric_is_unknown() -> None:
    out = _eval({"path": "$.v"}, {"type": "threshold", "op": "gt", "critical": 0}, {"v": "high"})
    assert out.state == "unknown"
    assert out.evidence["observed"] == "high"  # observed retained on unknown
    out_bool = _eval({"path": "$.v"}, {"type": "threshold", "op": "gt", "critical": 0}, {"v": True})
    assert out_bool.state == "unknown"  # bool is not a number


def test_threshold_requires_a_bound() -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {"select": {"path": "$.v"}, "compare": {"type": "threshold", "op": "gt"}}
        )


@pytest.mark.parametrize(
    ("op", "degraded", "critical"),
    [("gt", 95, 80), ("gte", 95, 80), ("lt", 5, 20), ("lte", 5, 20)],
)
def test_threshold_bound_ordering_validated(op: str, degraded: float, critical: float) -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {
                "select": {"path": "$.v"},
                "compare": {
                    "type": "threshold",
                    "op": op,
                    "degraded": degraded,
                    "critical": critical,
                },
            }
        )


# --------------------------------------------------------------------------- #
# Non-finite (NaN / Infinity) handling  (#2504 review B1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_threshold_non_finite_value_is_unknown(value: float) -> None:
    # A non-finite observed value cannot be judged: `nan <op> bound` is False on
    # every side (a silent false-negative reading as ok) and +/-inf is degenerate,
    # so it must route to unknown like any other unjudgeable value.
    out = _eval(
        {"path": "$.v"},
        {"type": "threshold", "op": "gt", "degraded": 80, "critical": 95},
        {"v": value},
    )
    assert out.state == "unknown"
    assert "finite" in out.evidence["reason"]


def test_threshold_nan_from_json_payload_is_unknown() -> None:
    # Reachable in production: stdlib json.loads accepts the bare NaN token.
    payload = json.loads('{"v": NaN}')
    out = _eval({"path": "$.v"}, {"type": "threshold", "op": "lt", "critical": 5}, payload)
    assert out.state == "unknown"


@pytest.mark.parametrize("aggregate", ["sum", "max", "min"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_numeric_aggregate_non_finite_element_is_unknown(aggregate: str, bad: float) -> None:
    out = _eval(
        {"path": "$.xs[*]", "aggregate": aggregate},
        {"type": "threshold", "op": "gt", "critical": 0},
        {"xs": [1.0, bad, 2.0]},
    )
    assert out.state == "unknown"
    assert "finite" in out.evidence["reason"]


def test_sum_aggregate_non_finite_result_is_unknown() -> None:
    # Finite elements whose sum overflows the float range to +inf are unknown too.
    out = _eval(
        {"path": "$.xs[*]", "aggregate": "sum"},
        {"type": "threshold", "op": "gt", "critical": 0},
        {"xs": [1.5e308, 1.5e308]},
    )
    assert out.state == "unknown"
    assert "finite" in out.evidence["reason"]


def test_sum_aggregate_bignum_plus_float_overflow_is_unknown() -> None:
    # #2504 review B2: an arbitrary-precision int (reachable via json.loads)
    # summed with a float promotes the int to float and raises OverflowError.
    # The evaluator must never raise on payload data -> unknown, not a crash.
    out = _eval(
        {"path": "$.xs[*]", "aggregate": "sum"},
        {"type": "threshold", "op": "gt", "critical": 0},
        {"xs": [10**400, 1.5]},
    )
    assert out.state == "unknown"
    assert "overflow" in out.evidence["reason"]


@pytest.mark.parametrize("field", ["degraded", "critical"])
@pytest.mark.parametrize("bound", [float("nan"), float("inf"), float("-inf")])
def test_threshold_non_finite_bound_rejected_at_parse(field: str, bound: float) -> None:
    # A non-finite bound is a 422 at Sensor create (#2503), not an eval-time surprise.
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {
                "select": {"path": "$.v"},
                "compare": {"type": "threshold", "op": "gt", field: bound},
            }
        )


# --------------------------------------------------------------------------- #
# Spec models forbid unknown fields  (#2504 review M1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("select", "compare"),
    [
        # A typo'd field beside valid ones must fail, not be silently dropped
        # (dropping `expectt` would leave `expect` defaulted to True, inverting it).
        ({"path": "$.v"}, {"type": "bool", "expect": True, "expectt": False}),
        # A misspelled bound must fail, not leave the real bound unset.
        ({"path": "$.v"}, {"type": "threshold", "op": "gt", "critical": 5, "criticl": 9}),
        # Unknown key on the select stage.
        ({"path": "$.v", "aggregate": "sum", "aggregat": "min"}, {"type": "bool"}),
        # Unknown key on the equality comparators.
        ({"path": "$.v"}, {"type": "equals", "value": 1, "valeu": 2}),
        ({"path": "$.v"}, {"type": "in", "values": [1], "vals": [2]}),
        # Unknown key on freshness.
        ({"path": "$.v"}, {"type": "freshness", "max_age_seconds": 60, "maxage": 1}),
    ],
)
def test_spec_models_forbid_unknown_fields(select: dict[str, Any], compare: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate({"select": select, "compare": compare})


def test_assertion_spec_forbids_unknown_top_level_field() -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {"select": {"path": "$.v"}, "compare": {"type": "bool"}, "severty": "critical"}
        )


# --------------------------------------------------------------------------- #
# Equality comparators
# --------------------------------------------------------------------------- #


def test_equals_bool_is_not_int() -> None:
    # Python's True == 1 footgun is neutralized in both directions.
    assert _eval({"path": "$.v"}, {"type": "equals", "value": True}, {"v": 1}).state == "critical"
    assert _eval({"path": "$.v"}, {"type": "equals", "value": 1}, {"v": True}).state == "critical"
    assert _eval({"path": "$.v"}, {"type": "equals", "value": True}, {"v": True}).state == "ok"
    assert _eval({"path": "$.v"}, {"type": "in", "values": [1, 2]}, {"v": True}).state == "critical"


@pytest.mark.parametrize(
    ("expected", "observed", "state"),
    [
        ("running", "running", "ok"),
        ("running", "stopped", "critical"),
        (200, 200, "ok"),
        (1, 1.0, "ok"),  # int/float equality preserved (only bool is special-cased)
        (None, None, "ok"),
        (None, 0, "critical"),
    ],
)
def test_equals_matrix(expected: Any, observed: Any, state: str) -> None:
    out = _eval({"path": "$.v"}, {"type": "equals", "value": expected}, {"v": observed})
    assert out.state == state


def test_in_membership() -> None:
    comp = {"type": "in", "values": ["running", "degraded"]}
    assert _eval({"path": "$.v"}, comp, {"v": "degraded"}).state == "ok"
    assert _eval({"path": "$.v"}, comp, {"v": "failed"}).state == "critical"


def test_in_requires_nonempty_values() -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {"select": {"path": "$.v"}, "compare": {"type": "in", "values": []}}
        )


# --------------------------------------------------------------------------- #
# Bool comparator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expect", "observed", "state"),
    [
        (True, True, "ok"),
        (True, False, "critical"),
        (False, False, "ok"),
        (False, True, "critical"),
    ],
)
def test_bool_comparator_matrix(expect: bool, observed: bool, state: str) -> None:
    out = _eval({"path": "$.v"}, {"type": "bool", "expect": expect}, {"v": observed})
    assert out.state == state


def test_bool_comparator_requires_strict_bool() -> None:
    for observed in (1, 0, "true", None):
        out = _eval({"path": "$.v"}, {"type": "bool"}, {"v": observed})
        assert out.state == "unknown", observed


# --------------------------------------------------------------------------- #
# Freshness comparator
# --------------------------------------------------------------------------- #


def test_freshness_naive_timestamp_unknown() -> None:
    out = _eval(
        {"path": "$.ts"},
        {"type": "freshness", "max_age_seconds": 3600},
        {"ts": "2026-07-16T10:00:00"},
    )
    assert out.state == "unknown"
    assert "naive timestamp" in out.evidence["reason"]


def test_freshness_epoch_seconds() -> None:
    fresh = (NOW - timedelta(seconds=30)).timestamp()
    stale = (NOW - timedelta(seconds=7200)).timestamp()
    assert (
        _eval({"path": "$.ts"}, {"type": "freshness", "max_age_seconds": 3600}, {"ts": fresh}).state
        == "ok"
    )
    assert (
        _eval(
            {"path": "$.ts"}, {"type": "freshness", "max_age_seconds": 3600}, {"ts": int(stale)}
        ).state
        == "critical"
    )


@pytest.mark.parametrize(
    ("ts", "state"),
    [
        ("2026-07-16T11:59:30Z", "ok"),  # 30s old, Z offset
        ("2026-07-16T13:59:30+02:00", "ok"),  # same instant via numeric offset
        ("2026-07-16T11:30:00Z", "degraded"),  # 30min old
        ("2026-07-16T10:00:00Z", "critical"),  # 2h old
        ("2026-07-16T12:05:00Z", "ok"),  # future -> ok
    ],
)
def test_freshness_string_offsets_and_bands(ts: str, state: str) -> None:
    comp = {"type": "freshness", "max_age_seconds": 3600, "degraded_age_seconds": 600}
    out = _eval({"path": "$.ts"}, comp, {"ts": ts})
    assert out.state == state
    assert "age_seconds" in out.evidence


@pytest.mark.parametrize("bad", ["not-a-date", True, None, {"nested": 1}, [1, 2]])
def test_freshness_uninterpretable_timestamp_unknown(bad: Any) -> None:
    out = _eval({"path": "$.ts"}, {"type": "freshness", "max_age_seconds": 3600}, {"ts": bad})
    assert out.state == "unknown"
    assert out.evidence["reason"]


def test_freshness_degraded_age_must_be_below_max() -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {
                "select": {"path": "$.ts"},
                "compare": {
                    "type": "freshness",
                    "max_age_seconds": 600,
                    "degraded_age_seconds": 600,
                },
            }
        )


# --------------------------------------------------------------------------- #
# Contract-wide invariants
# --------------------------------------------------------------------------- #


def test_naive_now_raises_value_error() -> None:
    spec = AssertionSpec.model_validate({"select": {"path": "$.v"}, "compare": {"type": "bool"}})
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_assertion(spec, {"v": True}, now=datetime(2026, 7, 16, 12, 0, 0))


def test_unknown_type_tag_rejected() -> None:
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate(
            {"select": {"path": "$.v"}, "compare": {"type": "regex", "pattern": ".*"}}
        )


def test_evidence_carries_minimum_keys_and_is_json_serializable() -> None:
    out = _eval(
        {"path": "$.disks[*].free_pct", "aggregate": "min"},
        {"type": "threshold", "op": "lt", "degraded": 20, "critical": 5},
        {"disks": [{"free_pct": 3.1}]},
    )
    for key in ("path", "aggregate", "comparator", "observed", "op", "degraded", "critical"):
        assert key in out.evidence, key
    # The whole outcome round-trips through JSON (persisted by #2505, rendered by #2506).
    json.dumps(out.model_dump())


def test_outcome_and_specs_are_frozen() -> None:
    out = _eval({"path": "$.v"}, {"type": "bool"}, {"v": True})
    with pytest.raises(ValidationError):
        out.state = "ok"  # type: ignore[misc]
    spec = AssertionSpec.model_validate({"select": {"path": "$.v"}, "compare": {"type": "bool"}})
    with pytest.raises(ValidationError):
        spec.select = spec.select  # type: ignore[misc]


def test_check_state_vocabulary_is_the_five_states() -> None:
    assert set(typing.get_args(CheckState)) == {"ok", "degraded", "critical", "unknown", "skip"}


def test_evaluator_never_raises_or_emits_skip() -> None:
    # A degenerate-payload sweep: no shape raises, and 'skip' is never emitted
    # (it is a scheduling-time fact a pure function cannot observe, per #2506).
    payloads: list[object] = [
        None,
        0,
        3.14,
        "scalar",
        True,
        [],
        {},
        [1, 2, 3],
        {"a": {"b": {"c": [1, 2]}}},
        {"summary": "reduced markdown", "handle": "s3://bucket/key"},  # a reducer summary dict
        {"disks": [{"free_pct": 3.1}, {"free_pct": "n/a"}]},
        {"disks": [{"free_pct": 10**400}, {"free_pct": 1.5}]},  # bignum+float sum overflow (B2)
    ]
    specs = [
        ({"path": "$.a", "aggregate": None}, {"type": "equals", "value": 1}),
        ({"path": "$.a", "aggregate": None}, {"type": "in", "values": ["x", 1, True]}),
        ({"path": "$.a", "aggregate": None}, {"type": "bool"}),
        (
            {"path": "$.disks[*].free_pct", "aggregate": "sum"},
            {"type": "threshold", "op": "gt", "critical": 0},
        ),
        (
            {"path": "$.disks[*].free_pct", "aggregate": "min"},
            {"type": "threshold", "op": "lt", "critical": 5},
        ),
        ({"path": "$.a", "aggregate": None}, {"type": "freshness", "max_age_seconds": 60}),
        ({"path": "$", "aggregate": "count"}, {"type": "threshold", "op": "gte", "critical": 1}),
    ]
    emitted: set[str] = set()
    for payload in payloads:
        for select, compare in specs:
            out = _eval(select, compare, payload)
            assert out.state in ("ok", "degraded", "critical", "unknown")
            assert out.state != "skip"
            if out.state == "unknown":
                assert out.value is None
                assert out.evidence.get("reason")
            emitted.add(out.state)
    assert "skip" not in emitted

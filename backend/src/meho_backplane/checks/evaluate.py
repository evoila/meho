# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure, no-I/O evaluator for the bounded assertion spec (#2504).

:func:`evaluate_assertion` maps ``(AssertionSpec, op-result payload, now)`` to
an :class:`~meho_backplane.checks.assertions.AssertionOutcome`. It is a plain
synchronous function with no I/O, no async, and no imports beyond the stdlib,
Pydantic (transitively, via the spec models), and this package.

Never-raises contract
=====================

Mirroring the dispatcher's never-raises posture and the reducer Protocol's
"tolerate every payload shape" rule, **no payload or spec/payload mismatch
raises** -- every mismatch (missing key, out-of-range index, type mismatch,
non-numeric threshold input, unparseable timestamp, ...) becomes
``state="unknown"`` with an ``evidence["reason"]`` naming the failure. The one
permitted exception is :class:`ValueError` on a naive ``now``: that is caller
misuse, not payload data, and the same aware-datetime discipline the scheduler
enforces -- a host-TZ-dependent "now" would make the freshness comparison
non-deterministic.

Aggregate semantics on an empty list follow Python's identity elements and are
fixed by contract: ``count`` -> ``0``, ``sum`` -> ``0``, ``any`` -> ``False``,
``all`` -> ``True``; ``max`` / ``min`` on an empty list are ``unknown``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from meho_backplane.checks.assertions import (
    AssertionOutcome,
    AssertionSpec,
    BoolCompare,
    CheckState,
    Compare,
    EqualsCompare,
    FreshnessCompare,
    InCompare,
    PathSegment,
    ThresholdCompare,
    parse_path,
)

__all__ = ["evaluate_assertion"]

#: Sentinel distinguishing "no observed value supplied" from "observed is None"
#: in :func:`_unknown`.
_UNSET: Any = object()


def evaluate_assertion(spec: AssertionSpec, payload: object, *, now: datetime) -> AssertionOutcome:
    """Evaluate *spec* against *payload* at instant *now*.

    Args:
        spec: The validated assertion (select stage + typed comparator).
        payload: The op-result JSON the assertion runs over. Any shape is
            tolerated; an incompatible shape yields ``unknown``.
        now: A timezone-aware instant the caller injects (the runner passes
            ``datetime.now(UTC)``). Used only by the ``freshness`` comparator,
            but validated unconditionally.

    Returns:
        An :class:`AssertionOutcome` with ``state`` in
        ``{ok, degraded, critical, unknown}`` (never ``skip``), the observed
        ``value``, and JSON-serializable ``evidence``.

    Raises:
        ValueError: *now* is naive (missing timezone). This is the only
            exception the evaluator ever raises -- payload data never does.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")

    compare = spec.compare
    evidence: dict[str, Any] = {
        "path": spec.select.path,
        "aggregate": spec.select.aggregate,
        "comparator": compare.type,
    }
    evidence.update(_expected_evidence(compare))

    selected, select_err = _select(payload, parse_path(spec.select.path))
    if select_err is not None:
        return _unknown(evidence, select_err)

    if spec.select.aggregate is None:
        if isinstance(selected, (dict, list)):
            return _unknown(
                evidence,
                f"selection is not a scalar, found {_typename(selected)}",
                observed=_typename(selected),
            )
        value: Any = selected
    else:
        if not isinstance(selected, list):
            return _unknown(
                evidence,
                f"aggregate {spec.select.aggregate!r} requires an array, "
                f"found {_typename(selected)}",
                observed=_typename(selected),
            )
        value, agg_err = _aggregate(selected, spec.select.aggregate)
        if agg_err is not None:
            return _unknown(evidence, agg_err)

    evidence["observed"] = value
    return _compare(compare, value, now, evidence)


# --------------------------------------------------------------------------- #
# Select / navigate
# --------------------------------------------------------------------------- #


def _select(payload: Any, segments: tuple[PathSegment, ...]) -> tuple[Any, str | None]:
    """Resolve *segments* against *payload*.

    Returns ``(value, None)`` on success or ``(None, reason)`` on any strict
    navigation failure. A ``[*]`` wildcard projects the remaining segments over
    every element of the list it stands on, collecting a new list; a failure in
    any element fails the whole selection (naming the element).
    """
    for i, segment in enumerate(segments):
        if segment.kind != "wildcard":
            continue
        base, err = _navigate(payload, segments[:i])
        if err is not None:
            return None, err
        if not isinstance(base, list):
            return None, f"expected array before '[*]', found {_typename(base)}"
        projected: list[Any] = []
        for elem_index, element in enumerate(base):
            elem_value, elem_err = _navigate(element, segments[i + 1 :])
            if elem_err is not None:
                return None, f"'[*]' element {elem_index}: {elem_err}"
            projected.append(elem_value)
        return projected, None
    return _navigate(payload, segments)


def _navigate(value: Any, segments: tuple[PathSegment, ...]) -> tuple[Any, str | None]:
    """Walk key / index *segments* (no wildcard) strictly from *value*."""
    current = value
    for segment in segments:
        if segment.kind == "key":
            if not isinstance(current, dict):
                return None, f"expected object at '.{segment.key}', found {_typename(current)}"
            if segment.key not in current:
                return None, f"missing key '{segment.key}'"
            current = current[segment.key]
        elif segment.kind == "index":
            index = segment.idx
            assert index is not None  # parse_path guarantees index kind carries an int
            if not isinstance(current, list):
                return None, f"expected array at '[{index}]', found {_typename(current)}"
            if index >= len(current):
                return None, f"index [{index}] out of range (length {len(current)})"
            current = current[index]
        else:  # pragma: no cover - _select consumes the sole wildcard first
            return None, "unexpected wildcard during navigation"
    return current, None


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


def _aggregate(values: list[Any], aggregate: str) -> tuple[Any, str | None]:
    """Reduce *values* by *aggregate*.

    Empty-list identity: ``count`` -> 0, ``sum`` -> 0, ``any`` -> False,
    ``all`` -> True; ``max`` / ``min`` on an empty list are ``unknown``.
    """
    if aggregate == "count":
        return len(values), None
    if aggregate in ("sum", "max", "min"):
        for element in values:
            if isinstance(element, bool) or not isinstance(element, (int, float)):
                return None, f"aggregate {aggregate!r} requires numbers, found {_typename(element)}"
            # Only a float can be non-finite; an int is always finite (and passing
            # a huge int to math.isfinite would itself raise OverflowError).
            if isinstance(element, float) and not math.isfinite(element):
                return None, f"aggregate {aggregate!r} requires finite numbers, found {element!r}"
        if aggregate == "sum":
            try:
                total = sum(values)
            except OverflowError:
                # A huge int summed with a float promotes the int to float and
                # overflows (e.g. [10**400, 1.5], reachable via json.loads);
                # the payload cannot be judged, so it is unknown, not a raise.
                return None, "aggregate 'sum' overflows the float range"
            if isinstance(total, float) and not math.isfinite(total):
                return None, f"aggregate 'sum' is non-finite ({total!r})"
            return total, None
        if not values:
            return None, f"aggregate {aggregate!r} is undefined on an empty list"
        return (max(values) if aggregate == "max" else min(values)), None
    # any / all: strictly boolean elements, no truthiness coercion
    for element in values:
        if not isinstance(element, bool):
            return None, f"aggregate {aggregate!r} requires booleans, found {_typename(element)}"
    return (any(values) if aggregate == "any" else all(values)), None


# --------------------------------------------------------------------------- #
# Compare
# --------------------------------------------------------------------------- #


def _compare(
    compare: Compare, value: Any, now: datetime, evidence: dict[str, Any]
) -> AssertionOutcome:
    """Dispatch *value* to the typed comparator and build the outcome."""
    if isinstance(compare, ThresholdCompare):
        return _compare_threshold(compare, value, evidence)
    if isinstance(compare, EqualsCompare):
        eq_state: CheckState = "ok" if _strict_eq(value, compare.value) else "critical"
        return AssertionOutcome(state=eq_state, value=value, evidence=evidence)
    if isinstance(compare, InCompare):
        in_state: CheckState = (
            "ok" if any(_strict_eq(value, member) for member in compare.values) else "critical"
        )
        return AssertionOutcome(state=in_state, value=value, evidence=evidence)
    if isinstance(compare, BoolCompare):
        if not isinstance(value, bool):
            return _unknown(
                evidence, f"bool comparator requires a boolean, found {_typename(value)}"
            )
        bool_state: CheckState = "ok" if value == compare.expect else "critical"
        return AssertionOutcome(state=bool_state, value=value, evidence=evidence)
    return _compare_freshness(compare, value, now, evidence)


def _compare_threshold(
    compare: ThresholdCompare, value: Any, evidence: dict[str, Any]
) -> AssertionOutcome:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _unknown(
            evidence, f"threshold comparator requires a number, found {_typename(value)}"
        )
    # A non-finite value (NaN/+-Inf, reachable via json.loads) cannot be judged:
    # every `value <op> bound` is False for NaN, so it would silently read as ok.
    if isinstance(value, float) and not math.isfinite(value):
        return _unknown(evidence, f"threshold comparator requires a finite number, found {value!r}")
    state: CheckState = "ok"
    if compare.critical is not None and _violates(value, compare.op, compare.critical):
        state = "critical"
    elif compare.degraded is not None and _violates(value, compare.op, compare.degraded):
        state = "degraded"
    return AssertionOutcome(state=state, value=value, evidence=evidence)


def _compare_freshness(
    compare: FreshnessCompare, value: Any, now: datetime, evidence: dict[str, Any]
) -> AssertionOutcome:
    timestamp, err = _parse_timestamp(value)
    if err is not None:
        return _unknown(evidence, err)
    age_seconds = (now - timestamp).total_seconds()
    evidence["age_seconds"] = age_seconds
    state: CheckState
    if age_seconds > compare.max_age_seconds:
        state = "critical"
    elif compare.degraded_age_seconds is not None and age_seconds > compare.degraded_age_seconds:
        state = "degraded"
    else:
        state = "ok"
    return AssertionOutcome(state=state, value=value, evidence=evidence)


def _parse_timestamp(value: Any) -> tuple[datetime, str | None]:
    """Interpret *value* as a timestamp: aware RFC3339 string or epoch seconds.

    Returns ``(timestamp, None)`` or ``(_EPOCH, reason)`` -- the returned
    datetime is meaningless when a reason is present and callers must gate on
    the reason first.
    """
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return _EPOCH, f"unparseable timestamp string {value!r}"
        if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
            return _EPOCH, f"naive timestamp {value!r} (no timezone offset)"
        return timestamp, None
    if isinstance(value, bool):
        return _EPOCH, "boolean is not a timestamp"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC), None
        except (ValueError, OverflowError, OSError):
            return _EPOCH, f"epoch seconds {value!r} out of range"
    return _EPOCH, f"cannot interpret {_typename(value)} as a timestamp"


_EPOCH = datetime.fromtimestamp(0, tz=UTC)


def _violates(value: float, op: str, bound: float) -> bool:
    """Return whether ``value <op> bound`` (the violation predicate)."""
    if op == "gt":
        return value > bound
    if op == "gte":
        return value >= bound
    if op == "lt":
        return value < bound
    return value <= bound  # lte


def _strict_eq(left: Any, right: Any) -> bool:
    """Equality that keeps ``bool`` distinct from ``int``.

    ``True == 1`` and ``False == 0`` are ``False`` here: when either side is a
    boolean, both sides must be booleans to be equal. Otherwise plain ``==``
    applies (so ``1 == 1.0`` stays true).
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return bool(left == right)


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #


def _expected_evidence(compare: Compare) -> dict[str, Any]:
    """The comparator's declared bounds/values, for the evidence dict."""
    if isinstance(compare, ThresholdCompare):
        return {"op": compare.op, "degraded": compare.degraded, "critical": compare.critical}
    if isinstance(compare, EqualsCompare):
        return {"expected": compare.value}
    if isinstance(compare, InCompare):
        return {"expected": list(compare.values)}
    if isinstance(compare, BoolCompare):
        return {"expect": compare.expect}
    return {
        "max_age_seconds": compare.max_age_seconds,
        "degraded_age_seconds": compare.degraded_age_seconds,
    }


def _unknown(evidence: dict[str, Any], reason: str, observed: Any = _UNSET) -> AssertionOutcome:
    """Build an ``unknown`` outcome, recording *reason* (and *observed*)."""
    enriched = dict(evidence)
    enriched["reason"] = reason
    if observed is not _UNSET:
        enriched["observed"] = observed
    elif "observed" not in enriched:
        enriched["observed"] = reason
    return AssertionOutcome(state="unknown", value=None, evidence=enriched)


def _typename(value: Any) -> str:
    """A JSON-flavored type name for evidence messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__

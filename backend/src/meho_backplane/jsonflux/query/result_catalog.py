# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded, lossless top-level field profiles for captured result rows.

The catalog observes the JSON rows retained by the reducer directly.  It does
not project nested values or construct a relational table: those are separate,
lazy concerns of the drill-in query path.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "AdmissionGuard",
    "AdmissionLimitError",
    "ResultField",
    "ResultFieldCatalog",
    "build_catalog",
]

_KIND_ORDER: Final[dict[str, int]] = {
    "boolean": 0,
    "integer": 1,
    "number": 2,
    "string": 3,
    "array": 4,
    "object": 5,
    "null": 6,
}
_SAFE_SCALAR_KINDS: Final[frozenset[str]] = frozenset({"boolean", "integer", "number", "string"})
_INT64_MIN: Final[int] = -(2**63)
_INT64_MAX: Final[int] = 2**63 - 1
_STRING_CHUNK_CHARS: Final[int] = 4096


class AdmissionLimitError(ValueError):
    """The bounded catalog walk cannot safely inspect a captured result."""

    code: Final[str] = "admission_limit_exceeded"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionGuard:
    """Bound catalog work before a payload is serialized or profiled.

    ``max_decoded_bytes`` is a work budget, not a transport or heap-size
    claim.  It accounts for UTF-8 key/string bytes, JSON punctuation for
    containers, fixed scalar tokens, and integer magnitude.  The traversal
    keeps only an iterator stack plus the active ancestry, so wide objects do
    not allocate a child worklist and shared acyclic objects remain valid.
    """

    max_decoded_bytes: int = 67_108_864
    max_depth: int = 64
    max_nodes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.max_decoded_bytes <= 0 or self.max_depth <= 0 or self.max_nodes <= 0:
            raise ValueError("admission limits must be positive")

    def require(self, value: Any) -> None:
        """Raise a coded error when *value* is not bounded JSON input."""
        if not self.check(value):
            raise AdmissionLimitError()

    # code-quality-allow: bounded iterator walk keeps accounting and active ancestry together
    def check(self, value: Any) -> bool:
        """Return whether *value* fits the configured bounded JSON walk."""
        size = 0
        nodes = 0
        active: set[int] = set()
        stack: list[tuple[Iterator[Any], int, int, bool]] = []

        def add_size(amount: int) -> bool:
            nonlocal size
            size += amount
            return size <= self.max_decoded_bytes

        def add_string(text: str) -> bool:
            # Character count is a cheap lower bound.  Encode only bounded
            # chunks so an extremely large Unicode string never allocates a
            # second full-size byte buffer merely for admission accounting.
            if len(text) > self.max_decoded_bytes - size:
                return False
            try:
                for start in range(0, len(text), _STRING_CHUNK_CHARS):
                    if not add_size(len(text[start : start + _STRING_CHUNK_CHARS].encode("utf-8"))):
                        return False
            except UnicodeEncodeError:
                return False
            return True

        def add_node() -> bool:
            nonlocal nodes
            nodes += 1
            return nodes <= self.max_nodes

        def inspect(item: Any, depth: int) -> bool:
            if not add_node() or depth > self.max_depth:
                return False
            if item is None:
                return add_size(4)
            if isinstance(item, bool):
                return add_size(5)
            if isinstance(item, int):
                # ``bit_length`` avoids decimal-string conversion and still
                # makes enormous Python integers consume a proportional
                # rational upper-bound estimate of decimal digits.
                decimal_digits = max(1, (item.bit_length() * 30_103 + 99_999) // 100_000)
                return add_size(decimal_digits + (1 if item < 0 else 0))
            if isinstance(item, float):
                return math.isfinite(item) and add_size(24)
            if isinstance(item, str):
                return add_string(item)
            if not isinstance(item, (dict, list)):
                return False

            identity = id(item)
            if identity in active:
                return False
            if not add_size(2):
                return False
            active.add(identity)
            if isinstance(item, dict):
                stack.append((iter(item.items()), identity, depth, True))
            else:
                stack.append((iter(item), identity, depth, False))
            return True

        if not inspect(value, 0):
            return False
        while stack:
            iterator, identity, parent_depth, dictionary = stack[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                active.remove(identity)
                stack.pop()
                continue
            if dictionary:
                key, child = entry
                if not isinstance(key, str) or not add_node() or not add_string(key):
                    return False
            else:
                child = entry
            if not inspect(child, parent_depth + 1):
                return False
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultField:
    """One exact top-level captured key and its safe relational status."""

    name: str
    column: str
    kinds: frozenset[str]
    nullable: bool
    queryable: bool
    reason: str | None
    scalar_family: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultFieldCatalog:
    """Deterministically ordered profile of every top-level captured key."""

    fields: tuple[ResultField, ...]

    def to_json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for result_field in self.fields:
            types = _ordered_kinds(result_field.kinds)
            properties[result_field.name] = {"type": types[0] if len(types) == 1 else types}
        return {"type": "array", "items": {"type": "object", "properties": properties}}


@dataclass(slots=True)
class _FieldAccumulator:
    kinds: set[str] = field(default_factory=set)
    presence_count: int = 0
    has_int64_overflow: bool = False
    has_nonfinite_number: bool = False


def build_catalog(rows: list[dict[str, Any]], *, guard: AdmissionGuard) -> ResultFieldCatalog:
    """Validate and profile every top-level key in *rows*.

    Direct callers receive the same guard as the reducer.  The reducer uses
    :func:`_build_catalog_from_admitted` after it has already bounded the
    complete captured payload before threshold serialization.
    """
    guard.require(rows)
    return _build_catalog_from_admitted(rows)


def _build_catalog_from_admitted(rows: list[dict[str, Any]]) -> ResultFieldCatalog:
    """Profile rows whose raw captured graph already passed admission."""
    accumulators: dict[str, _FieldAccumulator] = {}
    row_count = len(rows)
    for row in rows:
        for name, value in row.items():
            accumulator = accumulators.setdefault(name, _FieldAccumulator())
            accumulator.presence_count += 1
            kind = _kind(value)
            accumulator.kinds.add(kind)
            if kind == "integer" and not _is_int64(value):
                accumulator.has_int64_overflow = True
            if kind == "number" and not math.isfinite(value):
                accumulator.has_nonfinite_number = True

    fields = tuple(
        _freeze_field(name, accumulators[name], row_count, index)
        for index, name in enumerate(sorted(accumulators))
    )
    return ResultFieldCatalog(fields=fields)


def _freeze_field(
    name: str, accumulator: _FieldAccumulator, row_count: int, index: int
) -> ResultField:
    kinds = frozenset(accumulator.kinds)
    nonnull_kinds = kinds - {"null"}
    nullable = "null" in kinds or accumulator.presence_count != row_count
    reason: str | None = None
    scalar_family: str | None = None
    if not nonnull_kinds:
        reason = "null_only"
    elif len(nonnull_kinds) != 1:
        reason = "mixed_non_null_kinds"
    else:
        candidate_family = next(iter(nonnull_kinds))
        if candidate_family not in _SAFE_SCALAR_KINDS:
            reason = "container_value"
        else:
            scalar_family = candidate_family
        if accumulator.has_int64_overflow:
            reason = "integer_out_of_int64_range"
        elif accumulator.has_nonfinite_number:
            reason = "non_finite_number"

    return ResultField(
        name=name,
        column=f"_rr_v_{index:04d}",
        kinds=kinds,
        nullable=nullable,
        queryable=reason is None,
        reason=reason,
        scalar_family=scalar_family,
    )


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _is_int64(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and _INT64_MIN <= value <= _INT64_MAX
    )


def _ordered_kinds(kinds: frozenset[str]) -> list[str]:
    return sorted(kinds, key=_KIND_ORDER.__getitem__)

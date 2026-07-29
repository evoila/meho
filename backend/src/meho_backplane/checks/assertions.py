# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Frozen spec models for the bounded typed assertion evaluator (#2504).

Initiative #2416 (parent goal #221) draws its check layer as exactly two
bounded stages -- **select** (a dotted path plus at most one bounded
aggregate) then a **typed comparator** -- with no free-form assertion
language. This module lands the spec schemas, the compiled-path grammar,
the set-wide five-state vocabulary, and the outcome shape. The evaluator
itself lives in :mod:`meho_backplane.checks.evaluate`; both modules are
kept dependency-pure (stdlib + pydantic + this package only) so #2503 can
later add DB-facing siblings (``schemas.py`` / ``repository.py`` /
``service.py``) to the same package without dragging I/O into the
evaluator.

Design lineage
==============

* The discriminated-union mould is the runbook verify gate
  (:class:`meho_backplane.runbooks.schemas.OperationCallVerify`, #1301):
  frozen models, a ``type`` :class:`~typing.Literal` tag per member, and a
  ``Field(discriminator="type")`` union that routes on the tag and rejects
  an unknown tag with a clean validation error. That gate deliberately
  shipped "no operators, no JSONPath, and no boolean composition" -- the
  same substrate-minimalism bar this task holds.
* The dotted-path grammar is an intentionally small subset -- no JSONPath
  dependency is in the tree, and ``jsonpath-ng`` would ship recursive
  descent, filters, and arithmetic that #2416's out-of-scope bans. The
  in-house ``_extract_path`` navigator in the jsonflux engine already
  proves a subset walker is a handful of lines; this is its strict,
  pre-validated cousin. The jsonflux SQL engine itself is explicitly *not*
  reused.

Nothing here performs I/O; :func:`parse_path` and every model validator
are pure.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AssertionOutcome",
    "AssertionSpec",
    "BoolCompare",
    "CheckState",
    "Compare",
    "EqualsCompare",
    "FreshnessCompare",
    "InCompare",
    "PathSegment",
    "Scalar",
    "SelectSpec",
    "ThresholdCompare",
    "parse_path",
]

#: The set-wide check-state vocabulary, **declared once here**. ``degraded``
#: (not ``warn``) is the middle state so a single word serves the state, the
#: per-Sensor ``severity`` values #2503 pins (``degraded``/``critical``), and
#: the Initiative's ``UNKNOWN -> degraded`` rollup cap. #2505/#2506 import
#: this type; nobody re-declares it. The evaluator emits only the first four
#: -- ``skip`` is a scheduling-time fact a pure function cannot observe
#: (#2506 derives it for paused sensors).
CheckState = Literal["ok", "degraded", "critical", "unknown", "skip"]

#: A JSON scalar. The operand type for the equality comparators and the type
#: of a post-select / post-aggregate observed value. ``bool`` is listed
#: explicitly and kept distinct from ``int`` at compare time (see
#: :mod:`meho_backplane.checks.evaluate`) to neutralize Python's
#: ``True == 1`` footgun.
Scalar = str | int | float | bool | None


class PathSegment(NamedTuple):
    """One compiled step of a select path.

    ``kind`` discriminates the three grammar forms. A ``"key"`` segment
    carries ``key``; an ``"index"`` segment carries ``idx``; a
    ``"wildcard"`` segment carries neither. Produced only by
    :func:`parse_path`, which guarantees the invariant (a ``key`` segment
    always has a non-``None`` ``key``, an ``index`` segment a non-``None``
    ``idx``). ``idx`` is not spelled ``index`` because that name would
    shadow the inherited :meth:`tuple.index` method.
    """

    kind: Literal["key", "index", "wildcard"]
    key: str | None = None
    idx: int | None = None


#: One select-path segment: ``.name`` (a key), ``[int]`` (a list index), or
#: ``[*]`` (a single wildcard projection). Matched against the current offset
#: with :meth:`re.Pattern.match`, so a gap between segments is a grammar error.
_PATH_SEGMENT_RE = re.compile(
    r"\.(?P<key>[A-Za-z0-9_-]+)|\[(?P<index>[0-9]+)\]|\[(?P<wildcard>\*)\]"
)


def parse_path(path: str) -> tuple[PathSegment, ...]:
    """Compile a dotted-path selector into a tuple of :class:`PathSegment`.

    Grammar (bounded on purpose)::

        path    := "$" segment*
        segment := "." name | "[" int "]" | "[*]"
        name    := [A-Za-z0-9_-]+
        int     := [0-9]+

    ``$`` on its own selects the payload root. At most **one** ``[*]``
    wildcard is permitted. Any grammar violation -- a missing leading ``$``,
    an unrecognized segment, a trailing gap, or a second wildcard -- raises
    :class:`ValueError`, which Pydantic surfaces as a 422 when the path is
    validated inside :class:`SelectSpec` (the same 422 #2503 wants for a bad
    Sensor payload at create).
    """
    if not path or path[0] != "$":
        raise ValueError(f"select path must start with '$': {path!r}")

    segments: list[PathSegment] = []
    wildcards = 0
    pos = 1
    while pos < len(path):
        match = _PATH_SEGMENT_RE.match(path, pos)
        if match is None:
            raise ValueError(f"invalid path segment at offset {pos} in {path!r}")
        if (key := match.group("key")) is not None:
            segments.append(PathSegment(kind="key", key=key))
        elif (index := match.group("index")) is not None:
            segments.append(PathSegment(kind="index", idx=int(index)))
        else:
            wildcards += 1
            if wildcards > 1:
                raise ValueError(f"at most one '[*]' wildcard allowed in {path!r}")
            segments.append(PathSegment(kind="wildcard"))
        pos = match.end()
    return tuple(segments)


class SelectSpec(BaseModel):
    """The select stage: a dotted path and at most one bounded aggregate.

    With no aggregate the path must resolve to a scalar; with an aggregate it
    must resolve to a list (either a ``[*]`` projection or a directly selected
    array). The path is compiled at model-parse time so an invalid grammar is
    a 422, not an evaluation-time surprise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    aggregate: Literal["max", "min", "sum", "count", "any", "all"] | None = None

    @field_validator("path")
    @classmethod
    def _compile_path(cls, value: str) -> str:
        parse_path(value)  # raises ValueError -> 422 on any grammar violation
        return value


class ThresholdCompare(BaseModel):
    """Numeric-bound comparator: ``ok`` / ``degraded`` / ``critical``.

    The observed value is tested ``value <op> bound``. ``critical`` is checked
    before ``degraded``, so the more-severe band wins. At least one bound is
    required; when both are set they must be ordered by severity for the
    operator direction (``gt``/``gte``: ``degraded <= critical``; ``lt``/``lte``:
    ``degraded >= critical``). Non-numeric (or boolean) input is ``unknown``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["threshold"]
    op: Literal["gt", "gte", "lt", "lte"]
    degraded: float | None = None
    critical: float | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> ThresholdCompare:
        for name, bound in (("degraded", self.degraded), ("critical", self.critical)):
            if bound is not None and not math.isfinite(bound):
                raise ValueError(f"threshold {name} bound must be finite, got {bound!r}")
        if self.degraded is None and self.critical is None:
            raise ValueError("threshold requires at least one of 'degraded' / 'critical'")
        if self.degraded is not None and self.critical is not None:
            if self.op in ("gt", "gte") and self.degraded > self.critical:
                raise ValueError(
                    f"for op={self.op!r}, degraded ({self.degraded}) must be "
                    f"<= critical ({self.critical})"
                )
            if self.op in ("lt", "lte") and self.degraded < self.critical:
                raise ValueError(
                    f"for op={self.op!r}, degraded ({self.degraded}) must be "
                    f">= critical ({self.critical})"
                )
        return self


class EqualsCompare(BaseModel):
    """Strict-equality comparator: ``ok`` on match, ``critical`` otherwise.

    Equality is type-strict for booleans -- ``True`` never equals ``1`` -- so
    the ``value`` operand keeps whatever JSON scalar type it was given (Pydantic
    smart-union preserves it) and the evaluator compares with matching strictness.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["equals"]
    value: Scalar


class InCompare(BaseModel):
    """Membership comparator: ``ok`` if the value strict-equals a member.

    Same boolean strictness as :class:`EqualsCompare`, applied per member.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["in"]
    values: list[Scalar] = Field(min_length=1)


class BoolCompare(BaseModel):
    """Boolean comparator: the value must be strictly ``bool``.

    A non-boolean observed value is ``unknown`` (no truthiness coercion);
    a boolean that does not match :attr:`expect` is ``critical``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["bool"]
    expect: bool = True


class FreshnessCompare(BaseModel):
    """Age comparator over a timestamp carried in the payload.

    The observed value is the timestamp: an RFC3339 string (which **must**
    carry a timezone offset -- a naive string is ``unknown`` because a
    host-TZ-dependent comparison is banned) or an ``int``/``float`` of epoch
    seconds interpreted as UTC. ``age = now - timestamp``; ``age >
    max_age_seconds`` is ``critical``, ``> degraded_age_seconds`` (when set)
    is ``degraded``, otherwise ``ok`` (a future timestamp is ``ok``). ``now``
    is injected by the caller -- the op-result envelope carries no timestamp
    field of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["freshness"]
    max_age_seconds: float = Field(gt=0)
    degraded_age_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_ages(self) -> FreshnessCompare:
        if (
            self.degraded_age_seconds is not None
            and self.degraded_age_seconds >= self.max_age_seconds
        ):
            raise ValueError(
                f"degraded_age_seconds ({self.degraded_age_seconds}) must be "
                f"< max_age_seconds ({self.max_age_seconds})"
            )
        return self


#: A comparator, discriminated on ``type``. Pydantic routes a payload to the
#: matching member by the tag and surfaces an unknown tag as a clean
#: validation error -- no silent fall-through to the first member. Exactly five
#: members, mirroring the runbook verify union (#1301).
Compare = Annotated[
    ThresholdCompare | EqualsCompare | InCompare | BoolCompare | FreshnessCompare,
    Field(discriminator="type"),
]


class AssertionSpec(BaseModel):
    """A full assertion: one select stage feeding one typed comparator.

    #2503 imports this model to validate a Sensor's ``assertion`` payload at
    the wire (a bad path or comparator is a 422 at create); #2505 feeds it the
    op-result payload; #2506 rolls the outcomes up into a Dashboard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    select: SelectSpec
    compare: Compare


class AssertionOutcome(BaseModel):
    """The evaluator verdict.

    :attr:`state` is one of the four emittable states (``ok`` / ``degraded`` /
    ``critical`` / ``unknown``); the evaluator never emits ``skip``.
    :attr:`value` is the observed scalar the comparator judged (the aggregate
    result when an aggregate was applied), or ``None`` when the outcome is
    ``unknown``. :attr:`evidence` is a JSON-serializable dict carrying at least
    ``path``, ``aggregate``, ``comparator``, ``observed``, the comparator's
    expected bounds/values, and ``reason`` when ``state == "unknown"``. #2505
    persists the evidence; #2506 renders it.
    """

    model_config = ConfigDict(frozen=True)

    state: CheckState
    value: Scalar
    evidence: dict[str, Any]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Eval corpus loader + per-surface Pydantic schemas.

G4.3-T1 (#440). The :func:`load_corpus` entry point reads one of the
three per-surface YAML files (``kb_queries.yaml`` /
``memory_queries.yaml`` / ``operation_queries.yaml``) co-located with
this module, validates every entry against its frozen Pydantic v2
schema, and returns the typed list. Validation failures raise
:class:`CorpusValidationError` (a loud failure surfacing the bad
entry) — a broken corpus must not silently run an eval that reports
spuriously perfect numbers.

T1 ships the kb seed corpus only. The memory + operations YAML files
land in T4 (#443) / T3 (#442); :func:`load_corpus` returns ``[]`` for
those surfaces until the corresponding file lands so T2's eval runner
can iterate every surface without crashing.

Schemas are **frozen + extra=forbid + strict** (Pydantic v2). Frozen
keeps the loaded corpus immutable for the run lifetime — a regression
where an eval pass quietly rewrote ``expected_hits`` mid-run is
impossible by construction. ``extra=forbid`` rejects YAML keys that
look right but aren't (e.g. ``expected_slug`` instead of
``expected_hits`` — a copy-paste from the issue body would otherwise
silently ignore the field). ``strict`` blocks the YAML-typing footgun
where a bare ``yes`` or unquoted slug becomes a ``bool``.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal, overload

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

__all__ = [
    "CorpusValidationError",
    "KbCorpusQuery",
    "MemoryCorpusQuery",
    "OperationCorpusQuery",
    "load_corpus",
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# Shared model config: immutable after load, no unknown fields, no silent
# coercion. Reused by every per-surface schema so the discipline can't
# drift between surfaces.
_CORPUS_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)


class KbCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``kb`` retrieval surface.

    Attributes
    ----------
    query
        The natural-language phrasing an operator would type. Sourced
        from operator Slack history + recent Claude session logs (see
        per-entry ``notes`` for the originating context).
    expected_hits
        Ordered list of ``kb`` slugs (without the ``.md`` suffix) the
        operator would consider a correct hit. The first entry is the
        ideal top-1; subsequent entries are acceptable top-N
        alternatives. Order matters for MRR scoring (T2's runner).
    notes
        Free-form rationale — operator who phrased the query, the
        reason this slug ranks first, anything that informs human
        review on regression. Optional; informational only.
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_hits: list[str]
    notes: str | None = None


class MemoryCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``memory`` retrieval surface.

    Mirror of :class:`KbCorpusQuery` for the G5 memory layer — each
    expected hit is a ``(scope, slug)`` pair because the same slug can
    legitimately exist under different scopes (``user`` vs
    ``user-tenant`` vs ``tenant`` etc.) and the eval cares which one
    surfaced. The corpus YAML lands in T4 (#443).
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_hits: list[tuple[str, str]]
    notes: str | None = None


class OperationCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``operations`` retrieval surface.

    Captures the agent-facing ``search_operations`` UX: a free-text
    query, the connector implementation it should resolve to, and the
    set of acceptable ``op_id`` values. ``govc_equivalent`` is the
    pre-MEHO operator workflow (the consumer's existing CLI) — the
    baseline T2's runner compares MEHO ranking against. The corpus
    YAML lands in T3 (#442).
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_connector_id: str
    expected_op_ids: list[str]
    govc_equivalent: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class CorpusValidationError(Exception):
    """Raised when a corpus YAML file is malformed or schema-mismatched.

    The message names the surface, the file path that was loaded, and
    the underlying parse / validation error. Operators running
    ``meho retrieval eval`` (T2) see the message verbatim; the loud
    failure is intentional — a corpus that loads with garbage entries
    would produce eval numbers that look authoritative but reflect
    nothing about retrieval quality.
    """


# Per-surface adapters — built once at import time so each call to
# ``load_corpus`` reuses the cached schema. ``TypeAdapter`` is the
# Pydantic v2 escape hatch for validating non-``BaseModel`` shapes
# (here, ``list[T]``); it picks up the model's frozen / extra=forbid
# / strict configuration without needing a wrapper class.
_KB_ADAPTER = TypeAdapter(list[KbCorpusQuery])
_MEMORY_ADAPTER = TypeAdapter(list[MemoryCorpusQuery])
_OPERATION_ADAPTER = TypeAdapter(list[OperationCorpusQuery])


# Per-surface YAML filenames inside this package. Resolved relative to
# the package via ``importlib.resources`` so the loader works in both
# editable installs (source tree) and built wheels (where the YAML is
# packaged as resource data).
_CORPUS_FILES: dict[str, str] = {
    "kb": "kb_queries.yaml",
    "memory": "memory_queries.yaml",
    "operations": "operation_queries.yaml",
}


@overload
def load_corpus(surface: Literal["kb"]) -> list[KbCorpusQuery]: ...
@overload
def load_corpus(surface: Literal["memory"]) -> list[MemoryCorpusQuery]: ...
@overload
def load_corpus(surface: Literal["operations"]) -> list[OperationCorpusQuery]: ...


def load_corpus(
    surface: Literal["kb", "memory", "operations"],
) -> list[KbCorpusQuery] | list[MemoryCorpusQuery] | list[OperationCorpusQuery]:
    """Load + validate the YAML eval corpus for *surface*.

    Returns the validated list of typed query rows. The order of the
    returned list mirrors the YAML file order — eval runners that need
    deterministic ordering for diff-friendly output get it for free.

    A missing YAML file for the ``memory`` or ``operations`` surface
    returns ``[]`` (the corpora land in T4 / T3 respectively); a
    missing ``kb`` file is a hard error since this Task ships it.
    Any file present must parse + validate, regardless of surface — a
    half-baked memory corpus that fails validation must surface, not
    silently return ``[]``.

    Parameters
    ----------
    surface
        One of ``"kb"`` / ``"memory"`` / ``"operations"``.

    Returns
    -------
    list[KbCorpusQuery] | list[MemoryCorpusQuery] | list[OperationCorpusQuery]
        Typed list whose element type matches *surface*.

    Raises
    ------
    CorpusValidationError
        Raised when the YAML file is present but malformed, when its
        top-level value is not a list, or when any entry fails
        Pydantic validation. The message names the surface, file
        path, and underlying error.
    """
    filename = _CORPUS_FILES[surface]
    resource = resources.files(__package__).joinpath(filename)

    if not resource.is_file():
        if surface == "kb":
            # The kb corpus is mandatory in T1 — its absence is a
            # packaging error (the YAML failed to ship with the
            # wheel) or a developer who deleted it locally without
            # recreating it.
            raise CorpusValidationError(
                f"kb corpus YAML missing: expected {filename} alongside "
                f"{__package__}; T1 (#440) must ship this file"
            )
        return []

    raw = resource.read_text(encoding="utf-8")

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CorpusValidationError(
            f"{surface} corpus YAML failed to parse ({filename}): {exc}"
        ) from exc

    # Empty file (``parsed is None``) is treated as "no entries" rather
    # than a parse error — useful when stubbing a surface during an
    # in-flight migration. Schema validation still rejects entries that
    # are present-but-malformed.
    if parsed is None:
        return []

    if not isinstance(parsed, list):
        raise CorpusValidationError(
            f"{surface} corpus YAML must be a top-level list ({filename}); "
            f"got {type(parsed).__name__}"
        )

    try:
        if surface == "kb":
            return _KB_ADAPTER.validate_python(parsed)
        if surface == "memory":
            return _MEMORY_ADAPTER.validate_python(parsed)
        return _OPERATION_ADAPTER.validate_python(parsed)
    except ValidationError as exc:
        # Pydantic's default message names the failing field path
        # (e.g. ``2.expected_hits.0``) which is what an operator
        # editing the YAML needs to find the bad entry.
        raise CorpusValidationError(
            f"{surface} corpus YAML failed validation ({filename}):\n{exc}"
        ) from exc

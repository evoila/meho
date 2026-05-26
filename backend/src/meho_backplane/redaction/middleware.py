# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-boundary redaction middleware -- Initiative #805 (C1-b, #1071).

The dispatcher (:mod:`meho_backplane.operations.dispatcher`) invokes
:func:`apply_connector_boundary_redaction` once per dispatch, between
"connector returned a raw response" and "JSONFlux reducer consumes it":

.. code-block:: text

       handler returns raw payload
                   │
                   ▼
       capture raw  ─────────►  audit row stores raw verbatim
                   │
                   ▼
       resolver picks RedactionPolicy
                   │
                   ▼
       engine.redact(raw, policy, ...)
                   │
                   ▼
       (redacted, manifest)
            │           │
            ▼           ▼
       JSONFlux       audit row stores manifest
       reducer
            │
            ▼
       OperationResult to caller

The middleware is the **only** path that wires
:mod:`meho_backplane.redaction.resolver` to
:mod:`meho_backplane.redaction.engine`; the dispatcher calls it once
with the call's labels and the raw payload and gets back the redacted
payload plus the manifest the audit row needs to persist.

Why a thin wrapper around resolver + engine? Three reasons:

1. The dispatcher should not import the engine and resolver directly --
   it would couple the dispatcher to the redaction package's internal
   shape. The middleware is the public seam.
2. The wrapper is the unit-test boundary: redaction tests exercise
   :func:`apply_connector_boundary_redaction` end-to-end without
   spinning up a real dispatcher.
3. The wrapper centralises the "capture-raw-as-JSON-shape" contract so
   the audit row never stores a raw object the JSON encoder cannot
   serialise. Pydantic / dataclass / set / tuple payloads get
   normalised here so the audit-write path stays a dumb insert.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from meho_backplane.redaction.engine import RedactionManifestEntry, redact
from meho_backplane.redaction.resolver import resolve_policy

__all__ = [
    "RedactionMiddlewareResult",
    "apply_connector_boundary_redaction",
    "manifest_to_audit_payload",
    "normalize_for_audit",
]


class RedactionMiddlewareResult(BaseModel):
    """The middleware's return value.

    Carries the four artefacts the dispatcher needs to fan out into
    the reducer call, the audit row, and the broadcast event:

    * ``raw`` -- the JSON-normalised raw payload (what the audit row
      stores under ``payload['raw']``). Same shape as the engine's
      input, never a Pydantic model or arbitrary object.
    * ``redacted`` -- the engine's output, the value the JSONFlux
      reducer consumes and the caller eventually sees.
    * ``manifest`` -- the tuple of :class:`RedactionManifestEntry`
      records describing every rule firing.
    * ``policy_id`` -- the resolved policy's ``id`` field. Lands on
      the audit row so the audit replay can re-run the same policy
      on the same raw input (C1-d round-trip CI gate).
    """

    model_config = {"frozen": True, "extra": "forbid", "arbitrary_types_allowed": True}

    raw: Any
    redacted: Any
    manifest: tuple[RedactionManifestEntry, ...]
    policy_id: str


def apply_connector_boundary_redaction(
    raw: Any,
    *,
    connector_id: str | None,
    tenant: str | None,
    op: str | None,
) -> RedactionMiddlewareResult:
    """Resolve the policy + apply the engine to *raw*.

    The dispatcher calls this once per dispatch, after the handler
    returns and before the JSONFlux reducer runs. The labels are the
    call's connector id, the operator's tenant id (string form), and
    the descriptor's op_id; the resolver uses them to pick the most
    specific :class:`RedactionPolicy`, falling through to a
    conservative default when no override matches.

    *raw* is normalised to JSON-shaped containers via
    :func:`normalize_for_audit` before the engine sees it -- a
    Pydantic model returned by a handler flattens to a dict, a tuple
    flattens to a list, and so on. This keeps the audit row's
    ``raw`` field encoder-safe and lets the engine's walking
    strategy (Mapping / Sequence / str) cover handler returns
    without per-shape branching at the call site.

    The middleware is **pure with respect to redaction logic** -- it
    delegates to :func:`resolve_policy` and :func:`redact`, neither
    of which performs I/O. The single side-effecting hop is the
    policy-cache warm-up inside
    :func:`meho_backplane.redaction.resolver.get_default_policy`,
    which is idempotent and lock-protected. A second call with the
    same labels returns the same policy reference, so the audit row's
    ``policy_id`` is stable across dispatches.
    """
    policy = resolve_policy(connector_id=connector_id, tenant=tenant, op=op)
    normalized = normalize_for_audit(raw)
    result = redact(
        normalized,
        policy,
        connector_id=connector_id,
        tenant=tenant,
        op=op,
    )
    return RedactionMiddlewareResult(
        raw=normalized,
        redacted=result.redacted,
        manifest=result.manifest,
        policy_id=policy.id,
    )


def normalize_for_audit(value: Any) -> Any:
    """Coerce *value* to a JSON-encoder-safe shape.

    The audit row's ``payload`` column stores JSON; an unencoded
    Pydantic model or set raised by a connector handler would crash
    the insert. The redactor would also stumble on non-Mapping /
    non-Sequence containers. This helper produces the same shape
    in one place so the engine, the audit row, and the broadcast
    event all consume identical structure.

    Rules, applied recursively:

    * :class:`BaseModel` -> :meth:`BaseModel.model_dump` (dict). The
      ``mode='json'`` flag is omitted intentionally -- the inner
      walk handles datetime / UUID via :func:`_normalize_scalar`,
      keeping the encoder choice in this module rather than
      delegating to Pydantic's mode-specific serialisers (which
      stringify identifiers in ways that vary between v1 and v2).
    * :class:`Mapping` -> ``dict`` with string keys (non-string keys
      get :func:`str`-coerced; the audit row JSON cannot represent
      non-string keys anyway).
    * :class:`Sequence` other than ``str`` / ``bytes`` /
      ``bytearray`` -> ``list``. Tuples and sets thus flatten to
      lists; this loses ordering information for sets but the
      audit row's purpose is post-hoc inspection, not exact
      replay of the in-memory type.
    * :class:`bytes` / :class:`bytearray` -> hex string. The
      JSONFlux boundary upstream produces JSON-shaped values, so
      binary at the redaction boundary is uncommon; surfacing the
      hex preserves the audit record's debug value without forcing
      a base64 dialect into the audit schema.
    * Anything else (numbers, booleans, None, strings) passes
      through verbatim. The engine's :func:`redact` walks the
      result and only mutates string leaves.

    Cycles in the input raise :class:`RecursionError` rather than
    silently truncating -- a cyclic connector response is itself a
    bug worth surfacing.
    """
    return _normalize(value)


def manifest_to_audit_payload(
    manifest: tuple[RedactionManifestEntry, ...],
) -> list[dict[str, Any]]:
    """Convert the engine's manifest to a JSON-shaped list of dicts.

    The audit row stores this under ``payload['redaction_manifest']``;
    a separate dedicated column would be marginal value over the
    existing JSON column and would require a wider migration. Each
    entry is a flat dict with the rule / pattern / action / count /
    span / reason / path fields -- the same surface
    :class:`RedactionManifestEntry` exposes, serialised so the audit
    insert encoder accepts it without per-row Pydantic round-tripping.
    """
    return [entry.model_dump(mode="json") for entry in manifest]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize(value: Any) -> Any:
    """Recursive worker for :func:`normalize_for_audit`."""
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump())
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (bytes, bytearray)):
        # Hex (not base64) so an operator reading the audit row sees
        # something obviously non-string; base64 collides with the
        # opaque-token shapes the engine's named patterns target and
        # would trigger spurious matches at the redaction step.
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return [_normalize(item) for item in value]
    if isinstance(value, set | frozenset):
        # Sets become lists; ordering is lost but the audit row's
        # purpose is post-hoc inspection, not exact-shape replay.
        return [_normalize(item) for item in value]
    # Numbers, booleans, None, datetime, UUID, Decimal -- the audit
    # row's JSON encoder (orjson via SQLAlchemy's JSON type) handles
    # the standard non-string scalars; nothing for the engine to do
    # with them either (regex against numerics is nonsensical).
    return value

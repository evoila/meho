# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Declarative per-product ingest op allowlist enforcement (T3-F01).

Some connectors have a design invariant that their op set is **closed** to a
fixed list of operations — the ``mehoauto`` add-on, for instance, is closed to
exactly launch / validate / gate even though its published ``/openapi.json``
advertises ~30 mutating routes (tenant / site / adoption / run ``DELETE``\\s,
fleet import, blueprint / environment / deployment CRUD). The connector-owned
safety floor (:mod:`meho_backplane.connectors.meho_automation.ingest_safety`)
only pins *tiers* on the three known ops and passes every other op through to
the generic verb heuristic; nothing there stops the other ~27 ops from being
**persisted**, and :func:`enable_connector` then cascades ``is_enabled=True``
onto every ingested row. The invariant was documented but not enforced.

This module enforces it in code, declared in data. A catalog product MAY
declare an :attr:`~meho_backplane.operations.ingest.catalog.ConnectorSpecEntry.op_allowlist`
(a list of ``(method, path)`` pairs). :func:`apply_op_allowlist` is called on
the ingest path **before persistence**, at the same layer the safety floor is
applied (``register_ingested`` for the real register path,
``pipeline._run_dry_run`` for the preview). Operations not on the allowlist
are dropped — they are never persisted and never staged, so the
``enable_connector`` cascade cannot reach them. Absence of an allowlist keeps
the historical behaviour (persist every parsed op). The floor mechanism is
untouched: the kept ops still flow through :func:`apply_safety_floor`.

The dropped ``(method, path)`` list is returned so the caller can surface it
in the ingest result / report and emit one structured log line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meho_backplane.operations.ingest.catalog import op_allowlist_for
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = ["DroppedOp", "OpAllowlistResult", "apply_op_allowlist"]


@dataclass(frozen=True, slots=True)
class DroppedOp:
    """One parsed operation dropped by the allowlist before persistence."""

    method: str
    path: str


@dataclass(frozen=True, slots=True)
class OpAllowlistResult:
    """The kept operations plus the ``(method, path)`` pairs that were dropped."""

    kept: tuple[EndpointDescriptorProto, ...]
    dropped: tuple[DroppedOp, ...]


def _proto_key(proto: EndpointDescriptorProto) -> tuple[str, str]:
    """Normalise a proto to the ``(METHOD, path)`` key the allowlist matches.

    Mirrors the connector-owned safety floor's keying
    (:mod:`~meho_backplane.connectors.meho_automation.ingest_safety`) exactly
    — method upper-cased, any query string stripped from the path — so an op
    curated by the floor and admitted by the allowlist agree on the same key.
    """
    return (proto.method.upper(), proto.path.split("?", 1)[0])


def apply_op_allowlist(
    *,
    product: str,
    version: str,
    operations: Sequence[EndpointDescriptorProto],
) -> OpAllowlistResult:
    """Filter parsed operations to a product's declared ingest allowlist.

    When ``(product, version)`` declares no allowlist (the common case),
    every operation is kept and ``dropped`` is empty — the historical
    behaviour. When an allowlist is declared, only operations whose
    ``(method, path)`` matches an allowlist entry are kept; the rest are
    dropped (never persisted, never staged).
    """
    allow = op_allowlist_for(product, version)
    if allow is None:
        return OpAllowlistResult(kept=tuple(operations), dropped=())
    kept: list[EndpointDescriptorProto] = []
    dropped: list[DroppedOp] = []
    for proto in operations:
        method, path = _proto_key(proto)
        if (method, path) in allow:
            kept.append(proto)
        else:
            dropped.append(DroppedOp(method=method, path=path))
    return OpAllowlistResult(kept=tuple(kept), dropped=tuple(dropped))

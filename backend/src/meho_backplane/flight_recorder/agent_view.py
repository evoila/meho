# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The copy of a stored trace that an agent login reads.

A person reads a stored trace as it is (the console, ``meho audit trace``
and ``GET /api/v1/audit/{id}/trace``). A service login reads the stored
traces of its own calls as they are too. An agent login reads a copy: the
trace handle on a ``call_operation`` answer
(:mod:`~meho_backplane.flight_recorder.agent_read`) and the two REST trace
routes give an agent this copy.

In the copy, each span of a Kubernetes call (connector product ``k8s``)
goes through
:func:`~meho_backplane.connectors.kubernetes.trace_agent_view.agent_view`:
the ``env`` values that the secret rules of the direct answer hide, the
secret values on command lines and in ``sh -c`` scripts, and secret values
in annotations are hidden. Plain values and ``managedFields`` stay. The
spans of every other connector are copied unchanged.

The walk takes time in line with the size of the trace. When a trace holds
a Kubernetes span, the walk runs in a worker thread
(:func:`asyncio.to_thread`), so a large trace does not hold up other
requests on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.flight_recorder.read import TraceView

__all__ = ["agent_view_attributes", "agent_view_rows", "agent_view_trace"]

#: The product part of a Kubernetes ``connector_id`` (``k8s``, ``k8s-1.x``)
#: and the prefix of its operation ids (``k8s.pod.list``).
_KUBERNETES_PRODUCT: Final[str] = "k8s"


def _is_kubernetes_span(attributes: Mapping[str, Any]) -> bool:
    """True when the span belongs to a call of a Kubernetes connector."""
    connector_id = attributes.get("connector_id")
    if isinstance(connector_id, str) and (
        connector_id == _KUBERNETES_PRODUCT or connector_id.startswith(f"{_KUBERNETES_PRODUCT}-")
    ):
        return True
    op_id = attributes.get("op_id")
    return isinstance(op_id, str) and op_id.startswith(f"{_KUBERNETES_PRODUCT}.")


def agent_view_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """The span attributes an agent reads: cleaned for a Kubernetes span, else a copy."""
    if not _is_kubernetes_span(attributes):
        return dict(attributes)
    from meho_backplane.connectors.kubernetes.trace_agent_view import agent_view

    cleaned = agent_view(dict(attributes))
    return cleaned if isinstance(cleaned, dict) else {}


def _clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**row, "attributes": agent_view_attributes(row.get("attributes") or {})} for row in rows
    ]


def _needs_cleaning(attribute_maps: list[Mapping[str, Any]]) -> bool:
    return any(_is_kubernetes_span(attributes) for attributes in attribute_maps)


async def agent_view_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agent's copy of span rows (dicts with an ``attributes`` mapping).

    Returns *rows* unchanged when no row is a Kubernetes span. Else the walk
    runs in a worker thread. Raises when the walk fails; the caller then
    withholds the trace.
    """
    if not _needs_cleaning([row.get("attributes") or {} for row in rows]):
        return rows
    return await asyncio.to_thread(_clean_rows, rows)


def _clean_trace(view: TraceView) -> TraceView:
    spans = [
        span.model_copy(update={"attributes": agent_view_attributes(span.attributes)})
        for span in view.spans
    ]
    return view.model_copy(update={"spans": spans})


async def agent_view_trace(view: TraceView) -> TraceView:
    """The agent's copy of a :class:`TraceView`; see :func:`agent_view_rows`."""
    if not _needs_cleaning([span.attributes for span in view.spans]):
        return view
    return await asyncio.to_thread(_clean_trace, view)

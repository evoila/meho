# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared request-shape building blocks for K8s list ops.

G0.17-T1 (#1330) factored the workload list ops'
``_LIST_BASE_PROPERTIES`` + ``_NAMESPACE_XOR_ALL_NAMESPACES`` out of
:mod:`ops_workload` so every namespaced list op in the connector
(`k8s.pod.list`, `k8s.deployment.list`, `k8s.event.list`,
`k8s.service.list`, `k8s.ingress.list`, `k8s.configmap.list`) shares
one canonical request shape.

See ``docs/codebase/api-shape-conventions.md`` §10 (intra-connector
list-op request-shape parity) for the convention this module anchors:
sibling list operations over the same kind of scoped resource on one
connector share one input-parameter shape, the shared shape lives in
one place, and individual ops document deliberate omissions
(e.g. ``k8s.event.list`` omits ``continue_token`` because its
client-side recency-sort + truncation contract supersedes server-side
paging -- documented in
:data:`~meho_backplane.connectors.kubernetes.ops_events.K8S_EVENT_LIST_PAGINATION_HINT`).

Three building blocks live here:

* :data:`NAMESPACE_PARAM` / :data:`ALL_NAMESPACES_PARAM` --
  the two namespace selectors as standalone property schemas. An op
  that needs only a subset of the full base (e.g. ``k8s.event.list``
  intentionally omits ``continue_token``) imports the individual
  blocks and assembles its own ``properties`` dict.
* :data:`LABEL_SELECTOR_PARAM` / :data:`FIELD_SELECTOR_PARAM` /
  :data:`LIMIT_PARAM` / :data:`CONTINUE_TOKEN_PARAM` -- the standard
  k8s filter / paging knobs as standalone property schemas. Same
  cherry-pick pattern as above.
* :data:`LIST_BASE_PROPERTIES` -- the full six-key properties dict
  that ``k8s.pod.list`` / ``k8s.deployment.list`` use as-is. Ops that
  want the full shape import this directly.
* :data:`NAMESPACE_XOR_ALL_NAMESPACES` -- the ``oneOf`` clause that
  enforces "exactly one of ``{namespace, all_namespaces=true}``".
  Imported verbatim by every list op (the XOR semantics are uniform
  across the family).

Authoring contract for a new k8s list op:

1. **Has a per-namespace API + a `for_all_namespaces` API in
   ``kubernetes_asyncio``** -> compose its parameter schema by spread-
   importing :data:`LIST_BASE_PROPERTIES` (or by cherry-picking the
   subset that applies) and the :data:`NAMESPACE_XOR_ALL_NAMESPACES`
   ``oneOf``. Never copy-paste the property dicts.
2. **Has only a per-namespace API** -> omit ``all_namespaces`` and
   document the omission in the op's docstring (no upstream support
   = a vendor constraint, which §10's parity rule explicitly
   acknowledges).
3. **Is genuinely cluster-scoped** (`k8s.namespace.list`,
   ``k8s.node.list``) -> no ``namespace`` / ``all_namespaces`` axis;
   neither block applies.

Mutability note: every constant in this module is a dict / list and
Python does not freeze them, but the wider connector relies on the
treat-as-immutable convention (compare to ``KUBERNETES_OPS`` -- a
``tuple`` precisely because the operator descriptor list is shared
across many readers). Treat these constants the same way; do not
mutate them in tests or callers.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_NAMESPACES_PARAM",
    "CONTINUE_TOKEN_PARAM",
    "FIELD_SELECTOR_PARAM",
    "LABEL_SELECTOR_PARAM",
    "LIMIT_PARAM",
    "LIST_BASE_PROPERTIES",
    "NAMESPACE_PARAM",
    "NAMESPACE_XOR_ALL_NAMESPACES",
]


#: Per-namespace selector. Required unless ``all_namespaces`` is true;
#: the XOR clause in :data:`NAMESPACE_XOR_ALL_NAMESPACES` enforces
#: exactly-one-of {namespace, all_namespaces=true}.
NAMESPACE_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Namespace to list in. Required unless ``all_namespaces`` is true.",
}


#: Cluster-wide flag. ``true`` routes the handler to the
#: ``list_X_for_all_namespaces`` API. Mutually exclusive with
#: ``namespace`` via :data:`NAMESPACE_XOR_ALL_NAMESPACES`.
ALL_NAMESPACES_PARAM: dict[str, Any] = {
    "type": "boolean",
    "default": False,
    "description": (
        "List across every namespace the kubeconfig's service "
        "account can read. Mutually exclusive with ``namespace``."
    ),
}


#: Standard k8s label-selector string, forwarded verbatim to the API.
#: The K8s server applies it before paging; combine with
#: ``all_namespaces=true`` for cluster-wide narrowed listings.
LABEL_SELECTOR_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Standard k8s label selector (e.g. ``app=argocd-server``, "
        "``app in (frontend,backend)``). Forwarded server-side."
    ),
}


#: Standard k8s field-selector string, forwarded verbatim.
FIELD_SELECTOR_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Standard k8s field selector (e.g. ``status.phase=Running``, "
        "``spec.nodeName=node-1``). Forwarded server-side."
    ),
}


#: Server-side page size. Capped at 1000 to bound per-call payload --
#: above that the operator is better served by a tighter selector than
#: by a bigger result set.
LIMIT_PARAM: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 1000,
    "description": (
        "Server-side ``?limit=`` for paginated reads. Combine with "
        "``continue_token`` from a prior response's ``next_continue`` "
        "field to walk pages. Capped at 1000 to bound per-call "
        "payload."
    ),
}


#: Server-emitted pagination cursor. The connector renames the K8s
#: ``_continue`` cursor to ``continue_token`` on the way out so the
#: operator-facing surface stays kubectl-shaped; the handler renames
#: it back when forwarding to the API.
CONTINUE_TOKEN_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Server-emitted pagination cursor from a prior list call's "
        "``next_continue`` field. Pass it back unchanged to fetch the "
        "next page; passing a stale token (>5..15 min old) returns a "
        "410 ResourceExpired -- restart the list without it."
    ),
}


#: Full canonical ``properties`` dict for a list-op schema. Used as-is
#: by ops that adopt the entire shape (``k8s.pod.list``,
#: ``k8s.deployment.list``). Ops that deliberately omit a knob compose
#: the dict from the individual building blocks above and document the
#: omission in their docstring + pagination-hint.
LIST_BASE_PROPERTIES: dict[str, Any] = {
    "namespace": NAMESPACE_PARAM,
    "all_namespaces": ALL_NAMESPACES_PARAM,
    "label_selector": LABEL_SELECTOR_PARAM,
    "field_selector": FIELD_SELECTOR_PARAM,
    "limit": LIMIT_PARAM,
    "continue_token": CONTINUE_TOKEN_PARAM,
}


#: ``oneOf`` clause: exactly one of {namespace, all_namespaces=true}.
#:
#: The ``not`` branches encode "the other selector is absent" for the
#: namespace branch and "all_namespaces is false-or-absent" for the
#: missing-both branch. Draft 2020-12 evaluates each branch
#: independently; the combined effect is the operator must supply
#: exactly one selector. A schema using this clause MUST also include
#: both ``namespace`` and ``all_namespaces`` in its ``properties`` --
#: the clause references the names but does not declare them.
NAMESPACE_XOR_ALL_NAMESPACES: list[dict[str, Any]] = [
    {
        "required": ["namespace"],
        "not": {"required": ["all_namespaces"]},
    },
    {
        "required": ["namespace", "all_namespaces"],
        "properties": {"all_namespaces": {"const": False}},
    },
    {
        "required": ["all_namespaces"],
        "properties": {"all_namespaces": {"const": True}},
        "not": {"required": ["namespace"]},
    },
]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""proxmox real-spec reconcile lane (#2992) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the proxmox connector dispatches
is served by the pinned ``proxmox-8.4`` API schema on the shelf. Proxmox VE
publishes **no OpenAPI/Swagger** — its only machine-readable API schema is
the proprietary apidoc tree the API viewer serves as ``apidata.js`` (a
``const apiSchema = [ ... ];`` JS wrapper around a JSON array; AGPL-3.0-or-
later, provenance in the shelf's ``MANIFEST.md``). Per the standard doc's
non-OpenAPI clause, the artifact still resolves through ``require_shelf_spec``
(uniform skip semantics) while this lane supplies its own served-set
extraction (:func:`proxmox_served_op_ids`) and feeds ``assert_op_ids_served``.

The schema is a nested tree: each node carries a ``path`` (relative to the
``/api2/json`` REST mount, e.g. ``/nodes/{node}/tasks/{upid}/status``, with
Proxmox's own ``{param}`` template names) and an ``info`` object keyed by the
HTTP methods served there. :func:`proxmox_served_op_ids` walks the tree and
prepends the mount, so a served op_id is the real wire route
(``GET:/api2/json/version``) — the exact string the connector dispatches.

The declared set is **captured from live handler execution** (the #2944
never-a-hardcoded-mirror discipline, the rabbitmq-lane precedent): the
connector builds the task-poll path as an inline f-string in its handler
body, so a recording subclass overrides ``_get_json`` — the GET seam every
fixed read funnels through — and the two hand-coded read ops run against it:

* ``proxmox.about`` → ``GET /api2/json/version`` + ``GET /api2/json/nodes``
  (the fingerprint reads, via :meth:`ProxmoxConnector.fingerprint`).
* ``proxmox.task.status`` → ``GET /api2/json/nodes/{node}/tasks/{upid}/status``
  (captured with the injected placeholder values ``NODE``/``UPID``, matched
  against the served template segment-wise).

The two generic passthrough ops (``proxmox.api.get`` / ``proxmox.api.write``)
take an **agent-supplied** path at call time — no hand-coded vendor path to
reconcile — and are excluded (a guard pins that the excluded set is exactly
those two). The ticket-mint ``POST /api2/json/access/ticket`` does not flow
through ``_get_json`` (it is issued inside ``_ticket_session``), so it is
declared from the live ``_TICKET_PATH`` module constant.

Encoding/template honesty: the captured paths are runtime **literals** while
the schema documents **templates** (``/nodes/{node}/tasks/{upid}/status``).
The comparison resolves a literal against the served templates segment-wise —
a template segment matches exactly one literal segment — the rabbitmq-lane
matcher.

First run against the real shelf (the local pre-verify against the consumer
shelf PR branch, 2026-08-18): **green** — all four hand-coded op_ids served
(605 routes over 398 schema nodes). A red lane here is the guard surfacing a
real finding, not harness noise. Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.proxmox import connector as _connector
from meho_backplane.connectors.proxmox.connector import ProxmoxConnector
from meho_backplane.connectors.proxmox.ops import API_BASE, PROXMOX_READ_OPS
from meho_backplane.connectors.proxmox.ops_write import PROXMOX_WRITE_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "proxmox-8.4"
_SPEC_FILE = "apidata.js"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The Proxmox REST mount the api2 schema tree is served under. The apidoc
#: ``path`` fields are relative to it; the real wire route is this + path.
#: Locked to the connector's own ``API_BASE`` by
#: :func:`test_api_mount_matches_the_connector_base`.
_API_MOUNT = "/api2/json"

#: The generic passthrough ops — their path is agent-supplied at call time,
#: so there is no hand-coded vendor path to reconcile. Excluded from the
#: capture; the exact exclusion is pinned by
#: :func:`test_passthrough_ops_are_the_only_uncaptured_ops`.
_PASSTHROUGH_OP_IDS = frozenset({"proxmox.api.get", "proxmox.api.write"})

#: Per-op params for the capture. ``proxmox.about`` needs none; the task poll
#: needs a node + upid, injected as visibly-placeholder literals that
#: template-match the served ``{node}`` / ``{upid}`` segments.
_CAPTURE_PARAMS: dict[str, dict[str, Any]] = {
    "proxmox.task.status": {"node": "NODE", "upid": "UPID"},
}


class _RecordingProxmox(ProxmoxConnector):
    """Records every path ``_get_json`` is asked for; no wire, no auth."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Any,
        params: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator, params
        self.paths.append(path)
        return {}


def _captured_paths_by_op() -> dict[str, set[str]]:
    """Run every hand-coded read op's handler against the recording seam.

    The two passthrough ops are skipped (agent-supplied path); every other
    op runs with its :data:`_CAPTURE_PARAMS` (default empty). The captured
    strings are byte-for-byte the request paths dispatch puts on the wire.
    """

    async def _capture() -> dict[str, set[str]]:
        connector = _RecordingProxmox()
        operator = object()
        target = object()
        captured: dict[str, set[str]] = {}
        for op in (*PROXMOX_READ_OPS, *PROXMOX_WRITE_OPS):
            if op.op_id in _PASSTHROUGH_OP_IDS:
                continue
            handler = getattr(connector, op.handler_attr)
            connector.paths = []
            await handler(operator, target, _CAPTURE_PARAMS.get(op.op_id, {}))
            captured[op.op_id] = set(connector.paths)
        return captured

    return asyncio.run(_capture())


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the proxmox connector dispatches.

    Everything captured funnels through the GET-only ``_get_json`` seam, so
    the captured paths are all ``GET:``; the ticket mint (a ``POST`` issued
    outside that seam) is declared from the live ``_TICKET_PATH`` constant.
    """
    declared = {f"GET:{path}" for paths in _captured_paths_by_op().values() for path in paths}
    declared.add(f"POST:{_connector._TICKET_PATH}")
    return declared


def proxmox_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/api2/json/<path>`` routes from ``apidata.js``.

    Strips the ``const apiSchema = [ ... ];`` JS wrapper, then walks the tree:
    each node contributes ``<method>:<mount><path>`` for every method key in
    its ``info`` object. The ``/api2/json`` mount is prepended so the returned
    strings are the real wire routes (the schema ``path`` fields omit it).
    Templates keep Proxmox's own ``{param}`` segment names.
    """
    text = spec_path.read_text(encoding="utf-8")
    tree = json.loads(text[text.index("[") : text.rindex("]") + 1])
    served: set[str] = set()

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            info = node.get("info") or {}
            path = node.get("path")
            if info and isinstance(path, str):
                served.update(f"{method}:{_API_MOUNT}{path}" for method in info)
            walk(node.get("children") or [])

    walk(tree)
    return served


def _is_served_by_template(op_id: str, template_op_id: str) -> bool:
    """True iff the literal op_id instantiates the template op_id.

    Same method, same segment count, and each template segment either matches
    its literal segment byte-for-byte or is a ``{param}`` placeholder (which
    matches exactly one segment). The injected ``NODE`` / ``UPID`` literals
    therefore match ``{node}`` / ``{upid}`` while a wrong path length or a
    renamed literal segment fails — the defect this lane must catch.
    """
    method, _, path = op_id.partition(":")
    t_method, _, t_path = template_op_id.partition(":")
    if method != t_method:
        return False
    segments = path.split("/")
    t_segments = t_path.split("/")
    if len(segments) != len(t_segments):
        return False
    return all(
        t == s or (t.startswith("{") and t.endswith("}"))
        for s, t in zip(segments, t_segments, strict=True)
    )


def _matched(declared: set[str], served_templates: set[str]) -> set[str]:
    return {
        op_id
        for op_id in declared
        if any(_is_served_by_template(op_id, template) for template in served_templates)
    }


def test_capture_enumerates_the_hand_coded_read_ops() -> None:
    """Guard: the recording seam sees every hand-coded read op's request paths.

    Pins the exact captured path set per op (the #2944 guard shape): a
    handler that grows a new inline path literal, stops funnelling through
    ``_get_json``, or a new op that skips the capture changes this mapping and
    fails here instead of silently shrinking the reconciled set to a vacuous
    pass.
    """
    assert _captured_paths_by_op() == {
        "proxmox.about": {"/api2/json/version", "/api2/json/nodes"},
        "proxmox.task.status": {"/api2/json/nodes/NODE/tasks/UPID/status"},
    }


def test_captured_fingerprint_paths_match_the_module_constants() -> None:
    """Guard: the fingerprint path constants can't drift uncovered.

    ``proxmox.about`` delegates to :meth:`ProxmoxConnector.fingerprint`, which
    reads the module constants ``_VERSION_PATH`` / ``_NODES_PATH``. This pins
    that the captured about-paths are exactly those constants, so an edit to
    either side surfaces here instead of silently dropping a probe path from
    the reconcile.
    """
    assert _captured_paths_by_op()["proxmox.about"] == {
        _connector._VERSION_PATH,
        _connector._NODES_PATH,
    }


def test_passthrough_ops_are_the_only_uncaptured_ops() -> None:
    """Guard: only the two agent-supplied passthroughs are excluded.

    A new hand-coded op that forgets to register capture params (or a
    passthrough that gains a hand-coded path) changes this delta and fails
    here, so the exclusion can never silently drop a real hand-coded path
    from the reconcile.
    """
    all_ops = {op.op_id for op in (*PROXMOX_READ_OPS, *PROXMOX_WRITE_OPS)}
    assert all_ops - set(_captured_paths_by_op()) == _PASSTHROUGH_OP_IDS


def test_api_mount_matches_the_connector_base() -> None:
    """Guard: the served-extraction mount stays locked to the connector base.

    The served set is built by prepending :data:`_API_MOUNT` to the schema's
    mount-relative paths; the connector dispatches under its own ``API_BASE``.
    If they ever diverge the reconcile would compare mismatched namespaces —
    this fails loudly instead. The ticket constant is pinned to the same mount.
    """
    assert _API_MOUNT == API_BASE
    assert f"{_API_MOUNT}/access/ticket" == _connector._TICKET_PATH


def test_declared_op_id_manifest_is_pinned() -> None:
    """Pin the declared op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration is proven on
    every sweep. ``NODE`` / ``UPID`` are the injected placeholder values the
    task-poll capture carries (they template-match ``{node}`` / ``{upid}`` in
    the reconcile below). A new hand-coded path, a renamed handler literal, or
    a dropped op must update this manifest consciously.
    """
    assert _declared_op_ids() == {
        "GET:/api2/json/version",
        "GET:/api2/json/nodes",
        "GET:/api2/json/nodes/NODE/tasks/UPID/status",
        "POST:/api2/json/access/ticket",
    }


def test_served_extraction_parses_the_pinned_schema() -> None:
    """Guard: the apidata.js tree-walk keeps parsing the pinned schema.

    Pins a floor on the route count (the 8.4 pin serves 605) plus one
    sentinel per extraction feature: a bare read, a POST, a deep templated
    path, and a DELETE. A re-grounded shelf that changes the schema wrapper or
    shape fails here with a parse-shaped message instead of failing the
    reconcile with spurious unserved paths.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = proxmox_served_op_ids(spec_path)
    assert len(served) >= 550
    assert "GET:/api2/json/version" in served
    assert "POST:/api2/json/access/ticket" in served
    assert "GET:/api2/json/nodes/{node}/qemu/{vmid}/status/current" in served
    assert "DELETE:/api2/json/nodes/{node}/qemu/{vmid}" in served


def test_declared_op_ids_are_served_by_the_pinned_schema() -> None:
    """Every hand-coded op_id is served by the pinned proxmox-8.4 schema.

    The served set passed to the harness is the documented templates unioned
    with the declared literals that instantiate one of them, so an unserved
    literal fails with near-miss hints drawn from the real template set (the
    #2970 repoint-lead shape).
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served_templates = proxmox_served_op_ids(spec_path)
    declared = _declared_op_ids()
    matched = _matched(declared, served_templates)
    assert_op_ids_served(declared, served_templates | matched, spec_label=_SPEC_LABEL)

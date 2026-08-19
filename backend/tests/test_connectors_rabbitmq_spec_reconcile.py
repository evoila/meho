# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""rabbitmq real-spec reconcile lane (#2989) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the rabbitmq connector puts on
the wire is served by the pinned ``rabbitmq-4.3`` grounding artifacts on
the shelf. RabbitMQ publishes **no OpenAPI/Swagger spec** for the
management HTTP API (verified 2026-08-18: zero openapi artifacts in
``rabbitmq/rabbitmq-server`` at ``v4.3.5``; the community thinkco OAS2
document is unofficial and pinned to 3.7.1) — the pinned artifacts are
MPL-2.0 sources from the vendor's own tree at tag ``v4.3.5`` (provenance
in the shelf's ``MANIFEST.md``):

* ``http-api-index.md`` — the management plugin's own HTTP API route
  table (``deps/rabbitmq_management/priv/www/api/index.html``, the page
  the plugin serves at ``/api/index.html``) converted to a markdown
  route table: one row per documented path, ``X`` marks per method,
  vendor template segments ``{name}``-braced.
* ``rabbit_shovel_mgmt_shovels.erl`` — the shovel-management plugin's
  dispatcher module, vendored verbatim. The core route table does not
  document the surfaces extension plugins mount under ``/api``; this
  module's ``dispatcher()`` is the authoritative route registry for
  ``/api/shovels[/{vhost}]`` (its ``allowed_methods`` pins the surface
  to HEAD/GET/OPTIONS). ``/api/federation-links``, by contrast, *is* in
  the core table, so no federation source is pinned.

Per the standard doc, non-OpenAPI artifacts still resolve through
``require_shelf_spec`` (uniform skip semantics) while the lane supplies
its own served-set extraction and feeds ``assert_op_ids_served``.

The declared set is **captured from live handler execution** (the #2944
never-a-hardcoded-mirror discipline, taken one step further than source
introspection): the connector's path literals live inline in its handler
bodies, so a recording subclass overrides ``_get_json`` — the single GET
seam every read op funnels through — and each curated op's handler runs
against it, once bare and once with ``vhost="/"`` for the vhost-scoped
ops. The captured strings are byte-for-byte the request paths dispatch
puts on the wire, including the percent-encoded default vhost
(``/api/queues/%2F``).

Encoding/template honesty: the captured paths are runtime **literals**
while the vendor documents **templates** (``/api/queues/{vhost}``,
``/api/parameters/{component}``). The comparison resolves a literal
against the served templates segment-wise — a template segment matches
exactly one literal segment — so ``%2F`` (the encoded ``/`` vhost) must
land in a single segment to match ``{vhost}``, and
``/api/parameters/shovel`` matches ``/api/parameters/{component}`` with
no sentinel rewriting. ``rabbitmq.request`` (the agent-supplied
arbitrary-path passthrough) declares no hand-coded vendor path and is
excluded.

First run against the real shelf (the local pre-verify against the
consumer shelf PR branch, 2026-08-18): **green** — all 21 captured
op_ids served (19 by the route table, 2 by the shovel dispatcher). A
red lane here is the guard surfacing a real finding, not harness noise.
Protocol: docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.rabbitmq import RABBITMQ_OPS, RabbitMqConnector
from meho_backplane.connectors.rabbitmq.connector import _NODES_PATH, _OVERVIEW_PATH
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "rabbitmq-4.3"
_SPEC_FILE = "http-api-index.md"
_SHOVEL_FILE = "rabbit_shovel_mgmt_shovels.erl"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE} + {_SHOVEL_FILE}"

#: The op that takes an agent-supplied path at call time — no hand-coded
#: vendor path to reconcile.
_PASSTHROUGH_OP_ID = "rabbitmq.request"

_ROW_METHODS = ("GET", "PUT", "DELETE", "POST")

#: A cowboy dispatcher route in the vendored shovel module:
#: ``{"/shovels", ?MODULE, []}`` — ``:vhost``-style bindings become
#: ``{vhost}`` templates, mounted under the management plugin's ``/api``.
_DISPATCHER_ROUTE_RE = re.compile(r'\{"(/[^"]+)",\s*\?MODULE')


class _RecordingRabbitMq(RabbitMqConnector):
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
    """Run every curated op's handler against the recording seam.

    Vhost-scoped ops (their parameter schema exposes ``vhost``) run
    twice — bare, and with the default vhost ``"/"`` so the captured
    variant carries the connector's real percent-encoding (``%2F``).
    """

    async def _capture() -> dict[str, set[str]]:
        connector = _RecordingRabbitMq()
        operator = object()
        target = object()
        captured: dict[str, set[str]] = {}
        for op in RABBITMQ_OPS:
            if op.op_id == _PASSTHROUGH_OP_ID:
                continue
            handler = getattr(connector, op.handler_attr)
            connector.paths = []
            await handler(operator, target, {})
            if "vhost" in op.parameter_schema.get("properties", {}):
                await handler(operator, target, {"vhost": "/"})
            captured[op.op_id] = set(connector.paths)
        return captured

    return asyncio.run(_capture())


def _declared_op_ids() -> set[str]:
    """Every hand-coded request path the connector emits, as ``GET:`` op_ids.

    Every curated handler funnels through the GET-only ``_get_json``
    seam (the connector's method gate refuses anything but GET/HEAD, and
    HEAD exists only on the excluded passthrough), so the captured paths
    are all ``GET:``.
    """
    return {f"GET:{path}" for paths in _captured_paths_by_op().values() for path in paths}


def documented_index_op_ids(doc_path: Path) -> set[str]:
    """Extract ``METHOD:/path`` template op_ids from the converted route table.

    A route row is a 6-cell markdown table row — four method-mark cells
    (``X`` or empty), the path, the description — whose path cell starts
    with ``/api``. The converter emits exactly one path per row; a
    vendor ``(deprecated)`` annotation trails the path inside the same
    cell (whitespace-separated) and is not captured. The doc contains no
    escaped pipes, so plain cell-splitting is faithful.
    """
    op_ids: set[str] = set()
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 6 or not cells[4].startswith("/api"):
            continue
        marks = cells[:4]
        if any(mark not in ("X", "") for mark in marks):
            continue
        path = cells[4].split()[0]
        op_ids.update(
            f"{method}:{path}"
            for method, mark in zip(_ROW_METHODS, marks, strict=True)
            if mark == "X"
        )
    return op_ids


def shovel_dispatcher_op_ids(module_path: Path) -> set[str]:
    """Extract the vendored shovel dispatcher's routes as ``GET:`` op_ids.

    ``dispatcher()`` routes are mounted under the management plugin's
    ``/api`` prefix (the ``rabbit_mgmt_extension`` behaviour); cowboy
    ``:vhost`` bindings become ``{vhost}`` template segments. The module
    serves reads only — its ``allowed_methods`` is HEAD/GET/OPTIONS,
    pinned by :func:`test_shovel_dispatcher_extraction_parses_the_vendored_module`
    — so every route is declared ``GET:``.
    """
    text = module_path.read_text(encoding="utf-8")
    return {
        "GET:/api" + re.sub(r":([A-Za-z_]+)", r"{\1}", route)
        for route in _DISPATCHER_ROUTE_RE.findall(text)
    }


def _is_served_by_template(op_id: str, template_op_id: str) -> bool:
    """True iff the literal op_id instantiates the template op_id.

    Same method, same segment count, and each template segment either
    matches its literal segment byte-for-byte or is a ``{param}``
    placeholder. A placeholder matches exactly one segment — the
    percent-encoded default vhost ``%2F`` therefore matches ``{vhost}``
    while a raw (unencoded) ``/`` in its place would split into two
    segments and fail, which is exactly the encoding bug this lane
    must catch.
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


def test_capture_enumerates_every_curated_op() -> None:
    """Guard: the recording seam sees every curated op's real request paths.

    Pins the exact captured path set per op (the #2944 guard shape): a
    handler that grows a new inline path literal, stops funnelling
    through ``_get_json``, or a new op that skips the capture, changes
    this mapping and fails here instead of silently shrinking the
    reconciled set to a vacuous pass.
    """
    captured = _captured_paths_by_op()
    assert captured == {
        "rabbitmq.overview": {"/api/overview"},
        "rabbitmq.nodes": {"/api/nodes"},
        "rabbitmq.exchanges": {"/api/exchanges", "/api/exchanges/%2F"},
        "rabbitmq.queues": {"/api/queues", "/api/queues/%2F"},
        "rabbitmq.bindings": {"/api/bindings", "/api/bindings/%2F"},
        "rabbitmq.vhosts": {"/api/vhosts"},
        "rabbitmq.connections": {"/api/connections"},
        "rabbitmq.channels": {"/api/channels"},
        "rabbitmq.consumers": {"/api/consumers"},
        "rabbitmq.shovels": {"/api/parameters/shovel", "/api/parameters/shovel/%2F"},
        "rabbitmq.shovel_status": {"/api/shovels", "/api/shovels/%2F"},
        "rabbitmq.federation_links": {"/api/federation-links"},
        "rabbitmq.parameters": {"/api/parameters"},
        "rabbitmq.policies": {"/api/policies", "/api/policies/%2F"},
        "rabbitmq.definitions": {"/api/definitions"},
    }


def test_fingerprint_probe_literals_are_covered_by_curated_ops() -> None:
    """Guard: the fingerprint/probe path constants can't drift uncovered.

    ``RabbitMqConnector.fingerprint`` (and ``probe`` via delegation)
    reads the module constants ``_OVERVIEW_PATH`` / ``_NODES_PATH``. The
    curated ``rabbitmq.overview`` / ``rabbitmq.nodes`` handlers read the
    same constants, so the probe surface is covered by value — pinned
    here so an edit to either side surfaces instead of silently dropping
    the probe paths from the reconcile.
    """
    captured = _captured_paths_by_op()
    assert captured["rabbitmq.overview"] == {_OVERVIEW_PATH}
    assert captured["rabbitmq.nodes"] == {_NODES_PATH}


def test_route_table_extraction_parses_the_pinned_doc() -> None:
    """Guard: the markdown extractor keeps parsing the converted route table.

    Pins a floor on the extracted method:path count (the v4.3.5 snapshot
    documents 137 pairs over 103 path rows) plus one sentinel per
    extraction feature: a bare GET row, a multi-method row, a template
    row, a row whose path variant was split off a shared ``<br/>`` cell,
    and a vendor-deprecated variant (annotation trails the path, not
    captured into it). A re-grounded shelf that changes the table format
    fails here with a parse-shaped message instead of failing the
    reconcile with 21 spurious unserved paths.
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    op_ids = documented_index_op_ids(doc_path)
    assert len(op_ids) >= 130
    assert "GET:/api/overview" in op_ids
    assert {"GET:/api/cluster-name", "PUT:/api/cluster-name"} <= op_ids
    assert "GET:/api/parameters/{component}/{vhost}" in op_ids
    assert "GET:/api/federation-links/{vhost}" in op_ids
    assert "GET:/api/all-configuration" in op_ids


def test_shovel_dispatcher_extraction_parses_the_vendored_module() -> None:
    """Guard: the vendored shovel module keeps yielding its two GET routes.

    Extraction must produce exactly the two known routes, and the
    module's ``allowed_methods`` must still pin the surface to
    HEAD/GET/OPTIONS — a re-vendored module that grows a mutating verb
    or moves its dispatcher shape fails here, keeping the lane's
    GET-only declaration honest.
    """
    module_path = require_shelf_spec(_SPEC_DIR, _SHOVEL_FILE)
    assert shovel_dispatcher_op_ids(module_path) == {
        "GET:/api/shovels",
        "GET:/api/shovels/{vhost}",
    }
    text = module_path.read_text(encoding="utf-8")
    allowed = re.search(r"allowed_methods\(.*?\)\s*->\s*\{\[(.*?)\]", text, re.DOTALL)
    assert allowed is not None
    assert set(re.findall(r'<<"([A-Z]+)">>', allowed.group(1))) == {"HEAD", "GET", "OPTIONS"}


def test_shovel_only_routes_are_exactly_the_pinned_set() -> None:
    """Guard: reliance on the non-route-table source stays explicit.

    The ``/api/shovels`` surface is served only by the vendored shovel
    dispatcher, not the core route table. This pins exactly which
    declared op_ids ride on it: a new hand-coded path landing outside
    the documented route table must be a deliberate decision here, and
    a future re-ground that drops the vendored module shrinks the union
    and turns the main reconcile red — surfacing the gap instead of
    silently hiding it.
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    module_path = require_shelf_spec(_SPEC_DIR, _SHOVEL_FILE)
    declared = _declared_op_ids()
    index_served = _matched(declared, documented_index_op_ids(doc_path))
    shovel_served = _matched(declared, shovel_dispatcher_op_ids(module_path))
    assert shovel_served - index_served == {
        "GET:/api/shovels",
        "GET:/api/shovels/%2F",
    }


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned rabbitmq-4.3 artifacts.

    The served set passed to the harness is the documented templates
    unioned with the declared literals that instantiate one of them, so
    an unserved literal fails with near-miss hints drawn from the real
    template set (the #2970 repoint-lead shape).
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    module_path = require_shelf_spec(_SPEC_DIR, _SHOVEL_FILE)
    served_templates = documented_index_op_ids(doc_path) | shovel_dispatcher_op_ids(module_path)
    declared = _declared_op_ids()
    matched = _matched(declared, served_templates)
    assert_op_ids_served(declared, served_templates | matched, spec_label=_SPEC_LABEL)

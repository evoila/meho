# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hetzner-robot real-spec reconcile lane (#2985) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the hetzner-robot connector
surface carries is served by the pinned ``hetzner-robot-2026-04``
grounding artifact on the shelf. Hetzner publishes **no machine-readable
spec** for the Robot Webservice (no OpenAPI, no Swagger, no WSDL) — the
pinned artifact is the vendor's single HTML reference page
(https://robot.hetzner.com/doc/webservice/en.html) converted to markdown
(``webservice-en.md``, provenance in the shelf's ``MANIFEST.md``). Per
the standard doc, non-OpenAPI artifacts still resolve through
``require_shelf_spec`` (uniform skip semantics) while the lane supplies
its own served-set extraction and feeds ``assert_op_ids_served``.

The served set is the union of two documented-route families:

* **Canonical routes** — the ``## METHOD /path`` section headings
  (105 routes in the 2026-04 snapshot).
* **Documented deprecated alternatives** — the ``| @deprecated METHOD
  /path | …`` table rows. Hetzner documents IP-addressed variants of
  the server-number routes as working deprecated alternatives ("The
  main IPv4 address may be used alternatively to specify the server"),
  and the connector's ``{server-ip}`` paths land on exactly these rows.
  A dedicated pin below keeps the deprecated-only reliance explicit.

The declared set is introspected from the live constants (the #2944
pattern — never a hardcoded mirror), across the three surfaces that
hand-code a Robot path:

* :data:`~meho_backplane.connectors.hetzner_robot.core_ops.ROBOT_CORE_OPS`
  — the curated read-core op_ids.
* The MEHO-authored ingest source
  ``operations/ingest/specs/hetzner_robot_minimal.yaml`` (#2079) —
  parsed through the real ingest parser, so its op_ids are byte-for-byte
  what a live ingest lands in ``endpoint_descriptor``. The spec is
  MEHO-authored (the vendor publishes none), which makes its paths
  hand-coded and exactly this lane's exposure.
* :class:`~meho_backplane.connectors.hetzner_robot.HetznerRobotConnector`
  — the inline ``GET /server`` fingerprint/probe literal, covered by
  value through the curated ``GET:/server`` (pinned below).

A red lane here is the guard surfacing a real finding, not harness
noise — first run against the real shelf caught two: ``GET:/query``
(no such Robot Webservice endpoint exists; the ``hetzner-robot.about``
surface was removed in the same PR) and the ``/vswitch/{id}`` template
family (vendor template is ``{vswitch-id}``; renamed in the same PR,
runtime request path unchanged). Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import re
from importlib.resources import as_file, files
from pathlib import Path

from meho_backplane.connectors.hetzner_robot import ROBOT_CORE_OPS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "hetzner-robot-2026-04"
_SPEC_FILE = "webservice-en.md"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The MEHO-authored minimal OpenAPI spec the live ingest consumes (#2079).
_MINIMAL_SPEC_PACKAGE = "meho_backplane.operations.ingest.specs"
_MINIMAL_SPEC_RESOURCE = "hetzner_robot_minimal.yaml"

#: Canonical documented route: a ``## METHOD /path`` section heading.
_ROUTE_HEADING_RE = re.compile(r"^## (GET|POST|PUT|DELETE) (/\S+)$", re.MULTILINE)

#: Documented deprecated alternative: a ``| @deprecated METHOD /path |``
#: table row. An optional trailing ``-X METHOD`` curl-style override names
#: the effective verb (e.g. ``POST /boot/{server-ip}/rescue -X DELETE``).
_DEPRECATED_ROUTE_RE = re.compile(
    r"^\| @deprecated (GET|POST|PUT|DELETE) (/\S+?)(?: -X (GET|POST|PUT|DELETE))? \|",
    re.MULTILINE,
)


def _unescape(path: str) -> str:
    """Undo the markdown-conversion escape of underscores (``\\_`` → ``_``)."""
    return path.replace("\\_", "_")


def documented_robot_op_ids(doc_path: Path) -> tuple[set[str], set[str]]:
    """Extract ``(canonical, deprecated)`` op_id sets from the pinned vendor doc.

    The Slate-rendered reference inlines every endpoint as a ``## METHOD
    /path`` heading; deprecated alternatives appear as ``| @deprecated
    METHOD /path | …`` rows in per-section Deprecations tables. Both use
    the vendor's own template-segment names (``{server-number}``,
    ``{vswitch-id}``, …), so the returned strings compare byte-for-byte
    against hand-coded op_ids — same discipline as the OpenAPI lanes.
    """
    text = doc_path.read_text(encoding="utf-8")
    canonical = {f"{method}:{_unescape(path)}" for method, path in _ROUTE_HEADING_RE.findall(text)}
    deprecated = {
        f"{override or method}:{_unescape(path)}"
        for method, path, override in _DEPRECATED_ROUTE_RE.findall(text)
    }
    return canonical, deprecated


def _minimal_spec_op_ids() -> set[str]:
    """The shipped ingest spec's op_ids, via the real ingest parser."""
    resource = files(_MINIMAL_SPEC_PACKAGE).joinpath(_MINIMAL_SPEC_RESOURCE)
    with as_file(resource) as spec_path:
        return openapi_served_op_ids(spec_path)


def _curated_op_ids() -> set[str]:
    """The curated read-core op_ids, introspected live."""
    return {op.op_id for op in ROBOT_CORE_OPS}


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the hetzner-robot surface carries."""
    return _curated_op_ids() | _minimal_spec_op_ids()


def test_fingerprint_probe_literal_is_covered_by_the_curated_op() -> None:
    """Guard: the connector's inline ``GET /server`` literal can't drift uncovered.

    ``HetznerRobotConnector.fingerprint`` and ``.probe`` GET the literal
    ``"/server"`` inline (not introspectable), relying on the curated
    ``GET:/server`` declaring the same string. This pins that value
    coverage so an edit to either side surfaces here instead of silently
    dropping the probe path from the reconcile.
    """
    assert "GET:/server" in _curated_op_ids()


def test_ingest_spec_extends_the_curated_core_by_the_pinned_extras() -> None:
    """Guard: the two introspection wires each enumerate what they should.

    The MEHO-authored ingest spec must cover surfaces beyond the curated
    read core — the declared write ops and the deliberately-uncurated
    ``GET:/rdns/{ip}`` — and nothing else unexpected. A drifted spec or
    a broken introspection wire shrinks or grows this delta and fails
    here rather than passing vacuously.
    """
    extras = _minimal_spec_op_ids() - _curated_op_ids()
    assert extras == {
        "DELETE:/vswitch/{vswitch-id}",
        "GET:/rdns/{ip}",
        "POST:/firewall/{server-ip}",
        "POST:/order/server_addon/transaction",
        "POST:/vswitch/{vswitch-id}",
    }


def test_documented_route_extraction_parses_the_pinned_doc() -> None:
    """Guard: the markdown extractor keeps parsing the vendor doc's shapes.

    Pins a floor on the canonical-route count (the 2026-04 snapshot
    documents 105) plus one sentinel per extraction feature: a plain
    heading, an escaped-underscore heading, a deprecated row, and a
    deprecated row whose effective verb comes from the ``-X`` override.
    A re-grounded shelf that changes the page format fails here with a
    parse-shaped message instead of failing the reconcile with 15
    spurious unserved paths.
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    canonical, deprecated = documented_robot_op_ids(doc_path)
    assert len(canonical) >= 100
    assert "GET:/server" in canonical
    assert "POST:/order/server_addon/transaction" in canonical
    assert "GET:/server/{server-ip}" in deprecated
    assert "DELETE:/boot/{server-ip}/rescue" in deprecated


def test_deprecated_only_routes_are_exactly_the_pinned_set() -> None:
    """Guard: reliance on documented-deprecated routes stays explicit.

    The ``{server-ip}``-addressed paths are served only through the
    vendor's Deprecations tables ("the main IPv4 address may be used
    alternatively"). This pins exactly which declared op_ids ride on
    deprecated-only routes: a new hand-coded path landing on one must be
    a deliberate decision here, and a future re-ground that drops the
    deprecated rows shrinks the union and turns the main reconcile red —
    forcing the migration to ``{server-number}`` addressing instead of
    silently hiding it.
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    canonical, deprecated = documented_robot_op_ids(doc_path)
    deprecated_only = _declared_op_ids() & (deprecated - canonical)
    assert deprecated_only == {
        "GET:/firewall/{server-ip}",
        "GET:/server/{server-ip}",
        "POST:/firewall/{server-ip}",
    }


def test_declared_op_ids_are_served_by_the_pinned_doc() -> None:
    """Every hand-coded op_id is served by the pinned hetzner-robot-2026-04 doc."""
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    canonical, deprecated = documented_robot_op_ids(doc_path)
    assert_op_ids_served(
        _declared_op_ids(),
        canonical | deprecated,
        spec_label=_SPEC_LABEL,
    )

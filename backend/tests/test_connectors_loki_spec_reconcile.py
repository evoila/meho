# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""loki real-spec reconcile lane (#2991) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the loki connector puts on the
wire is served by the pinned ``loki-3.7`` grounding artifact on the shelf.
Grafana Loki publishes **no OpenAPI/Swagger spec** for its HTTP API
(verified 2026-08-18: the only ``openapi``-named artifacts in
``grafana/loki`` at ``v3.7.6`` are the Loki *Operator*'s CRD schemas —
``openAPIV3Schema`` for ``LokiStack`` / ``AlertingRule`` / … — and Go
module names in ``go.mod`` / ``*.sum``, none an HTTP-API spec) — so the
pinned artifact is the vendor's own documented HTTP API reference
(``docs/sources/reference/loki-http-api.md``, AGPL-3.0, byte-identical
from tag ``v3.7.6``; provenance in the shelf's ``MANIFEST.md``). Its
``## Endpoints`` section lists every route as a bulleted markdown link
``- [`METHOD /path`](#anchor)``. This is the ``hetzner-robot-2026-04``
documented-route precedent, from an OSS-licensed source: non-OpenAPI
artifacts still resolve through ``require_shelf_spec`` (uniform skip
semantics) while the lane supplies its own served-set extraction and
feeds ``assert_op_ids_served``.

The declared set is **captured from live handler execution** (the #2944
never-a-hardcoded-mirror discipline, one step past source introspection —
the rabbitmq precedent): the connector's read-path literals live inline in
its handler bodies (and ``LOKI_OPS`` carries the logical op ids like
``loki.query``, not the HTTP paths), so a recording subclass overrides
``_loki_get`` — the single GET seam every curated read op funnels through
— and each curated op's handler runs against it. The captured strings are
byte-for-byte the request paths dispatch puts on the wire. The tenant-free
fingerprint/probe literals travel a *different* seam (``_unauth_get``, not
``_loki_get``), so they are pinned separately from their live module
constants (``_BUILDINFO_PATH`` / ``_READY_PATH`` / ``_LABELS_PATH``).

Encoding/template honesty: the captured ``label_values`` path is a runtime
literal (``/loki/api/v1/label/x/values``) while the reference documents a
template (``/loki/api/v1/label/<name>/values``, normalised to
``{name}``). The comparison resolves a literal against the served
templates segment-wise — a template segment matches exactly one literal
segment — so the literal instantiates ``{name}`` with no sentinel
rewriting. ``loki.get`` (the agent-supplied arbitrary-path passthrough)
declares no hand-coded vendor path and is excluded.

First run against the real shelf (the local pre-verify against the
consumer shelf branch for ``claude-rdc-hetzner-dc#2544``, 2026-08-18):
**green** — all 7 declared op_ids served (6 by byte-for-byte match, the
``label_values`` literal by template). A red lane here is the guard
surfacing a real finding, not harness noise. Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.loki import LOKI_OPS, LokiConnector
from meho_backplane.connectors.loki.connector import (
    _BUILDINFO_PATH,
    _LABELS_PATH,
    _READY_PATH,
)
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "loki-3.7"
_SPEC_FILE = "loki-http-api.md"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The op that takes an agent-supplied path at call time — no hand-coded
#: vendor path to reconcile (the rabbitmq.request / hetzner-passthrough shape).
_PASSTHROUGH_OP_ID = "loki.get"

#: The tenant-free fingerprint/probe literals the connector issues via
#: ``_unauth_get`` (a different seam than the curated ops' ``_loki_get``),
#: pinned from the live module constants. ``_LABELS_PATH`` is also a
#: curated-op path (``loki.labels``); ``_BUILDINFO_PATH`` / ``_READY_PATH``
#: are fingerprint-only (``/ready`` is not even under ``/loki/api/v1``).
_FINGERPRINT_PROBE_PATHS = (_BUILDINFO_PATH, _READY_PATH, _LABELS_PATH)

#: A documented route bullet in the reference's ``## Endpoints`` section:
#: ``- [`METHOD /path`](#anchor)``. The path runs to the closing backtick
#: (paths carry no backtick of their own).
_ROUTE_RE = re.compile(r"^- \[`(GET|POST|PUT|DELETE|PATCH|HEAD) (/[^`]+)`\]", re.MULTILINE)

#: An angle-bracket template segment (``<name>``) → the brace form the rule
#: endpoints already use (``{namespace}``), so the served set carries one
#: template convention that the connector's ``{name}`` literal matches.
_ANGLE_RE = re.compile(r"<([^>]+)>")


class _RecordingLoki(LokiConnector):
    """Records every path the curated ops ask ``_loki_get`` for; no wire, no auth."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    async def _loki_get(
        self,
        operator: Any,
        target: Any,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        del operator, target, params, tenant
        self.paths.append(path)
        return {}


def _minimal_params(parameter_schema: dict[str, Any]) -> dict[str, Any]:
    """Synthesise a minimal call payload from an op's *required* params.

    Driven by the live parameter schema (never a hardcoded param map): each
    required property gets a type-appropriate placeholder — a one-item list
    for an ``array`` param (``match``), a single-segment string otherwise
    (``query`` / ``name``). Placeholders never affect the request *path*
    except ``name``, whose value becomes the ``label/<value>/values``
    segment (a plain single-segment literal that instantiates ``{name}``).
    """
    properties = parameter_schema.get("properties", {})
    payload: dict[str, Any] = {}
    for name in parameter_schema.get("required", []):
        prop_type = properties.get(name, {}).get("type")
        payload[name] = ["x"] if prop_type == "array" else "x"
    return payload


def _captured_paths_by_op() -> dict[str, set[str]]:
    """Run every curated op's handler against the recording seam.

    The agent-path passthrough (``loki.get``) is excluded — it hand-codes
    no vendor path. Source-constant introspection cannot reach an inline
    handler literal, so live execution is the non-mirror enumeration.
    """

    async def _capture() -> dict[str, set[str]]:
        connector = _RecordingLoki()
        captured: dict[str, set[str]] = {}
        for op in LOKI_OPS:
            if op.op_id == _PASSTHROUGH_OP_ID:
                continue
            handler = getattr(connector, op.handler_attr)
            connector.paths = []
            await handler(object(), object(), _minimal_params(op.parameter_schema))
            captured[op.op_id] = set(connector.paths)
        return captured

    return asyncio.run(_capture())


def _fingerprint_probe_op_ids() -> set[str]:
    """The fingerprint/probe literals as ``GET:`` op_ids (all issued via GET)."""
    return {f"GET:{path}" for path in _FINGERPRINT_PROBE_PATHS}


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the connector puts on the wire.

    The curated read paths (captured live) unioned with the tenant-free
    fingerprint/probe literals (pinned from the live module constants).
    Every path is reached by GET — the curated ops funnel through the
    GET-only ``_loki_get`` and the fingerprint/probe issue GETs via
    ``_unauth_get``.
    """
    curated = {f"GET:{path}" for paths in _captured_paths_by_op().values() for path in paths}
    return curated | _fingerprint_probe_op_ids()


def documented_route_op_ids(doc_path: Path) -> set[str]:
    """Extract ``METHOD:/path`` op_ids from the reference's documented routes.

    The ``## Endpoints`` section lists every route as a bulleted markdown
    link ``- [`METHOD /path`](#anchor)``. The one angle-bracket template
    segment (``/loki/api/v1/label/<name>/values``) is normalised to the
    brace form so the served set carries one template convention.
    """
    text = doc_path.read_text(encoding="utf-8")
    op_ids: set[str] = set()
    for method, path in _ROUTE_RE.findall(text):
        normalized = _ANGLE_RE.sub(r"{\1}", path)
        op_ids.add(f"{method}:{normalized}")
    return op_ids


def _is_served_by_template(op_id: str, template_op_id: str) -> bool:
    """True iff the literal op_id instantiates the template op_id.

    Same method, same segment count, and each template segment either
    matches its literal segment byte-for-byte or is a ``{param}``
    placeholder. A placeholder matches exactly one segment, so the
    ``label_values`` literal ``/loki/api/v1/label/x/values`` instantiates
    ``/loki/api/v1/label/{name}/values``.
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
    """Guard: the recording seam sees every curated op's real request path.

    Pins the exact captured path set per op (the #2944 guard shape): a
    handler that grows a new inline path literal, stops funnelling through
    ``_loki_get``, or a new curated op that skips the capture changes this
    mapping and fails here instead of silently shrinking the reconciled
    set to a vacuous pass. ``loki.get`` is absent by design (excluded).
    """
    assert _captured_paths_by_op() == {
        "loki.query": {"/loki/api/v1/query"},
        "loki.query_range": {"/loki/api/v1/query_range"},
        "loki.labels": {"/loki/api/v1/labels"},
        "loki.label_values": {"/loki/api/v1/label/x/values"},
        "loki.series": {"/loki/api/v1/series"},
    }


def test_fingerprint_probe_literals_are_pinned_and_split_from_the_curated_set() -> None:
    """Guard: the fingerprint/probe constants can't drift uncovered.

    ``_LABELS_PATH`` is reached by both ``loki.labels`` (curated) and the
    fingerprint's best-effort label count, so it is covered by value in the
    captured curated set. ``_BUILDINFO_PATH`` and ``/ready`` ride *only* on
    the fingerprint/probe ``_unauth_get`` seam; this pins exactly which
    literals are fingerprint-only, so a new hand-coded probe path landing
    outside the curated ops is a conscious edit here rather than a silent
    reconcile gap.
    """
    curated = {f"GET:{path}" for paths in _captured_paths_by_op().values() for path in paths}
    assert f"GET:{_LABELS_PATH}" in curated
    fingerprint_only = _fingerprint_probe_op_ids() - curated
    assert fingerprint_only == {"GET:/loki/api/v1/status/buildinfo", "GET:/ready"}


def test_route_reference_extraction_parses_the_pinned_doc() -> None:
    """Guard: the markdown extractor keeps parsing the vendor route list.

    Pins a floor on the extracted route count (the v3.7.6 snapshot
    documents 43) plus one sentinel per extraction feature: a plain
    ``/loki/api/v1`` GET route, a top-level route outside ``/loki/api/v1``
    (``/ready``), the angle-bracket → brace normalisation
    (``label/{name}/values``), a non-GET (ingest) route, and a
    brace-template route kept verbatim. A re-grounded shelf that changes
    the page format fails here with a parse-shaped message instead of
    failing the reconcile with spurious unserved paths.
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    op_ids = documented_route_op_ids(doc_path)
    assert len(op_ids) >= 40
    assert "GET:/loki/api/v1/query_range" in op_ids
    assert "GET:/ready" in op_ids
    assert "GET:/loki/api/v1/label/{name}/values" in op_ids
    assert "POST:/loki/api/v1/push" in op_ids
    assert "DELETE:/loki/api/v1/rules/{namespace}/{groupName}" in op_ids


def test_declared_op_ids_are_served_by_the_pinned_reference() -> None:
    """Every hand-coded op_id is served by the pinned loki-3.7 reference.

    The served set passed to the harness is the documented templates
    unioned with the declared literals that instantiate one of them, so an
    unserved literal fails with near-miss hints drawn from the real route
    set (the #2970 repoint-lead shape).
    """
    doc_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served_templates = documented_route_op_ids(doc_path)
    declared = _declared_op_ids()
    matched = _matched(declared, served_templates)
    assert_op_ids_served(declared, served_templates | matched, spec_label=_SPEC_LABEL)

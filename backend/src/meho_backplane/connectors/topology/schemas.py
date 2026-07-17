# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter + response schemas for the ``topology.*`` targetless typed ops (#2537).

Single source for both validation layers: the MCP tools'
``inputSchema`` (:mod:`meho_backplane.mcp.tools.topology`,
:mod:`meho_backplane.mcp.tools.topology_create_node`) and the
``endpoint_descriptor.parameter_schema`` rows the registrar in
:mod:`meho_backplane.connectors.topology.ops` upserts are the same
JSON Schema 2020-12 documents, so the wire boundary and the dispatcher
can never drift apart. Split out of ``ops.py`` to keep both modules
under the 600-line file guidance.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.db.models import (
    KIND_SLUG_MAX_LENGTH,
    KIND_SLUG_MIN_LENGTH,
    KIND_SLUG_PATTERN,
    WELL_KNOWN_NODE_KINDS,
)

__all__ = [
    "ANNOTATE_PARAMETER_SCHEMA",
    "ANNOTATE_RESPONSE_SCHEMA",
    "BULK_IMPORT_MAX_EDGES",
    "BULK_IMPORT_PARAMETER_SCHEMA",
    "BULK_IMPORT_RESPONSE_SCHEMA",
    "BULK_IMPORT_TOOL_INPUT_SCHEMA",
    "CREATE_NODE_PARAMETER_SCHEMA",
    "CREATE_NODE_RESPONSE_SCHEMA",
    "UNANNOTATE_PARAMETER_SCHEMA",
    "UNANNOTATE_RESPONSE_SCHEMA",
]

#: Well-known graph-node kinds, materialised once at module load so the
#: create_node description's suggestion list tracks the documented core
#: set automatically. The vocabulary is open (T1 #2534): the schema
#: constrains ``kind`` by slug pattern, not by membership.
_NODE_KIND_VALUES: list[str] = sorted(WELL_KNOWN_NODE_KINDS)


# ---------------------------------------------------------------------------
# Parameter schemas (JSON Schema 2020-12) — single source for both the
# endpoint_descriptor.parameter_schema and the MCP tools' inputSchema.
# ---------------------------------------------------------------------------

ANNOTATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` of the edge's `from` endpoint. "
                "Resolved against the operator's tenant (cross-tenant "
                "is structurally impossible — no `tenant_id` argument)."
            ),
        },
        "kind": {
            "type": "string",
            "pattern": KIND_SLUG_PATTERN,
            "minLength": KIND_SLUG_MIN_LENGTH,
            "maxLength": KIND_SLUG_MAX_LENGTH,
            "description": (
                "Edge kind: a lowercase slug (letters/digits joined "
                "by `.`, `_` or `-`; 2-63 chars). The vocabulary is "
                "open — any slug matching the pattern is accepted — "
                "but prefer a well-known kind when one fits. "
                "Operator-curated well-known kinds "
                "(`authenticates-via`, `depends-on`, "
                "`replicates-to`, `backed-up-by`, `routes-via`, "
                "`policy-binds`) cover the cross-system relationships "
                "auto-discovery cannot infer — those are the canonical "
                "use cases. The four auto-discoverable kinds "
                "(`runs-on`, `mounts`, `routes-through`, `belongs-to`) "
                "are accepted too, as are novel kinds "
                "(`resolves-to`, `same-as`, ...) when no well-known "
                "kind describes the relationship — `same-as` is the "
                "documented convention for cross-system identity "
                "stitching. A curated assertion of an auto-kind "
                "lands as a §6 conflict marker *only when a competing "
                "**auto** edge already exists for that pair* — i.e. on "
                "a pair a probe covers (today, only the Kubernetes "
                "connector populates auto edges; G0.18-T4 #1357). For "
                "a non-k8s pair, or any pair no probe covers, the "
                "curated row inserts clean with "
                "`source: curated, conflicts: []` and is the right way "
                "to assert e.g. `runs-on` against vault / vcenter / "
                "nsx / sddc-manager / gh targets until non-k8s "
                "populators land. The over-cautious 'annotating "
                "auto-kinds is noise' wording the pre-G0.18-T4 doc "
                "carried steered operators away from this legitimate "
                "path."
            ),
        },
        "to_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` of the edge's `to` endpoint. Same "
                "resolution rules as `from_name`."
            ),
        },
        "from_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `from_name` "
                "endpoint. Required only when the bare name resolves to "
                "multiple kinds in the tenant (e.g. a `target` and a "
                "`vm` both named `app`); an ambiguous bare name returns "
                "-32602 naming the candidate kinds."
            ),
            "maxLength": 64,
        },
        "to_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `to_name` "
                "endpoint. Same contract as `from_node_kind`."
            ),
            "maxLength": 64,
        },
        "note": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional free-text annotation stored on "
                "`graph_edge.properties.note`. Use to record the "
                "operational rationale — 'Vault role `k8s-prod-read` "
                "binds to namespace `prod`; rotated 2026-04-22'."
            ),
        },
        "evidence_url": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional URL the operator attached as evidence "
                "(typically an INVENTORY.md anchor / runbook). Stored "
                "on `graph_edge.properties.evidence_url`."
            ),
        },
    },
    "required": ["from_name", "kind", "to_name"],
    "additionalProperties": False,
}

ANNOTATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edge_id": {"type": "string"},
        "from": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["id", "kind", "name"],
        },
        "to": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["id", "kind", "name"],
        },
        "kind": {"type": "string"},
        "source": {"type": "string"},
        "conflicts": {
            "type": "array",
            "items": {"type": "string"},
        },
        "superseded": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ids of the `source='auto'` edges this assertion displaced "
                "(§6 class 1 — same kind, different endpoint). The auto row "
                "is stamped `properties.superseded_by=<this edge id>` and "
                "drops out of traversal until this curated edge is removed. "
                "Empty on a pair no probe covers. Matches the audit / "
                "broadcast payload's `superseded` list exactly."
            ),
        },
    },
    "required": ["edge_id", "from", "to", "kind", "source", "conflicts", "superseded"],
}

UNANNOTATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edge_id": {
            "type": "string",
            "description": (
                "UUID of the curated `graph_edge` to remove. Mutually "
                "exclusive with the `(from_name, kind, to_name)` triple — "
                "pass exactly one selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
        "from_name": {
            "type": "string",
            "description": (
                "Triple selector: the edge's `from` endpoint name. Must "
                "appear together with `kind` and `to_name` (or with "
                "neither, when using `edge_id`)."
            ),
            "minLength": 1,
            "maxLength": 256,
        },
        "kind": {
            "type": "string",
            "pattern": KIND_SLUG_PATTERN,
            "minLength": KIND_SLUG_MIN_LENGTH,
            "maxLength": KIND_SLUG_MAX_LENGTH,
            "description": (
                "Triple selector: the edge's `graph_edge.kind` (any "
                "lowercase kind slug; the vocabulary is open). Must "
                "appear together with `from_name` and `to_name`."
            ),
        },
        "to_name": {
            "type": "string",
            "description": (
                "Triple selector: the edge's `to` endpoint name. Must "
                "appear together with `from_name` and `kind`."
            ),
            "minLength": 1,
            "maxLength": 256,
        },
        "from_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `from_name` "
                "endpoint, used for ambiguity disambiguation. Only "
                "meaningful with the triple selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
        "to_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `to_name` "
                "endpoint, used for ambiguity disambiguation. Only "
                "meaningful with the triple selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
    },
    "additionalProperties": False,
    # XOR at the wire boundary: either `edge_id` alone, or the full
    # `(from_name, kind, to_name)` triple. Partial triples, both
    # selectors, or neither are rejected by jsonschema (Draft 2020-12)
    # before reaching the service. The substrate-level XOR guard in
    # `unannotate_edge` stays as belt-and-suspenders for the
    # never-validated path (direct in-process callers).
    "oneOf": [
        {
            "required": ["edge_id"],
            "not": {
                "anyOf": [
                    {"required": ["from_name"]},
                    {"required": ["kind"]},
                    {"required": ["to_name"]},
                ],
            },
        },
        {
            "required": ["from_name", "kind", "to_name"],
            "not": {"required": ["edge_id"]},
        },
    ],
}

UNANNOTATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edge_id": {"type": "string"},
    },
    "required": ["edge_id"],
}

CREATE_NODE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "pattern": KIND_SLUG_PATTERN,
            "minLength": KIND_SLUG_MIN_LENGTH,
            "maxLength": KIND_SLUG_MAX_LENGTH,
            "description": (
                "Node kind: a lowercase slug (letters/digits joined "
                "by `.`, `_` or `-`; 2-63 chars). The vocabulary is "
                "open — any slug matching the pattern is accepted — "
                "but prefer a well-known kind when one fits: "
                + ", ".join(f"`{k}`" for k in _NODE_KIND_VALUES)
                + ". Novel kinds (`dns-record`, `keycloak-realm`, "
                "`database`, ...) are the right call when no "
                "well-known kind describes the resource class. "
                "Inner-graph kinds like `vault-role`, `vault-mount`, "
                "`principal` are the canonical use case: those rows "
                "cannot be auto-discovered (no probe walks the Vault "
                "policy tree as a topology source) and must be seeded "
                "manually before `meho.topology.annotate` can reference "
                "them."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` to create. Unique within "
                "`(tenant, kind, name)`; a repeat call with the same "
                "triple is idempotent (refreshes `last_seen` + merges "
                "manual-seed properties)."
            ),
        },
        "note": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional free-text annotation stored on "
                "`graph_node.properties.note`. Use to record the "
                "operational rationale for the manual seed — 'Vault "
                "role pinned by INVENTORY.md L42; rotated 2026-04-22'."
            ),
        },
        "evidence_url": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional URL the operator attached as evidence "
                "(typically an INVENTORY.md anchor / runbook). Stored "
                "on `graph_node.properties.evidence_url`."
            ),
        },
    },
    "required": ["kind", "name"],
    "additionalProperties": False,
}

CREATE_NODE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "node_id": {"type": "string"},
        "kind": {"type": "string"},
        "name": {"type": "string"},
        "source": {
            "type": "string",
            "enum": ["auto", "curated"],
            "description": (
                "Always `curated` after this call — a fresh seed "
                "inserts curated, a re-seed over a probe-discovered "
                "row promotes it (#2536)."
            ),
        },
        "was_created": {"type": "boolean"},
    },
    "required": ["node_id", "kind", "name", "source", "was_created"],
}


# ---------------------------------------------------------------------------
# Bulk import (#2539) — batch curated-edge authoring for the agent surface.
# ---------------------------------------------------------------------------

#: Boundary ceiling on the number of edge rows one ``meho.topology.
#: bulk_import`` call accepts. Mirrors the REST boundary cap
#: (``api/v1/topology._BULK_IMPORT_MAX_EDGES`` = 1000) so the two fronts
#: reject an oversized batch identically. The service layer
#: (:func:`~meho_backplane.topology.bulk_import.bulk_import_edges`) is
#: unbounded by design — the size guard belongs at each front boundary.
BULK_IMPORT_MAX_EDGES = 1000

#: Per-row shape is exactly one single-edge annotate's params
#: (``from_name`` / ``kind`` / ``to_name`` + the optional kind pins,
#: note, evidence_url). Reusing :data:`ANNOTATE_PARAMETER_SCHEMA`
#: verbatim keeps the row grammar and the single-edge grammar from
#: drifting — a bulk row is definitionally one annotate.
_BULK_IMPORT_ROWS_PROPERTY: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": BULK_IMPORT_MAX_EDGES,
    "items": ANNOTATE_PARAMETER_SCHEMA,
    "description": (
        "The edges to import, in source order. Each row is one "
        "`meho.topology.annotate` call's params: `from_name`, `kind`, "
        "`to_name` are required; `from_node_kind` / `to_node_kind` pin "
        "an ambiguous endpoint; `note` / `evidence_url` are optional "
        f"free text. Between 1 and {BULK_IMPORT_MAX_EDGES} rows — an "
        "oversized batch is rejected at the tool boundary before any "
        "service call runs. Both endpoints of every row must already "
        "exist as `graph_node` rows; seed them with "
        "`meho.topology.create_node` first."
    ),
}

#: Typed-op parameter schema (apply path only): the ``rows`` array with
#: no ``dry_run`` — the dispatched op is always the apply, so the
#: ``ApprovalRequest`` an agent parks carries exactly the batch to apply
#: and nothing else. The MCP front routes the free dry-run away from
#: dispatch, so ``dry_run`` never reaches this schema.
BULK_IMPORT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"rows": _BULK_IMPORT_ROWS_PROPERTY},
    "required": ["rows"],
    "additionalProperties": False,
}

#: MCP tool inputSchema: the ``rows`` array plus the ``dry_run`` toggle.
#: ``dry_run`` defaults to ``true`` — the safe, read-shaped plan is the
#: default; an agent opts into the gated apply explicitly with
#: ``dry_run=false``.
BULK_IMPORT_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": _BULK_IMPORT_ROWS_PROPERTY,
        "dry_run": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (the default), returns the per-row "
                "create/update/conflict plan without writing anything "
                "and without parking — the free, read-shaped propose "
                "step. When false, applies the whole batch atomically "
                "(all-or-nothing): a human tenant_admin executes "
                "immediately, an agent principal parks the batch as one "
                "`ApprovalRequest` for a human to approve."
            ),
        },
    },
    "required": ["rows"],
    "additionalProperties": False,
}

#: Response shape for both the dry-run plan and the applied result —
#: mirrors :class:`~meho_backplane.topology.bulk_import.BulkImportResult`
#: and the REST ``POST /edges/bulk`` body. ``edge_id`` is null on
#: dry-run rows (no row exists yet) and on the rare post-commit reload
#: miss; ``superseded`` / ``conflicts`` echo the §6 marker arrays.
BULK_IMPORT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dry_run": {"type": "boolean"},
        "created": {"type": "integer"},
        "updated": {"type": "integer"},
        "conflicts": {"type": "integer"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "conflict"],
                    },
                    "edge_id": {"type": ["string", "null"]},
                    "from_name": {"type": "string"},
                    "from_kind": {"type": "string"},
                    "to_name": {"type": "string"},
                    "to_kind": {"type": "string"},
                    "kind": {"type": "string"},
                    "superseded": {"type": "array", "items": {"type": "string"}},
                    "conflicts": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "index",
                    "action",
                    "edge_id",
                    "from_name",
                    "from_kind",
                    "to_name",
                    "to_kind",
                    "kind",
                    "superseded",
                    "conflicts",
                ],
            },
        },
    },
    "required": ["dry_run", "created", "updated", "conflicts", "rows"],
}

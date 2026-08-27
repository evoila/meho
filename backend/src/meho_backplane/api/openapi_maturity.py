# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``x-maturity`` OpenAPI extension injection (#2675).

Stamps the public OpenAPI document with feature-maturity tiers resolved
from :data:`~meho_backplane.features.FEATURE_MATURITY` — the propagation
surface the #2664 program requires on REST, mirroring the ``[beta]`` /
``[experimental]`` MCP description prefixes. Two placements, both plain
`specification extensions <https://spec.openapis.org/oas/v3.1.0#specification-extensions>`_
(legal on the Tag Object and the Operation Object alike):

* **Top-level ``tags``** — one entry per mapped route tag carrying
  ``x-maturity``. FastAPI's app is constructed without ``openapi_tags``,
  so this module owns the top-level ``tags`` array outright.
* **Per-operation ``x-maturity``** — only where a tag spans tiers
  (:data:`PATH_FEATURE_OVERRIDES`): today the ``connectors`` tag, whose
  lifecycle verbs follow ``typed_connector_reads`` (GA) while the
  spec-ingestion pipeline paths follow ``connector_ingest``
  (experimental) — the same split the MCP ``meho_connector_*`` tools
  encode.

Mapping maintenance
===================

:data:`TAG_FEATURE` is the REST twin of the per-tool ``feature`` field
on :class:`~meho_backplane.mcp.registry.ToolDefinition` and of
:data:`~meho_backplane.features._READY_ENTRY_FEATURE`. Tags absent
from it are **deliberately unclassified**: infrastructure surfaces
only (``health``, ``version``, ``mcp`` — the transport, whose tools
carry their own per-tool labels, ``discovery``'s untagged siblings
``/`` and ``/metrics``). The BFF ``/ui*`` tags are excluded wholesale
— the console's maturity surface is the #2677 badge chips, and the
#2678 drift guard (:mod:`tests.test_maturity_surface_drift`) excludes
``/ui/*`` by prefix until the #2662 public/BFF split lands. Every
other tag must map here — the drift guard is red otherwise, naming
this module as the file to edit. The #2678 decisions for the two tags
the provisional #2664 table left unclassified: ``conventions`` (the
preamble knowledge packer) is a face of the memory/knowledge plane →
``memory_knowledge``; ``runbooks`` drives writes through the run
driver → ``write_surfaces`` — both matching the /ui area mapping in
:mod:`meho_backplane.ui.maturity`. Values are validated against the
registry at import so a typo'd key fails at boot, not as a silently
missing label.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.features import FEATURE_MATURITY

__all__ = [
    "PATH_FEATURE_OVERRIDES",
    "TAG_FEATURE",
    "inject_maturity_extensions",
]

#: Route tag → :data:`FEATURE_MATURITY` key. See module docstring for
#: the deliberately-unmapped set.
TAG_FEATURE: Final[dict[str, str]] = {
    "agent-grants": "agent_runtime",
    "agent-principals": "agent_runtime",
    "agents": "agent_runtime",
    "approvals": "approvals",
    "audit": "audit",
    "auth": "auth_tenancy",
    "broadcast": "broadcast",
    "checks": "sensors",
    "checks-dashboards": "sensors",
    "connectors": "typed_connector_reads",
    "conventions": "memory_knowledge",
    "discovery": "auth_tenancy",
    "docs": "doc_collections",
    # The event_source registry (#2880) is the inbound producer side of the
    # event -> kind=event ScheduledTrigger substrate (Initiative #2877), so
    # it follows the scheduler feature it feeds.
    "event-sources": "scheduler",
    # The inbound ingest endpoint (#2881) is the runtime that consumes the
    # event_source registry and publishes to the event_outbox the scheduler
    # drains -- same substrate, same feature.
    "events": "scheduler",
    "feed": "broadcast",
    "gateway": "satellite_gateway",
    "knowledge": "memory_knowledge",
    "memory": "memory_knowledge",
    "operations": "typed_connector_reads",
    "retrieval": "memory_knowledge",
    "runbooks": "write_surfaces",
    "runner-principals": "satellite_gateway",
    "scheduler": "scheduler",
    "sensors": "sensors",
    # Standing scoped auto-approval grants for service principals (#3151 /
    # #3152) are the persistent form of an operator approve decision --
    # same substrate, same maturity tier as the approval queue.
    "service-principal-grants": "approvals",
    "targets": "targets",
    "topology": "topology",
}

#: Path → :data:`FEATURE_MATURITY` key, for paths whose feature differs
#: from their tag's (#2675 "per-path where a tag spans tiers"). Today:
#: the ``connectors`` tag's spec-ingestion pipeline paths, which follow
#: ``connector_ingest`` while the tag-level default follows the
#: lifecycle verbs' ``typed_connector_reads`` — the same per-verb split
#: the ``meho_connector_*`` MCP tools declare.
PATH_FEATURE_OVERRIDES: Final[dict[str, str]] = {
    "/api/v1/connectors/ingest": "connector_ingest",
    "/api/v1/connectors/ingest/jobs/{job_id}": "connector_ingest",
    "/api/v1/connectors/{connector_id}/review": "connector_ingest",
    "/api/v1/connectors/{connector_id}/groups/{group_key}": "connector_ingest",
    "/api/v1/connectors/{connector_id}/operations/{op_id}": "connector_ingest",
}


def _validate_mapping() -> None:
    """Fail at import on a mapping value the registry does not know.

    Same loud-pre-traffic posture as the MCP registry's duplicate-name
    guard: a typo'd feature key must never degrade to a silently
    missing maturity label.
    """
    unknown = {
        key
        for key in (*TAG_FEATURE.values(), *PATH_FEATURE_OVERRIDES.values())
        if key not in FEATURE_MATURITY
    }
    if unknown:
        raise RuntimeError(
            "openapi_maturity maps to feature keys absent from "
            f"FEATURE_MATURITY: {sorted(unknown)!r}",
        )


_validate_mapping()

#: HTTP methods that carry Operation Objects inside a Path Item. Path
#: Items also hold non-operation keys (``parameters``, ``summary``,
#: ``servers``), so iterating blindly over the dict would stamp
#: extensions onto non-operation nodes.
_OPERATION_METHODS: Final[frozenset[str]] = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace"),
)


def inject_maturity_extensions(schema: dict[str, Any]) -> None:
    """Stamp ``x-maturity`` onto *schema* in place.

    Top-level ``tags`` entries are emitted only for mapped tags that
    actually appear on an operation, so the document never advertises a
    tag with no routes. Per-operation stamps come exclusively from
    :data:`PATH_FEATURE_OVERRIDES`; an override path missing from the
    document raises — a stale override after a route rename must fail
    the snapshot-freshness gate loudly rather than silently un-label
    the path.
    """
    paths: dict[str, Any] = schema.get("paths", {})

    used_tags: set[str] = set()
    for path_item in paths.values():
        for method, operation in path_item.items():
            if method in _OPERATION_METHODS and isinstance(operation, dict):
                used_tags.update(operation.get("tags", ()))

    schema["tags"] = [
        {
            "name": tag,
            "x-maturity": FEATURE_MATURITY[TAG_FEATURE[tag]]["maturity"],
        }
        for tag in sorted(used_tags & TAG_FEATURE.keys())
    ]

    for path, feature in PATH_FEATURE_OVERRIDES.items():
        path_item = paths.get(path)
        if path_item is None:
            raise RuntimeError(
                f"PATH_FEATURE_OVERRIDES names {path!r} which is absent "
                "from the OpenAPI document — update the override map "
                "alongside the route change",
            )
        for method, operation in path_item.items():
            if method in _OPERATION_METHODS and isinstance(operation, dict):
                operation["x-maturity"] = FEATURE_MATURITY[feature]["maturity"]

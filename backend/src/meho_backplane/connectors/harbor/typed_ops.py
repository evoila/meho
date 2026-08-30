# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`HarborConnector` (#2856).

The audited Harbor 2.x read core is registered as typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog ingest** -- the #2247 failure class the older ingested-curation
enablement (retired ``harbor.core_ops.apply_harbor_core_curation``) was
subject to (per-deploy catalog state). The op bodies live in
:mod:`meho_backplane.connectors.harbor.typed_reads`; thin bound-method
shims on
:class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
expose them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
recovers each callable from its persisted ``module.ClassName.method``
path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.nsx.typed_ops` (#2302) and
:mod:`meho_backplane.connectors.sddc_manager.typed_ops` (#2306) -- the
same Task #2358 conversion this module applies to Harbor.

The 9 reads are the whole curated Harbor read core; there is no declined
ingested-browse remainder (unlike NSX, whose transport-node / segment
listings stayed ingested). The two robot **writes**
(``harbor.robot.create`` / ``.delete``) are registered separately in
:mod:`meho_backplane.connectors.harbor.ops` and are out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.harbor.typed_reads import HARBOR_READ_LLM_INSTRUCTIONS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "HARBOR_TYPED_OPS",
    "HARBOR_TYPED_WHEN_TO_USE_BY_GROUP",
    "HarborTypedOp",
    "register_harbor_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys -- the same curated groups the retired
# ``HARBOR_CORE_GROUPS`` named, so an agent's group-selection step reads
# the same taxonomy. The robot *write* ops use a separate ``robot`` group
# (see ``harbor.ops``); ``harbor-robots`` here is the read-only inventory.
_GROUP_SYSTEM = "harbor-system"
_GROUP_PROJECTS = "harbor-projects"
_GROUP_REPOSITORIES = "harbor-repositories"
_GROUP_ARTIFACTS = "harbor-artifacts"
_GROUP_ROBOTS = "harbor-robots"


@dataclass(frozen=True)
class HarborTypedOp:
    """Metadata for one Harbor typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_harbor_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on :class:`HarborConnector` exposing the async
    handler; the registrar resolves the bound method against the class at
    registration time. Mirrors
    :class:`~meho_backplane.connectors.nsx.typed_ops.NsxTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group. Moved verbatim from
#: the retired ``HARBOR_CORE_GROUPS`` (#2856); ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set.
HARBOR_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_SYSTEM: (
        "Use this group to read Harbor appliance-level information: the "
        "software version and auth mode (systeminfo), and the composite "
        "health status across DB, redis, registry, and jobservice subsystems. "
        "The probe surface the agent calls before any registry read or when "
        "confirming the Harbor appliance is reachable."
    ),
    _GROUP_PROJECTS: (
        "Use this group to list or inspect Harbor projects (namespaces). "
        "The entry point for any registry workflow: a project holds "
        "repositories which hold artifacts. Use to answer 'what projects "
        "exist on this registry', 'is project X public or private', or "
        "'how many repositories does project Y contain'."
    ),
    _GROUP_REPOSITORIES: (
        "Use this group to list or inspect repositories within a Harbor "
        "project. A repository groups all tags and digests for one image "
        "name. Use to answer 'what images exist in project X', 'how many "
        "pulls has image Y received', or to navigate to a specific "
        "repository before querying its artifacts."
    ),
    _GROUP_ARTIFACTS: (
        "Use this group to list or inspect artifacts (images, Helm charts, "
        "OCI artifacts) within a repository. Each artifact carries its "
        "tags, digest, push time, SBOM accessor, and signature status. "
        "Use when answering 'what tags exist for image X', 'what digest "
        "does tag Y resolve to', 'has this image been signed', or "
        "'does this artifact have an SBOM attached'."
    ),
    _GROUP_ROBOTS: (
        "Use this group to list Harbor robot accounts (service accounts "
        "used for CI/CD push and pull operations). Returns account names, "
        "permissions, expiry, and enabled status — never the account secret "
        "(secrets are only available at robot-creation time). Use when "
        "auditing which robots exist, checking expiry dates, or confirming "
        "a robot has the expected project permissions."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object shared by the no-argument reads (about / health).
_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

#: Optional list-narrowing query params Harbor's collection endpoints share.
_LIST_FILTER_PROPS: dict[str, Any] = {
    "q": {
        "type": "string",
        "description": "Harbor query filter, e.g. 'name=~ubuntu' (see Harbor 'q' syntax).",
    },
    "sort": {
        "type": "string",
        "description": "Sort expression, e.g. 'name' or '-creation_time'.",
    },
    "page": {"type": "integer", "minimum": 1, "description": "1-based page number."},
    "page_size": {"type": "integer", "minimum": 1, "description": "Rows per page."},
}

#: Optional artifact ``with_*`` include flags (each defaults false server-side).
_WITH_FLAG_PROPS: dict[str, Any] = {
    name: {"type": "boolean", "description": f"Include {label} in each returned artifact."}
    for name, label in (
        ("with_tag", "the tags"),
        ("with_scan_overview", "the vulnerability scan overview"),
        ("with_sbom_overview", "the SBOM overview"),
        ("with_signature", "the signature status"),
        ("with_label", "the labels"),
        ("with_accessory", "the accessories (SBOM / signature entries)"),
    )
}

_PROJECT_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Harbor project name, from harbor.project.list.",
}
_REPOSITORY_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Bare image name within the project (e.g. 'ubuntu', not 'library/ubuntu').",
}
_REFERENCE_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Tag name or 'sha256:…' digest identifying the artifact.",
}

#: Response schema shared by the bare-JSON-array list ops.
_ARRAY_RESPONSE: dict[str, Any] = {"type": "array", "items": {"type": "object"}}
#: Response schema shared by the single-object detail ops.
_OBJECT_RESPONSE: dict[str, Any] = {"type": "object", "additionalProperties": True}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build a closed (``additionalProperties=False``) object schema."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


#: The typed ops :class:`HarborConnector` registers at lifespan startup --
#: the audited read core (#2856). Ordered system -> projects -> repositories
#: -> artifacts -> robots to match the operator's typical drill-down path.
HARBOR_TYPED_OPS: tuple[HarborTypedOp, ...] = (
    HarborTypedOp(
        op_id="harbor.about",
        handler_attr="about",
        summary="Harbor appliance version, auth mode, and registry URL.",
        description=(
            "Returns the Harbor appliance system info via GET /api/v2.0/systeminfo: "
            "harbor_version, auth_mode (db_auth / ldap_auth / oidc_auth), registry_url, "
            "external_url, and the project-creation policy. The pre-flight probe read. "
            "Works with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SYSTEM,
        tags=("read-only", "harbor", "system"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.about"],
    ),
    HarborTypedOp(
        op_id="harbor.health",
        handler_attr="health",
        summary="Harbor composite health across all subsystems.",
        description=(
            "Returns Harbor's composite health via GET /api/v2.0/health across core, "
            "database, jobservice, redis, registry, and registryctl: an overall status "
            "plus a per-component components[] list. Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SYSTEM,
        tags=("read-only", "harbor", "system", "health"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.health"],
    ),
    HarborTypedOp(
        op_id="harbor.project.list",
        handler_attr="project_list",
        summary="Harbor project (namespace) inventory.",
        description=(
            "Lists Harbor projects via GET /api/v2.0/projects -- the registry-workflow "
            "entry point (projects hold repositories hold artifacts). Optional public "
            "filter + q / sort / page / page_size. Returns a bare JSON array; a large "
            "list is reduced to a JSONFlux handle. Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"public": {"type": "boolean"}, **_LIST_FILTER_PROPS}),
        response_schema=_ARRAY_RESPONSE,
        group_key=_GROUP_PROJECTS,
        tags=("read-only", "harbor", "project"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.project.list"],
    ),
    HarborTypedOp(
        op_id="harbor.project.info",
        handler_attr="project_info",
        summary="Full detail for one Harbor project.",
        description=(
            "Reads one Harbor project by name via GET /api/v2.0/projects/{project_name}: "
            "quota usage, repo count, chart count, and the metadata dict. Requires "
            "project_name. Works with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"project_name": _PROJECT_NAME_PROP}, ("project_name",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_PROJECTS,
        tags=("read-only", "harbor", "project"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.project.info"],
    ),
    HarborTypedOp(
        op_id="harbor.repository.list",
        handler_attr="repository_list",
        summary="Repositories within a Harbor project.",
        description=(
            "Lists repositories (image names) within a project via "
            "GET /api/v2.0/projects/{project_name}/repositories. Requires project_name; "
            "optional q / sort / page / page_size. Returns a bare JSON array reduced to a "
            "JSONFlux handle when large. Works with zero catalog ingest. safety_level=safe, "
            "read-only."
        ),
        parameter_schema=_schema(
            {"project_name": _PROJECT_NAME_PROP, **_LIST_FILTER_PROPS}, ("project_name",)
        ),
        response_schema=_ARRAY_RESPONSE,
        group_key=_GROUP_REPOSITORIES,
        tags=("read-only", "harbor", "repository"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.repository.list"],
    ),
    HarborTypedOp(
        op_id="harbor.repository.info",
        handler_attr="repository_info",
        summary="Full detail for one Harbor repository.",
        description=(
            "Reads one repository via "
            "GET /api/v2.0/projects/{project_name}/repositories/{repository_name}: pull "
            "count, artifact count, description, timestamps. Requires project_name + "
            "repository_name (the bare image name). Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema(
            {"project_name": _PROJECT_NAME_PROP, "repository_name": _REPOSITORY_NAME_PROP},
            ("project_name", "repository_name"),
        ),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_REPOSITORIES,
        tags=("read-only", "harbor", "repository"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.repository.info"],
    ),
    HarborTypedOp(
        op_id="harbor.artifact.list",
        handler_attr="artifact_list",
        summary="Artifacts (tags + digests) in a Harbor repository.",
        description=(
            "Lists artifacts via "
            ".../repositories/{repository_name}/artifacts: tags, digest, push time, and "
            "(with the matching with_* flag) scan overview / SBOM / signature / label / "
            "accessory data. Requires project_name + repository_name. Returns a bare JSON "
            "array reduced to a JSONFlux handle when large. Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema(
            {
                "project_name": _PROJECT_NAME_PROP,
                "repository_name": _REPOSITORY_NAME_PROP,
                **_LIST_FILTER_PROPS,
                **_WITH_FLAG_PROPS,
            },
            ("project_name", "repository_name"),
        ),
        response_schema=_ARRAY_RESPONSE,
        group_key=_GROUP_ARTIFACTS,
        tags=("read-only", "harbor", "artifact"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.artifact.list"],
    ),
    HarborTypedOp(
        op_id="harbor.artifact.info",
        handler_attr="artifact_info",
        summary="Full metadata for one Harbor artifact by tag or digest.",
        description=(
            "Reads one artifact via .../artifacts/{reference} where reference is a tag or "
            "'sha256:' digest: all tags, labels, and (with the matching with_* flag) the "
            "vulnerability scan overview, SBOM accessors, and signature status. Requires "
            "project_name + repository_name + reference. Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema(
            {
                "project_name": _PROJECT_NAME_PROP,
                "repository_name": _REPOSITORY_NAME_PROP,
                "reference": _REFERENCE_PROP,
                **_WITH_FLAG_PROPS,
            },
            ("project_name", "repository_name", "reference"),
        ),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_ARTIFACTS,
        tags=("read-only", "harbor", "artifact"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.artifact.info"],
    ),
    HarborTypedOp(
        op_id="harbor.robot.list",
        handler_attr="robot_list",
        summary="Harbor system-level robot accounts (never the secret).",
        description=(
            "Lists Harbor system-level robot accounts via GET /api/v2.0/robots: each "
            "robot's numeric id (needed for harbor.robot.delete), name, enabled status, "
            "expiry, and permissions. NEVER returns the robot secret -- Harbor only "
            "exposes it in the create-time POST response (#621). Optional q / sort / page / "
            "page_size. Returns a bare JSON array. Works with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema(dict(_LIST_FILTER_PROPS)),
        response_schema=_ARRAY_RESPONSE,
        group_key=_GROUP_ROBOTS,
        tags=("read-only", "harbor", "robot"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HARBOR_READ_LLM_INSTRUCTIONS["harbor.robot.list"],
    ),
)


async def register_harbor_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`HARBOR_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after every connector subpackage has been imported, so the
    descriptor rows land before the first dispatch. Idempotent across pod
    restarts (the helper skips the embedding recompute on unchanged summary /
    description / tags). Mirrors :func:`register_nsx_typed_operations` and the
    SDDC Manager typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should
    # not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in HARBOR_TYPED_OPS:
        handler = getattr(HarborConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"HarborConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = HARBOR_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"HarborConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                f"but no curated when_to_use exists for that key. Add an entry to "
                f"HARBOR_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=HarborConnector.product,
            version=HarborConnector.version,
            impl_id=HarborConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "harbor_typed_operations_registered",
        count=len(HARBOR_TYPED_OPS),
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor typed ops — robot lifecycle writes + standalone typed reads.

G3.5-T9 (#621) registered the two scoped-write robot-lifecycle ops.
#2857 added the standalone artifact CVE-detail read
``harbor.artifact.vulnerabilities`` via
:func:`register_harbor_artifact_operations`.
Wave-2 (#2858) adds two more standalone typed **reads** —
``harbor.project.summary`` (per-project storage-quota occupancy) and
``harbor.quota.list`` (fleet-wide project quotas) — via
:func:`register_harbor_project_quota_operations`. They register the same
way the robot ops do (independent of the deferred read-core ingest
state), landing in the ``harbor-projects`` operation group; their handlers
live on :class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
(``project_summary`` / ``quota_list``).

Robot ops — registers two scoped-write ops against ``connector_id="harbor-rest-2.x"``:

* ``harbor.robot.create`` — create a project-scoped robot account.
  Returns the minted secret in its response payload; classified
  ``credential_mint`` by :func:`~meho_backplane.broadcast.events.classify_op`
  so the broadcast collapses to aggregate-only and the secret never
  appears in the SSE stream.

* ``harbor.robot.delete`` — delete a project-scoped robot account by
  numeric ID. Classified ``write`` (suffix-based). No secret material
  in the response payload.

Both ops use non-retried HTTP calls (Harbor write endpoints are
non-idempotent). ``harbor.robot.create`` calls
:meth:`~meho_backplane.connectors.harbor.connector.HarborConnector._post_json`;
``harbor.robot.delete`` calls the pooled httpx client directly (DELETE
is not in ``_IDEMPOTENT_METHODS`` and ``HttpConnector`` provides no
``_delete_json`` helper — calling the client directly keeps the no-retry
contract explicit).

Handler methods live on
:class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
(``robot_create`` / ``robot_delete``) so the dispatcher's
:func:`~meho_backplane.operations.dispatcher._maybe_bind_method` can
bind them to the per-process connector instance at dispatch time.
This mirrors the bind9 / Kubernetes pattern for connectors whose
handlers need the HTTP transport.

Registration is triggered at lifespan startup via
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`;
the registrar is queued in
:mod:`~meho_backplane.connectors.harbor.__init__` alongside the
connector-registry entry.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "register_harbor_artifact_operations",
    "register_harbor_project_quota_operations",
    "register_harbor_robot_operations",
]


#: Curated ``when_to_use`` blurb for the Harbor ``robot`` group --
#: ``harbor.robot.create`` and ``harbor.robot.delete`` share one
#: ``OperationGroup`` row keyed on ``group_key="robot"`` (typed-
#: register first-write-wins, see
#: :func:`~meho_backplane.operations.typed_register._resolve_or_create_group`),
#: so both registrations pass the identical string. Surfaced verbatim
#: by ``list_operation_groups``.
#:
#: G0.9-T4b (#732) curated the per-group blurbs for the four typed
#: connectors shipped at v0.3.1 (bind9 / kubernetes / vault / vmware-
#: rest) but missed Harbor (the connector was new in v0.3.1). G0.9.1-T2
#: (#774) is the catch-up: source-side curation here plus the Alembic
#: ``0011`` data migration that backfills the same string onto v0.3.0-
#: era rows still carrying the kill-switched
#: ``"Operations grouped under 'robot' for harbor harbor-rest."``
#: auto-derive template. The text mirrors the entry under
#: ``("harbor", "2.x", "harbor-rest", "robot")`` in
#: :data:`~backend.alembic.versions.0011_backfill_operation_group_when_to_use._CURATED_WHEN_TO_USE`
#: so fresh-DB registrations and backfilled rows are byte-identical.
_HARBOR_ROBOT_WHEN_TO_USE: str = (
    "Use for project-scoped robot-account lifecycle on Harbor: "
    "mint a new robot credential (``harbor.robot.create`` -- "
    "the response carries a freshly-minted secret returned "
    "ONLY on creation, never again) and decommission an "
    "existing one by numeric id (``harbor.robot.delete``). "
    "Both ops are non-idempotent writes; create is "
    "credential_mint-classified so the minted secret never "
    "appears in the SSE broadcast. The right group when "
    "provisioning a CI/CD push/pull token, rotating an "
    "expired robot, or revoking machine access to a project. "
    "Read-only robot inventory (listing existing robots, "
    "checking expiry, auditing permissions) lives in the "
    "``harbor-robots`` group under the curated read core; "
    "this 'robot' group is the write surface. Pair with the "
    "``harbor-projects`` read group when the agent needs to "
    "confirm the target project exists before minting, and "
    "with ``harbor-robots`` when the post-mint audit step "
    "needs to verify the new robot landed with the expected "
    "permissions."
)


# ---------------------------------------------------------------------------
# harbor.robot.create
# ---------------------------------------------------------------------------

_HARBOR_ROBOT_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "^(?=.*\\S)[a-zA-Z0-9_-]+$",
            "description": (
                "Robot account name (alphanumeric, hyphens, underscores). "
                "Harbor prefixes it as 'robot$<project>+<name>' in the response."
            ),
        },
        "project": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Harbor project name that scopes this robot account.",
        },
        "duration": {
            "type": "integer",
            "description": (
                "Validity period in days. -1 means the account never expires. "
                "Prefer a short duration for temporary access; use -1 for "
                "long-lived CI credentials only."
            ),
            "default": -1,
        },
    },
    "required": ["name", "project", "duration"],
    "additionalProperties": False,
}

_HARBOR_ROBOT_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "integer",
            "description": "Harbor-assigned numeric robot account ID.",
        },
        "name": {
            "type": "string",
            "description": (
                "Harbor-formatted robot name (e.g. 'robot$project+name'). "
                "Use this as the username when authenticating with Harbor."
            ),
        },
        "secret": {
            "type": "string",
            "description": (
                "Minted robot credential. Returned ONLY on creation — "
                "Harbor does not expose it again after this call. "
                "Store it immediately and treat it as a password."
            ),
        },
    },
    "required": ["id", "name", "secret"],
}

_HARBOR_ROBOT_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a project-scoped robot account in Harbor. Use when the operator "
        "needs a machine credential (CI/CD push/pull token, automated deployment "
        "credential) for a specific Harbor project. "
        "IMPORTANT: The response payload contains a freshly-minted secret credential "
        "that is returned ONLY once and cannot be retrieved later. The caller receives "
        "the full response including the secret; the broadcast feed does NOT "
        "(credential_mint classification keeps it aggregate-only). "
        "Store the secret immediately after this call."
    ),
    "parameter_hints": {
        "name": (
            "Robot account name — alphanumeric, hyphens, underscores. "
            "Harbor prefixes it as 'robot$<project>+<name>' in the response; "
            "supply only the suffix (e.g. 'ci-push', not 'robot$myproject+ci-push')."
        ),
        "project": "Harbor project name. The robot is scoped to this project only.",
        "duration": (
            "Validity in days. -1 = never expires. "
            "Prefer a short duration (e.g. 90) for temporary access. "
            "Use -1 only for long-lived CI credentials."
        ),
    },
    "output_shape": (
        "On success: {id: <int>, name: 'robot$<project>+<name>', secret: '<minted-secret>'}. "
        "The secret is a one-time value — save it immediately. "
        "On failure: a connector_error OperationResult with "
        "extras.exception_class naming the failure class."
    ),
}


# ---------------------------------------------------------------------------
# harbor.robot.delete
# ---------------------------------------------------------------------------

_HARBOR_ROBOT_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Harbor project name that scopes the robot account.",
        },
        "id": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Numeric robot account ID (returned by harbor.robot.create). "
                "Harbor's delete endpoint is keyed on the numeric ID, not the name."
            ),
        },
    },
    "required": ["project", "id"],
    "additionalProperties": False,
}

_HARBOR_ROBOT_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "integer",
            "description": "Echo of the deleted robot account ID.",
        },
        "deleted": {
            "type": "boolean",
            "description": "Always true on success.",
        },
    },
    "required": ["id", "deleted"],
}

_HARBOR_ROBOT_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete a project-scoped robot account from Harbor by its numeric ID. "
        "Use when decommissioning a CI credential or revoking machine access "
        "to a Harbor project. Requires the numeric 'id' returned by "
        "harbor.robot.create — Harbor's delete endpoint is keyed on the ID, "
        "not the robot name. This operation is irreversible; the account and "
        "its associated secret are permanently removed."
    ),
    "parameter_hints": {
        "project": "Harbor project name that scopes the robot account.",
        "id": (
            "Numeric robot account ID from harbor.robot.create's response "
            "(the 'id' field). Not the Harbor-formatted name string."
        ),
    },
    "output_shape": (
        "On success: {id: <int>, deleted: true}. "
        "Harbor returns HTTP 200 with an empty body; the id echo is synthetic. "
        "On failure: a connector_error OperationResult with "
        "extras.exception_class naming the failure class."
    ),
}


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------


async def register_harbor_robot_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert harbor.robot.create and harbor.robot.delete into ``endpoint_descriptor``.

    Called once per process from the FastAPI lifespan via
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    Idempotent: a second call with unchanged descriptions is a no-op for
    the embedding pipeline (body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    Test seam: ``embedding_service`` lets fixtures inject a stub so
    chassis tests don't load the ONNX model.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.robot.create",
        handler=HarborConnector.robot_create,
        summary="Create a project-scoped robot account in Harbor.",
        description=(
            "Creates a project-scoped robot account via "
            "POST /api/v2.0/robots (Harbor v2 robot API, level=project). "
            "The response payload contains a freshly-minted secret credential "
            "returned ONLY on creation — Harbor does not expose it again. "
            "Classified credential_mint: the broadcast collapses to aggregate-only "
            "so the secret never appears in the SSE feed. "
            "Non-idempotent — never auto-retried by the HttpConnector "
            "tenacity decorator. safety_level=caution. "
            "requires_approval=True: minting a robot credential is privilege "
            "issuance, so it is four-eyes-gated (a second operator must approve) "
            "for every principal kind — the non-agent policy gate keys on "
            "requires_approval, not safety_level (#147)."
        ),
        parameter_schema=_HARBOR_ROBOT_CREATE_PARAMETER_SCHEMA,
        response_schema=_HARBOR_ROBOT_CREATE_RESPONSE_SCHEMA,
        group_key="robot",
        when_to_use=_HARBOR_ROBOT_WHEN_TO_USE,
        tags=["write", "credential-mint"],
        safety_level="caution",
        # Four-eyes on privilege issuance (#147). ``harbor.robot.create`` mints
        # a fresh robot credential in its response — a credential_mint op. The
        # non-agent policy gate (``_non_agent_verdict``) keys the verdict solely
        # on ``requires_approval`` (not ``safety_level``), so leaving this
        # ``False`` let a human ``tenant_admin`` mint a credential end-to-end
        # with no second-operator approval — the credential-mint arm of the
        # four-eyes bypass #128 set out to close. Flip to True so the dispatch
        # parks at ``awaiting_approval`` and a second operator must approve.
        requires_approval=True,
        llm_instructions=_HARBOR_ROBOT_CREATE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.robot.delete",
        handler=HarborConnector.robot_delete,
        summary="Delete a project-scoped robot account from Harbor.",
        description=(
            "Deletes a project-scoped robot account via "
            "DELETE /api/v2.0/robots/{id} (Harbor v2 robot API). "
            "Requires the numeric robot ID (returned by harbor.robot.create). "
            "Non-idempotent write — permanent removal. safety_level=caution."
        ),
        parameter_schema=_HARBOR_ROBOT_DELETE_PARAMETER_SCHEMA,
        response_schema=_HARBOR_ROBOT_DELETE_RESPONSE_SCHEMA,
        group_key="robot",
        when_to_use=_HARBOR_ROBOT_WHEN_TO_USE,
        tags=["write", "destructive"],
        safety_level="caution",
        # Left ungated (#147). ``harbor.robot.delete`` is a ``caution``-class
        # ``write`` (suffix-classified) that revokes access — it does NOT mint
        # or return any credential, so it is not part of the credential-mint
        # bypass this Task closes. It mirrors the bind9 precedent (#129), which
        # four-eyes-gated only the ``dangerous`` config-replacement ops and left
        # ``caution`` ops default-allow for humans. Revoking a robot is
        # recoverable (re-mint via ``harbor.robot.create``), so it does not clear
        # the four-eyes bar. Gating it would also break the deliberate
        # full-detail-broadcast contrast the credential_mint tests pin.
        requires_approval=False,
        llm_instructions=_HARBOR_ROBOT_DELETE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )


# ---------------------------------------------------------------------------
# harbor.artifact.vulnerabilities
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for the ``harbor-artifacts`` group.
#: ``harbor.artifact.vulnerabilities`` registers into the same group the
#: artifact list/info reads live under (the sibling harbor-read-core typed
#: promotion, #2856); ``register_typed_operation`` requires a non-empty
#: string whenever ``group_key`` is set, and typed-register group creation is
#: first-write-wins. Kept close to the sibling artifact-read blurb so the two
#: Harbor wave-2 tasks reconcile trivially at merge.
_HARBOR_ARTIFACTS_WHEN_TO_USE: str = (
    "Use this group to list or inspect artifacts (images, Helm charts, OCI "
    "artifacts) within a repository and to read an artifact's full "
    "vulnerability detail. Each artifact carries its tags, digest, push time, "
    "SBOM accessor, and signature status; harbor.artifact.vulnerabilities "
    "returns the per-CVE list (id, severity, affected package, installed and "
    "fixed-in version, description) that sits behind harbor.artifact.info's "
    "severity-count scan overview. The right group for 'what tags exist for "
    "image X', 'what digest does tag Y resolve to', 'has this image been "
    "signed', 'does this artifact have an SBOM', and 'which CVEs are in image "
    "X:tag / is CVE-NNNN present before I promote it'."
)

_HARBOR_ARTIFACT_VULN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Harbor project name (from harbor.project.list).",
        },
        "repository_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Bare repository/image name within the project (from "
                "harbor.repository.list) -- e.g. 'nginx', or 'team/nginx' for a "
                "nested repository. The project prefix is NOT repeated here."
            ),
        },
        "reference": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Artifact reference: a tag name (e.g. 'v1.2.3') or a digest "
                "('sha256:...'). A digest pins an exact artifact; a tag may move."
            ),
        },
    },
    "required": ["project_name", "repository_name", "reference"],
    "additionalProperties": False,
}

_HARBOR_ARTIFACT_VULN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {
            "type": ["string", "null"],
            "description": "Overall severity of the scan report (e.g. 'Critical').",
        },
        "scanner": {
            "type": ["object", "null"],
            "description": "Scanner provenance ({name, vendor, version}).",
        },
        "generated_at": {
            "type": ["string", "null"],
            "description": "When the scan report was generated (ISO-8601).",
        },
        "vulnerabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "package": {"type": ["string", "null"]},
                    "version": {"type": ["string", "null"]},
                    "fix_version": {"type": ["string", "null"]},
                    "severity": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "links": {"type": "array", "items": {"type": "string"}},
                },
            },
            "description": (
                "Per-CVE list. A large list is reduced to a JSONFlux handle "
                "with a bounded inline sample; the full set is reachable via "
                "result_query."
            ),
        },
    },
    "additionalProperties": True,
}

_HARBOR_ARTIFACT_VULN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read the full per-CVE vulnerability list for one Harbor artifact -- "
        "the detail behind harbor.artifact.info's scan_overview, which carries "
        "severity COUNTS only. Use when answering 'which CVEs are in image "
        "X:tag', 'is CVE-2024-NNNN present', or 'what is the fixed-in version "
        "for package P' before promoting an image. Requires project_name + "
        "repository_name + reference (a tag or sha256 digest) -- the same "
        "coordinates as harbor.artifact.info, one segment deeper. The artifact "
        "must already be scanned; an unscanned artifact returns a "
        "connector_error (Harbor 404)."
    ),
    "parameter_hints": {
        "project_name": "Harbor project name (from harbor.project.list).",
        "repository_name": (
            "Bare image name within the project (from harbor.repository.list); "
            "e.g. 'nginx', or 'team/nginx' for a nested repo. No project prefix."
        ),
        "reference": (
            "A tag (e.g. 'v1.2.3') or a digest ('sha256:...'). Digests pin an "
            "exact artifact; tags may move."
        ),
    },
    "output_shape": (
        "{severity (overall, e.g. 'Critical'), scanner {name, vendor, version}, "
        "generated_at, vulnerabilities: [{id (CVE/GHSA id), package, version, "
        "fix_version, severity, description, links[]}, ...]}. A large list is "
        "reduced to a JSONFlux handle with a bounded inline sample plus a "
        "fetch_more envelope; the full set is reachable via result_query."
    ),
    "next_step": (
        "For a named-CVE lookup against a reduced handle, drill in with "
        "result_query, e.g. SELECT id, severity, package, fix_version FROM "
        "result WHERE id = 'CVE-2024-3094'. For a promotion gate, filter on "
        "severity or on fix_version (non-empty = fixable). Pair with "
        "harbor.artifact.info for the at-a-glance severity counts."
    ),
}


async def register_harbor_artifact_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert harbor.artifact.vulnerabilities into ``endpoint_descriptor``.

    Standalone typed read (source_kind="typed") so it dispatches on a fresh
    boot with zero catalog ingest -- the same shape as ``harbor.robot.create``
    / ``.delete`` above and the sibling harbor-read-core promotion (#2856).
    Called once per process from the FastAPI lifespan via
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`;
    idempotent (the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    Test seam: ``embedding_service`` lets fixtures inject a stub so chassis
    tests don't load the ONNX model.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.artifact.vulnerabilities",
        handler=HarborConnector.artifact_vulnerabilities,
        summary="Read an artifact's per-CVE vulnerability list in Harbor.",
        description=(
            "Reads one artifact's full vulnerability report via "
            "GET /api/v2.0/projects/{project_name}/repositories/{repository_name}"
            "/artifacts/{reference}/additions/vulnerabilities (Harbor v2 artifact "
            "API, operationId getVulnerabilitiesAddition) with the "
            "X-Accept-Vulnerabilities header pinned to the current native report "
            "format 'application/vnd.security.vulnerability.report; version=1.1'. "
            "The detail behind harbor.artifact.info's scan_overview severity "
            "counts: each vulnerability is projected to {id, package, version, "
            "fix_version, severity, description, links}. safety_level=safe, "
            "read-only, no approval. A large CVE list is JSONFlux-reduced to a "
            "result handle (CLAUDE.md postulate 6)."
        ),
        parameter_schema=_HARBOR_ARTIFACT_VULN_PARAMETER_SCHEMA,
        response_schema=_HARBOR_ARTIFACT_VULN_RESPONSE_SCHEMA,
        group_key="harbor-artifacts",
        when_to_use=_HARBOR_ARTIFACTS_WHEN_TO_USE,
        tags=["read-only", "harbor", "artifact", "vulnerability", "security"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_HARBOR_ARTIFACT_VULN_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )


# ---------------------------------------------------------------------------
# harbor.project.summary + harbor.quota.list (#2858)
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for the ``harbor-projects`` group. Both wave-2
#: read ops register into the same group the typed project list/info
#: reads live under (the typed read core, #2856). Typed-register group
#: creation is first-write-wins, so ``register_harbor_typed_operations``
#: (queued first) sets the canonical blurb from
#: :data:`~meho_backplane.connectors.harbor.typed_ops.HARBOR_TYPED_WHEN_TO_USE_BY_GROUP`;
#: ``register_typed_operation`` still requires a non-empty string whenever
#: ``group_key`` is set, so this mirror is passed here and covers the
#: storage-quota reads.
_HARBOR_PROJECTS_WHEN_TO_USE: str = (
    "Use this group to list or inspect Harbor projects (namespaces) and to "
    "read their storage-quota occupancy. A project holds repositories which "
    "hold artifacts, and each project has a storage quota. Use to answer "
    "'what projects exist on this registry', 'is project X public or "
    "private', 'how many repositories does project Y contain', 'how much "
    "storage is project Z using vs. its hard limit' "
    "(harbor.project.summary), or 'which projects are near their storage "
    "quota' fleet-wide (harbor.quota.list)."
)


_HARBOR_PROJECT_SUMMARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Harbor project name (from harbor.project.list). Queried by "
                "name; a numeric-looking name is still resolved as a name via "
                "the X-Is-Resource-Name header, never as a project id."
            ),
        },
    },
    "required": ["project_name"],
    "additionalProperties": False,
}

_HARBOR_PROJECT_SUMMARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo_count": {
            "type": ["integer", "null"],
            "description": "Number of repositories under the project.",
        },
        "quota": {
            "type": "object",
            "description": "Storage quota. hard/used are resource maps keyed by resource name.",
            "properties": {
                "hard": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                    "description": "Hard limits, e.g. {storage: <bytes>}; storage -1 = unlimited.",
                },
                "used": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                    "description": "Used amounts, e.g. {storage: <bytes>}.",
                },
            },
        },
        "member_counts": {
            "type": "object",
            "description": "Per-role member counts for the project.",
            "properties": {
                "project_admin": {"type": ["integer", "null"]},
                "maintainer": {"type": ["integer", "null"]},
                "developer": {"type": ["integer", "null"]},
                "guest": {"type": ["integer", "null"]},
                "limited_guest": {"type": ["integer", "null"]},
            },
        },
    },
    "additionalProperties": False,
}

_HARBOR_PROJECT_SUMMARY_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read one Harbor project's storage-quota occupancy and membership "
        "summary. Use for a capacity pre-flight before onboarding a new push "
        "pipeline, or to diagnose a denied (quota-exceeded) push. This is "
        "where storage quota lives -- harbor.project.info's Project object "
        "does NOT carry a quota field. Requires project_name (from "
        "harbor.project.list)."
    ),
    "parameter_hints": {
        "project_name": "Harbor project name (from harbor.project.list).",
    },
    "output_shape": (
        "{repo_count, quota: {hard: {storage: <bytes>, ...}, used: {storage: "
        "<bytes>, ...}}, member_counts: {project_admin, maintainer, developer, "
        "guest, limited_guest}}. Storage is bytes; a hard storage of -1 means "
        "unlimited. Compare used.storage against hard.storage for pressure."
    ),
    "next_step": (
        "If used.storage is close to hard.storage (and hard is not -1), surface "
        "the storage pressure to the operator. For a fleet-wide 'which projects "
        "are near quota' view, call harbor.quota.list instead of looping this "
        "op per project."
    ),
}


_HARBOR_QUOTA_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sort": {
            "type": "string",
            "default": "-used.storage",
            "description": (
                "Sort key. '-used.storage' (default) lists the highest storage "
                "consumers first -- the 'near quota' answer. Use 'used.storage' "
                "for ascending, or 'hard.storage' / '-hard.storage' by limit."
            ),
        },
        "page": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "1-based page number for a large fleet.",
        },
        "page_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 50,
            "description": "Quotas per page (max 100). Default 50.",
        },
    },
    "additionalProperties": False,
}

_HARBOR_QUOTA_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quotas": {
            "type": "array",
            "description": (
                "Project quotas ordered per sort. The single set-shaped field: a "
                "large fleet is reduced to a JSONFlux result handle with a "
                "bounded inline sample."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["integer", "null"], "description": "Quota id."},
                    "ref": {
                        "type": ["object", "null"],
                        "description": (
                            "Project reference this quota belongs to ({id, name, owner_name})."
                        ),
                    },
                    "hard": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                        "description": "Hard limits, e.g. {storage: <bytes>}; -1 = unlimited.",
                    },
                    "used": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                        "description": "Used amounts, e.g. {storage: <bytes>}.",
                    },
                },
            },
        },
    },
    "additionalProperties": False,
}

_HARBOR_QUOTA_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List project storage quotas across the whole registry, sorted by "
        "usage. Use to answer 'which projects are near their storage quota' in "
        "one call instead of looping harbor.project.summary per project -- the "
        "capacity-planning and quota-audit entry point. Default sort "
        "'-used.storage' puts the fullest projects first."
    ),
    "parameter_hints": {
        "sort": (
            "Default '-used.storage' (fullest first). Other keys: 'used.storage', "
            "'hard.storage', '-hard.storage'."
        ),
        "page": "1-based page number for a large fleet.",
        "page_size": "Quotas per page (max 100, default 50).",
    },
    "output_shape": (
        "{quotas: [{id, ref: {id, name, owner_name}, hard: {storage: <bytes>}, "
        "used: {storage: <bytes>}}, ...]} ordered per sort. Storage is bytes; a "
        "hard storage of -1 means unlimited; ref.name is the project. The "
        "quotas list is reduced to a JSONFlux handle with a bounded inline "
        "sample plus a fetch_more envelope for a large fleet."
    ),
    "next_step": (
        "The head rows (default sort) are the projects nearest their quota -- "
        "surface those with used vs. hard storage. Against a reduced handle, "
        "drill in with result_query (e.g. filter on used.storage). For one "
        "project's detail, call harbor.project.summary."
    ),
}


async def register_harbor_project_quota_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert harbor.project.summary and harbor.quota.list into ``endpoint_descriptor``.

    Standalone typed reads (source_kind="typed") so they dispatch on a fresh
    boot with zero catalog ingest -- the same shape as ``harbor.robot.create``
    / ``.delete`` above and ``harbor.artifact.vulnerabilities`` (#2857). Called
    once per process from the FastAPI lifespan via
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`;
    idempotent (the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    Both ops register into the ``harbor-projects`` group (project-level
    reads); ``harbor.quota.list`` is a fleet-wide project-quota read, so it is
    discoverable next to the per-project ``harbor.project.summary`` there.

    Test seam: ``embedding_service`` lets fixtures inject a stub so chassis
    tests don't load the ONNX model.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.project.summary",
        handler=HarborConnector.project_summary,
        summary="Read a Harbor project's storage-quota occupancy and member counts.",
        description=(
            "Reads one project's storage-quota usage via "
            "GET /api/v2.0/projects/{project_name_or_id}/summary (Harbor v2 "
            "project API, operationId getProjectSummary) with the "
            "X-Is-Resource-Name header set so the path segment resolves as a "
            "project name. This is the storage quota that harbor.project.info "
            "does NOT carry -- the vendor Project object has no quota field. "
            "Projected to {repo_count, quota: {hard, used}, member_counts}; "
            "quota.hard.storage / quota.used.storage are bytes. "
            "safety_level=safe, read-only, no approval."
        ),
        parameter_schema=_HARBOR_PROJECT_SUMMARY_PARAMETER_SCHEMA,
        response_schema=_HARBOR_PROJECT_SUMMARY_RESPONSE_SCHEMA,
        group_key="harbor-projects",
        when_to_use=_HARBOR_PROJECTS_WHEN_TO_USE,
        tags=["read-only", "harbor", "project", "quota", "storage"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_HARBOR_PROJECT_SUMMARY_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.quota.list",
        handler=HarborConnector.quota_list,
        summary="List Harbor project storage quotas fleet-wide, sorted by usage.",
        description=(
            "Lists project storage quotas across the registry via "
            "GET /api/v2.0/quotas?reference=project&sort=<sort> (Harbor v2 quota "
            "API, operationId listQuotas). Default sort '-used.storage' returns "
            "the highest-usage projects first, answering 'which projects are "
            "near their storage quota' in one call instead of an N-way "
            "harbor.project.summary fan-out. Each Quota is projected to "
            "{id, ref, hard, used} under a 'quotas' key; storage is bytes. The "
            "quotas list is JSONFlux-reduced to a result handle for large fleets "
            "(CLAUDE.md postulate 6). safety_level=safe, read-only, no approval."
        ),
        parameter_schema=_HARBOR_QUOTA_LIST_PARAMETER_SCHEMA,
        response_schema=_HARBOR_QUOTA_LIST_RESPONSE_SCHEMA,
        group_key="harbor-projects",
        when_to_use=_HARBOR_PROJECTS_WHEN_TO_USE,
        tags=["read-only", "harbor", "quota", "storage", "capacity"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_HARBOR_QUOTA_LIST_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )

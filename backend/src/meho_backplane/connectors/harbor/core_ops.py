# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor 2.x read-only v0.2 core — curated operator-enabled subset.

This module names the **9 read-only Harbor operations** the G3.5
Harbor v0.2 ship enables out of the much larger Harbor 2.x REST corpus
the G0.7 spec-ingestion pipeline lands under
``connector_id="harbor-rest-2.x"``. The curation is two-layered:

* :data:`HARBOR_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug :func:`classify_harbor_op` assigns to Harbor
  ops; the ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`HARBOR_CORE_OPS` — the 9 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #368 and CLAUDE.md postulates 1-2, Harbor is **fully
generic-ingested**: the underlying ops are not registered in code,
they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_harbor_core_curation` against an existing ingested
connector.

``HARBOR_PRODUCT`` / ``HARBOR_VERSION`` / ``HARBOR_IMPL_ID`` note
-----------------------------------------------------------------

``HARBOR_PRODUCT = "harbor"`` is the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
from ``HARBOR_CONNECTOR_ID = "harbor-rest-2.x"``
(``head.split("-", 1)[0]`` where head is ``"harbor-rest"``). It is
the same as :attr:`HarborConnector.product` (``"harbor"``), so no
product-key discrepancy exists for Harbor (unlike the SDDC Manager
case where ``SddcManagerConnector.product="sddc-manager"`` but rows
carry ``product="sddc"``).

The 9 ops (paths cross-checked against Harbor 2.11 at
https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/):

1. ``GET:/api/v2.0/systeminfo`` — ``harbor.about`` — Harbor appliance
   system info (version, auth mode, registry URL).
2. ``GET:/api/v2.0/health`` — ``harbor.health`` — composite health
   covering DB, redis, registry, jobservice, and related subsystems.
3. ``GET:/api/v2.0/projects`` — ``harbor.project.list`` — project
   inventory (public and private, filtered by ``public`` query param).
4. ``GET:/api/v2.0/projects/{project_name}`` — ``harbor.project.info``
   — full project detail including quota, repo count, and metadata.
5. ``GET:/api/v2.0/projects/{project_name}/repositories`` —
   ``harbor.repository.list`` — repositories under a project.
6. ``GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}``
   — ``harbor.repository.info`` — repository detail (pull count,
   push time, description).
7. ``GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts``
   — ``harbor.artifact.list`` — artifacts in a repository with tag,
   digest, SBOM-presence, and signature metadata.
8. ``GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}``
   — ``harbor.artifact.info`` — full artifact metadata (all tags,
   labels, signature, vulnerability summary, SBOM accessors).
9. ``GET:/api/v2.0/robots`` — ``harbor.robot.list`` — system-level
   robot accounts. **Never returns the robot secret** — Harbor only
   returns ``secret`` in the create-time ``POST`` response (#621).

Path families and group_keys
-----------------------------

Harbor's REST paths are hierarchical. :data:`HARBOR_PATH_RULES` lists
the 6 rules in most-specific-first order so the ``startswith`` loop
in :func:`classify_harbor_op` terminates at the right group:

* ``/api/v2.0/systeminfo`` + ``/api/v2.0/health`` → ``harbor-system``.
* ``/api/v2.0/robots`` → ``harbor-robots``.
* ``/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts``
  (and deeper) → ``harbor-artifacts``.
* ``/api/v2.0/projects/{project_name}/repositories`` (and deeper,
  until the ``/artifacts`` suffix triggers the rule above) →
  ``harbor-repositories``.
* ``/api/v2.0/projects`` (and all templated subpaths not caught
  above) → ``harbor-projects``.

The rule ordering is load-bearing: ``/artifacts`` must precede
``/repositories`` which must precede ``/projects`` because each is a
prefix of the next. :func:`classify_harbor_op` documents the loop
contract.

Curation application
--------------------

:func:`apply_harbor_core_curation` is the operator-review-time
substrate call that makes exactly the 9 curated ops dispatchable.
Mirrors :func:`apply_sddc_core_curation` verbatim, threading the
"enable group but pin non-core ops disabled" needle via the
audit-log-driven operator-override exclusion.

Robot secret invariant
-----------------------

``GET:/api/v2.0/robots`` is the **only** robot endpoint in the read
v0.2 core. The Harbor 2.x API confirms that list responses never
include ``secret`` — that field appears only in the ``POST`` response
body at robot-creation time. Acceptance tests assert this invariant;
see :mod:`tests.test_connectors_harbor_core_ops` and
:mod:`tests.acceptance._harbor_canary_fixtures`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "HARBOR_CONNECTOR_ID",
    "HARBOR_CORE_GROUPS",
    "HARBOR_CORE_OPS",
    "HARBOR_IMPL_ID",
    "HARBOR_PATH_RULES",
    "HARBOR_PRODUCT",
    "HARBOR_VERSION",
    "HarborCoreGroup",
    "HarborCoreOp",
    "apply_harbor_core_curation",
    "classify_harbor_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: extracts from ``"harbor-rest-2.x"`` (first hyphen-segment of
#: impl_id ``"harbor-rest"``).
#:
#: Matches :attr:`HarborConnector.product` directly — no discrepancy
#: like the SDDC Manager case (``"sddc"`` vs ``"sddc-manager"``).
HARBOR_PRODUCT: Final[str] = "harbor"
HARBOR_VERSION: Final[str] = "2.x"
HARBOR_IMPL_ID: Final[str] = "harbor-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"harbor-rest-2.x"``.
HARBOR_CONNECTOR_ID: Final[str] = f"{HARBOR_IMPL_ID}-{HARBOR_VERSION}"


@dataclass(frozen=True, slots=True)
class HarborCoreGroup:
    """One curated operator-review entry for a Harbor operation group.

    ``group_key`` is the slug :func:`classify_harbor_op` emits.
    ``name`` is the operator-readable label ``meho connector review``
    renders. ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class HarborCoreOp:
    """One curated operator-review entry for a Harbor operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in the Harbor 2.x OpenAPI spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and :mod:`meho_backplane.connectors.nsx.core_ops` — same agent
    reads both surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for Harbor.
#:
#: **Order is load-bearing.** Each rule is checked via
#: ``path.startswith(prefix)``. More-specific prefixes must precede
#: less-specific ones to avoid a shorter prefix consuming a path that
#: belongs to a deeper group:
#:
#: * ``/artifacts`` before ``/repositories`` — artifact paths also
#:   start with the repository prefix.
#: * ``/repositories`` before ``/projects`` — repository paths also
#:   start with the project prefix.
#:
#: The template variable names (``{project_name}``, etc.) are literal
#: substrings of the rule strings so ``startswith`` comparisons against
#: ingested op_ids (which also carry the literal template var names)
#: resolve correctly.
HARBOR_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/v2.0/systeminfo", "harbor-system"),
    ("/api/v2.0/health", "harbor-system"),
    ("/api/v2.0/robots", "harbor-robots"),
    # Nested project hierarchy — most-specific first.
    (
        "/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts",
        "harbor-artifacts",
    ),
    ("/api/v2.0/projects/{project_name}/repositories", "harbor-repositories"),
    ("/api/v2.0/projects", "harbor-projects"),
)


def classify_harbor_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a Harbor op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`HARBOR_PATH_RULES` in order.

    Rule ordering guarantees that the most-specific prefix wins:
    a path like
    ``/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts``
    matches the ``harbor-artifacts`` rule before the broader
    ``/api/v2.0/projects/{project_name}/repositories`` rule can fire.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/api/v2.0/configurations``, ``/api/v2.0/users``); those rows
    are un-curated and stay ``is_enabled=False`` after
    :func:`apply_harbor_core_curation` runs.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in HARBOR_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 5 Harbor groups
#: the read-only v0.2 core spans. Every hint is one complete sentence
#: the agent reads verbatim — vague hints poison
#: ``search_operations`` ranking, per the ai_engineering pack.
HARBOR_CORE_GROUPS: Final[tuple[HarborCoreGroup, ...]] = (
    HarborCoreGroup(
        group_key="harbor-system",
        name="Harbor (system)",
        when_to_use=(
            "Use this group to read Harbor appliance-level information: the "
            "software version and auth mode (systeminfo), and the composite "
            "health status across DB, redis, registry, and jobservice subsystems. "
            "The probe surface the agent calls before any registry read or when "
            "confirming the Harbor appliance is reachable."
        ),
    ),
    HarborCoreGroup(
        group_key="harbor-projects",
        name="Harbor Projects",
        when_to_use=(
            "Use this group to list or inspect Harbor projects (namespaces). "
            "The entry point for any registry workflow: a project holds "
            "repositories which hold artifacts. Use to answer 'what projects "
            "exist on this registry', 'is project X public or private', or "
            "'how many repositories does project Y contain'."
        ),
    ),
    HarborCoreGroup(
        group_key="harbor-repositories",
        name="Harbor Repositories",
        when_to_use=(
            "Use this group to list or inspect repositories within a Harbor "
            "project. A repository groups all tags and digests for one image "
            "name. Use to answer 'what images exist in project X', 'how many "
            "pulls has image Y received', or to navigate to a specific "
            "repository before querying its artifacts."
        ),
    ),
    HarborCoreGroup(
        group_key="harbor-artifacts",
        name="Harbor Artifacts",
        when_to_use=(
            "Use this group to list or inspect artifacts (images, Helm charts, "
            "OCI artifacts) within a repository. Each artifact carries its "
            "tags, digest, push time, SBOM accessor, and signature status. "
            "Use when answering 'what tags exist for image X', 'what digest "
            "does tag Y resolve to', 'has this image been signed', or "
            "'does this artifact have an SBOM attached'."
        ),
    ),
    HarborCoreGroup(
        group_key="harbor-robots",
        name="Harbor Robot Accounts",
        when_to_use=(
            "Use this group to list Harbor robot accounts (service accounts "
            "used for CI/CD push and pull operations). Returns account names, "
            "permissions, expiry, and enabled status — never the account secret "
            "(secrets are only available at robot-creation time). Use when "
            "auditing which robots exist, checking expiry dates, or confirming "
            "a robot has the expected project permissions."
        ),
    ),
)


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
) -> dict[str, object]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys.

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops`
    and :mod:`meho_backplane.connectors.sddc_manager.core_ops` use so
    an agent crossing connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 9 curated read-only Harbor core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths cross-checked against Harbor 2.11 at
#: https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/.
HARBOR_CORE_OPS: Final[tuple[HarborCoreOp, ...]] = (
    HarborCoreOp(
        op_id="GET:/api/v2.0/systeminfo",
        group_key="harbor-system",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read Harbor appliance system info: version string, "
                "auth mode (db_auth / ldap_auth / oidc_auth), registry URL, "
                "and project-creation policy. Useful as a pre-flight probe "
                "before heavier registry reads or when confirming which Harbor "
                "instance you are connected to."
            ),
            output_shape=(
                "Object with harbor_version (e.g. 'v2.11.0'), auth_mode, "
                "registry_url, external_url, self_registration (bool), "
                "project_creation_restriction, and read_only (bool)."
            ),
            next_step=(
                "Cross-reference harbor_version with the supported_version_range "
                "to confirm compatibility; proceed to harbor.project.list for "
                "the registry inventory or harbor.health for subsystem status."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/health",
        group_key="harbor-system",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to check Harbor's composite health status across all "
                "subsystems: core, database, jobservice, redis, registry, and "
                "registryctl. Use before any write-path operation or when "
                "diagnosing a reachability or performance issue."
            ),
            output_shape=(
                "Object with overall status ('healthy' or 'unhealthy') and "
                "components[] — each entry carries name and status ('healthy' "
                "or 'unhealthy') plus an optional error field on unhealthy entries."
            ),
            next_step=(
                "If any component is unhealthy, surface its name and error "
                "to the operator. If all components are healthy, proceed with "
                "the intended registry operation."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects",
        group_key="harbor-projects",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list Harbor projects on this registry. Accepts a "
                "'public=true' query parameter to filter to public projects "
                "only. The primary inventory entry point for any registry "
                "workflow — all repositories and artifacts are scoped under "
                "a project. Supports pagination via 'page' and 'page_size' "
                "query parameters; large registries may return many projects."
            ),
            output_shape=(
                "Array of Project objects; each carries id, name, owner_name, "
                "creation_time, update_time, repo_count, and metadata (including "
                "the 'public' flag as a string 'true'/'false')."
            ),
            next_step=(
                "Pick a project name for harbor.project.info to get quota and "
                "full metadata, or pass it as project_name to "
                "harbor.repository.list to enumerate its images."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects/{project_name}",
        group_key="harbor-projects",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one Harbor project by name. "
                "Returns quota usage, repository count, chart count, and the "
                "full metadata dict including public flag and content trust, "
                "vulnerability scanning, and auto-scan settings. Requires "
                "a project_name path parameter obtained from harbor.project.list."
            ),
            output_shape=(
                "Project object with id, name, owner_name, repo_count, "
                "chart_count, metadata (public, enable_content_trust, "
                "auto_scan, severity), quota (used/hard storage in bytes), "
                "creation_time, and update_time."
            ),
            next_step=(
                "If quota.used is close to quota.hard, surface the storage "
                "pressure to the operator. Proceed to harbor.repository.list "
                "to enumerate the project's images."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects/{project_name}/repositories",
        group_key="harbor-repositories",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list repositories (image names) within a Harbor project. "
                "Requires project_name from harbor.project.list. Supports "
                "pagination via 'page' and 'page_size'; large projects may "
                "return many repositories. Large lists return a JSONFlux "
                "handle through the shared HandleStore."
            ),
            output_shape=(
                "Array of Repository objects; each carries id, name "
                "('{project}/{repo}' form), description, artifact_count, "
                "pull_count, and update_time."
            ),
            next_step=(
                "Pick a repository name for harbor.repository.info for full "
                "metadata, or pass project_name + repository_name to "
                "harbor.artifact.list to enumerate tags and digests."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}",
        group_key="harbor-repositories",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one Harbor repository by "
                "project_name and repository_name. Returns pull count, "
                "artifact count, description, and timestamps. The "
                "repository_name is the bare image name (without the project "
                "prefix), e.g. 'ubuntu' not 'library/ubuntu'."
            ),
            output_shape=(
                "Repository object with id, name ('{project}/{repo}'), "
                "description, artifact_count, pull_count, creation_time, "
                "and update_time."
            ),
            next_step=(
                "Proceed to harbor.artifact.list to enumerate the repository's "
                "tags, digests, and SBOM/signature status."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts",
        group_key="harbor-artifacts",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list artifacts in a Harbor repository. Returns all "
                "tags associated with each digest, the digest itself, push "
                "time, SBOM accessor presence, and signature status. Requires "
                "project_name and repository_name from prior list calls. "
                "Large repositories return a JSONFlux handle; use "
                "result_describe + result_query to navigate the full set."
            ),
            output_shape=(
                "Array of Artifact objects; each carries digest (sha256:…), "
                "tags[] (name + push_time), size, push_time, media_type, "
                "accessories[] (SBOM, signature, cosign entries), and "
                "addition_links (for detailed scan reports)."
            ),
            next_step=(
                "Pick a tag or digest as the reference for harbor.artifact.info "
                "to get the full metadata including vulnerability summary and "
                "all labels. Confirm SBOM presence via accessories[].type == "
                "'build.sbom'."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}",
        group_key="harbor-artifacts",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full metadata of one Harbor artifact by "
                "project_name, repository_name, and reference (a tag name "
                "or digest 'sha256:…'). Returns all tags, labels, "
                "vulnerability summary, SBOM accessors, signature status, "
                "and addition_links for detailed scan reports. Use when "
                "answering 'what vulnerabilities does image X:tag have', "
                "'is this artifact signed', or 'what SBOM is attached'."
            ),
            output_shape=(
                "Artifact object with digest, tags[], size, push_time, "
                "media_type, labels[], accessories[] (SBOM + signature entries), "
                "scan_overview (vulnerability counts by severity), and "
                "addition_links mapping to detailed report endpoints."
            ),
            next_step=(
                "Surface scan_overview severity counts to the operator; "
                "for a signed artifact, confirm the signature in accessories[] "
                "where type == 'notation.signature' or 'cosign.signature'. "
                "Cross-reference the digest with harbor.robot.list if "
                "investigating which robot pushed the artifact."
            ),
        ),
    ),
    HarborCoreOp(
        op_id="GET:/api/v2.0/robots",
        group_key="harbor-robots",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list Harbor system-level robot accounts. Returns "
                "account names, enabled status, expiry time, permissions, "
                "and creation metadata. **Never returns the robot secret** — "
                "Harbor only exposes the secret at robot-creation time (POST). "
                "Use when auditing which robots exist, checking for expired "
                "or near-expiry robots, or confirming a robot has the expected "
                "project-scoped pull/push permissions."
            ),
            output_shape=(
                "Array of Robot objects; each carries id, name "
                "('robot$name'), description, level ('system' or 'project'), "
                "expires_at (Unix timestamp, -1 = never expires), "
                "editable (bool), disable (bool), and permissions[] (each "
                "entry has resource, access, and namespace)."
            ),
            next_step=(
                "Surface any robot where disable=true or expires_at is "
                "within 30 days to the operator for rotation. For a "
                "project-scoped robot audit, cross-reference permissions[].namespace "
                "against harbor.project.list results."
            ),
        ),
    ),
)


async def apply_harbor_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 9-op read core against an ingested Harbor connector.

    Drives the substrate so that, after this call returns, exactly
    the 9 ops in :data:`HARBOR_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 5 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion — the same mechanism
    :func:`~meho_backplane.connectors.sddc_manager.core_ops.apply_sddc_core_curation`
    and :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`
    established:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`HARBOR_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`HARBOR_CORE_OPS`.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``harbor-rest-2.x`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the Harbor
    2.x spec before this helper applies).
    """
    payload = await review_service.get_review_payload(
        HARBOR_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in HARBOR_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                HARBOR_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "harbor_non_core_op_disabled",
                connector_id=HARBOR_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in HARBOR_CORE_GROUPS:
        await review_service.edit_group(
            HARBOR_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            HARBOR_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "harbor_core_group_enabled",
            connector_id=HARBOR_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in HARBOR_CORE_OPS:
        await review_service.edit_op(
            HARBOR_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "harbor_core_op_curated",
            connector_id=HARBOR_CONNECTOR_ID,
            op_id=op.op_id,
        )

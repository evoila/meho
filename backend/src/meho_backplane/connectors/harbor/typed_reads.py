# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`HarborConnector`.

The audited Harbor 2.x read core (#2856) is registered as typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog state** -- the #2247 failure class the older
ingested-curation enablement (retired ``harbor.core_ops``) was subject
to (per-deploy catalog state). Each function here is the body a thin
bound-method shim on
:class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.harbor.typed_ops`.

This mirrors the NSX (#2302) and SDDC Manager (#2306) conversions under
Task #2358: typed ops own the core surface, generic review owns ingested
breadth.

Every read is issued directly on the connector's own authenticated
client via :meth:`HttpConnector._get_json` -- no ``dispatch_child``, no
ingested descriptor. Harbor authenticates with HTTP Basic on every
request (no session establish / XSRF dance), so there is no session
cache to invalidate; a raw ``401`` propagates as
:class:`httpx.HTTPStatusError` to the dispatcher's credential-recovery
arm, which evicts the cached credentials via the connector's public
:meth:`HarborConnector.invalidate_credentials` hook (#2396) and
re-dispatches once.

The audited 9-read set (each ``METHOD:/path`` reconciled against the
pinned Harbor 2.12 spec on the operator's shelf by
:mod:`tests.test_connectors_harbor_spec_reconcile` (#2990); the path
templates live in :mod:`~meho_backplane.connectors.harbor._paths`):

* ``harbor.about`` -- ``GET /api/v2.0/systeminfo`` (appliance version,
  auth mode, registry URL).
* ``harbor.health`` -- ``GET /api/v2.0/health`` (composite subsystem
  health).
* ``harbor.project.list`` -- ``GET /api/v2.0/projects`` (project
  inventory; optional ``public`` / ``q`` / ``sort`` / ``page`` /
  ``page_size`` filters).
* ``harbor.project.info`` -- ``GET /api/v2.0/projects/{project_name}``
  (full project detail).
* ``harbor.repository.list`` --
  ``GET /api/v2.0/projects/{project_name}/repositories`` (repositories
  under a project; optional ``q`` / ``sort`` / ``page`` / ``page_size``).
* ``harbor.repository.info`` --
  ``GET /api/v2.0/projects/{project_name}/repositories/{repository_name}``
  (single repository detail).
* ``harbor.artifact.list`` --
  ``GET /api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts``
  (artifacts in a repository; optional ``with_*`` include flags +
  pagination).
* ``harbor.artifact.info`` --
  ``.../artifacts/{reference}`` (full artifact metadata; ``reference``
  is a tag or ``sha256:`` digest).
* ``harbor.robot.list`` -- ``GET /api/v2.0/robots`` (system-level robot
  accounts). **Never returns the robot secret** -- Harbor only exposes
  the secret in the create-time ``POST`` response (#621), so this
  handler returns Harbor's list payload verbatim and no secret can ride
  the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.harbor._paths import (
    _ARTIFACT_PATH,
    _ARTIFACTS_PATH,
    _HEALTH_PATH,
    _PROJECT_PATH,
    _PROJECT_REPOSITORIES_PATH,
    _PROJECTS_PATH,
    _REPOSITORY_PATH,
    _ROBOTS_PATH,
    _SYSTEMINFO_PATH,
    encode_segment,
    fill_path,
)
from meho_backplane.connectors.harbor.session import HarborTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.harbor.connector import HarborConnector

__all__ = [
    "HARBOR_READ_LLM_INSTRUCTIONS",
    "harbor_about_impl",
    "harbor_artifact_info_impl",
    "harbor_artifact_list_impl",
    "harbor_health_impl",
    "harbor_project_info_impl",
    "harbor_project_list_impl",
    "harbor_repository_info_impl",
    "harbor_repository_list_impl",
    "harbor_robot_list_impl",
]


def _query(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    """Forward the *keys* present (non-``None``) in *params* as a query dict, or ``None``.

    Presence-based (``params.get(key) is not None``), not truthiness-based,
    so a caller-supplied ``public=false`` or ``with_scan_overview=false``
    still forwards (httpx renders a Python ``bool`` as lowercase
    ``true``/``false``). An empty result collapses to ``None`` so
    :meth:`HttpConnector._get_json` omits the query string entirely.
    """
    query = {key: params[key] for key in keys if params.get(key) is not None}
    return query or None


async def harbor_about_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``harbor.about`` -- ``GET /api/v2.0/systeminfo``.

    Returns the Harbor appliance's ``GeneralInfo``: ``harbor_version``,
    ``auth_mode``, ``registry_url``, ``external_url``, and the
    project-creation policy. The same surface
    :meth:`HarborConnector.fingerprint` reads, exposed as an
    operator-callable typed op.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SYSTEMINFO_PATH, operator=operator)


async def harbor_health_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``harbor.health`` -- ``GET /api/v2.0/health``.

    Returns Harbor's composite health across ``core`` / ``database`` /
    ``jobservice`` / ``redis`` / ``registry`` / ``registryctl``: an overall
    ``status`` plus a per-component ``components[]`` list. The
    purpose-built reachability surface :meth:`HarborConnector.probe` reads,
    exposed as an operator-callable typed op.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _HEALTH_PATH, operator=operator)


async def harbor_project_list_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """``harbor.project.list`` -- ``GET /api/v2.0/projects``.

    Lists Harbor projects (namespaces), optionally narrowed by the
    ``public`` filter or narrowed/paged via ``q`` / ``sort`` / ``page`` /
    ``page_size``. Harbor returns a bare JSON array (no ``results`` /
    ``elements`` envelope); the dispatcher's JSONFlux reducer wraps a large
    array in a handle.
    """
    query = _query(params, ("public", "q", "sort", "page", "page_size"))
    raw = await connector._get_json(target, _PROJECTS_PATH, operator=operator, params=query)
    return cast("list[dict[str, Any]]", raw)


async def harbor_project_info_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``harbor.project.info`` -- ``GET /api/v2.0/projects/{project_name}``.

    Reads the full detail of one Harbor project by name: repo count and the
    metadata dict (public flag, content trust, auto-scan). The vendor
    ``Project`` object carries no quota field -- storage-quota usage lives on
    the summary endpoint (``harbor.project.summary``, #2858). Requires
    ``project_name`` from ``harbor.project.list``.
    """
    path = fill_path(_PROJECT_PATH, {"project_name_or_id": encode_segment(params["project_name"])})
    return await connector._get_json(target, path, operator=operator)


async def harbor_repository_list_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """``harbor.repository.list`` -- repositories under a Harbor project.

    ``GET /api/v2.0/projects/{project_name}/repositories`` -- the image
    names within a project. Requires ``project_name``; optional ``q`` /
    ``sort`` / ``page`` / ``page_size``. Returns a bare JSON array.
    """
    path = fill_path(
        _PROJECT_REPOSITORIES_PATH,
        {"project_name": encode_segment(params["project_name"])},
    )
    query = _query(params, ("q", "sort", "page", "page_size"))
    raw = await connector._get_json(target, path, operator=operator, params=query)
    return cast("list[dict[str, Any]]", raw)


async def harbor_repository_info_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``harbor.repository.info`` -- single Harbor repository detail.

    ``GET /api/v2.0/projects/{project_name}/repositories/{repository_name}``
    -- pull count, artifact count, description, and timestamps for one
    repository. ``repository_name`` is the bare image name (without the
    project prefix).
    """
    path = fill_path(
        _REPOSITORY_PATH,
        {
            "project_name": encode_segment(params["project_name"]),
            "repository_name": encode_segment(params["repository_name"]),
        },
    )
    return await connector._get_json(target, path, operator=operator)


async def harbor_artifact_list_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """``harbor.artifact.list`` -- artifacts in a Harbor repository.

    ``GET /api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts``
    -- each artifact's tags, digest, push time, and (when the matching
    ``with_*`` include flag is passed) scan overview / SBOM / signature /
    label / accessory data. Requires ``project_name`` + ``repository_name``;
    optional ``q`` / ``sort`` / ``page`` / ``page_size`` and the ``with_*``
    flags. Returns a bare JSON array.
    """
    path = fill_path(
        _ARTIFACTS_PATH,
        {
            "project_name": encode_segment(params["project_name"]),
            "repository_name": encode_segment(params["repository_name"]),
        },
    )
    query = _query(
        params,
        (
            "q",
            "sort",
            "page",
            "page_size",
            "with_tag",
            "with_scan_overview",
            "with_sbom_overview",
            "with_signature",
            "with_label",
            "with_accessory",
        ),
    )
    raw = await connector._get_json(target, path, operator=operator, params=query)
    return cast("list[dict[str, Any]]", raw)


async def harbor_artifact_info_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``harbor.artifact.info`` -- full metadata for one Harbor artifact.

    ``.../artifacts/{reference}`` where ``reference`` is a tag name or a
    ``sha256:`` digest. Returns all tags, labels, and (when the matching
    ``with_*`` include flag is passed) the vulnerability scan overview,
    SBOM accessors, and signature status.
    """
    path = fill_path(
        _ARTIFACT_PATH,
        {
            "project_name": encode_segment(params["project_name"]),
            "repository_name": encode_segment(params["repository_name"]),
            "reference": encode_segment(params["reference"]),
        },
    )
    query = _query(
        params,
        (
            "with_tag",
            "with_scan_overview",
            "with_sbom_overview",
            "with_signature",
            "with_label",
            "with_accessory",
        ),
    )
    return await connector._get_json(target, path, operator=operator, params=query)


async def harbor_robot_list_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """``harbor.robot.list`` -- ``GET /api/v2.0/robots``.

    Lists Harbor system-level robot accounts with each robot's numeric
    ``id`` (needed for ``harbor.robot.delete``), name, enabled status,
    expiry, and permissions. **Never returns the robot secret** -- Harbor
    only exposes the secret in the create-time ``POST`` response, so the
    list payload is returned verbatim and no secret can ride the result.
    Optional ``q`` / ``sort`` / ``page`` / ``page_size``. Returns a bare
    JSON array.
    """
    query = _query(params, ("q", "sort", "page", "page_size"))
    raw = await connector._get_json(target, _ROBOTS_PATH, operator=operator, params=query)
    return cast("list[dict[str, Any]]", raw)


# ---------------------------------------------------------------------------
# Per-op ``llm_instructions`` blobs.
#
# Moved verbatim (not rewritten) from the retired
# ``harbor.core_ops.HARBOR_CORE_OPS`` per #2856 -- the operator-reviewed
# curation that shipped with the Harbor v0.2 core. Co-located here with the
# read impls they describe (rather than inline in ``typed_ops``) so the
# per-op metadata table stays compact. The canonical three-key shape
# (``when_to_call`` / ``output_shape`` / ``next_step``) is unchanged; the
# prose already references the dot-form op ids the reads now register under.
# ---------------------------------------------------------------------------

_ABOUT_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read Harbor appliance system info: version string, "
        "auth mode (db_auth / ldap_auth / oidc_auth), registry URL, "
        "and project-creation policy. Useful as a pre-flight probe "
        "before heavier registry reads or when confirming which Harbor "
        "instance you are connected to."
    ),
    "output_shape": (
        "Object with harbor_version (e.g. 'v2.11.0'), auth_mode, "
        "registry_url, external_url, self_registration (bool), "
        "project_creation_restriction, and read_only (bool)."
    ),
    "next_step": (
        "Cross-reference harbor_version with the supported_version_range "
        "to confirm compatibility; proceed to harbor.project.list for "
        "the registry inventory or harbor.health for subsystem status."
    ),
}

_HEALTH_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to check Harbor's composite health status across all "
        "subsystems: core, database, jobservice, redis, registry, and "
        "registryctl. Use before any write-path operation or when "
        "diagnosing a reachability or performance issue."
    ),
    "output_shape": (
        "Object with overall status ('healthy' or 'unhealthy') and "
        "components[] — each entry carries name and status ('healthy' "
        "or 'unhealthy') plus an optional error field on unhealthy entries."
    ),
    "next_step": (
        "If any component is unhealthy, surface its name and error "
        "to the operator. If all components are healthy, proceed with "
        "the intended registry operation."
    ),
}

_PROJECT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list Harbor projects on this registry. Accepts a "
        "'public=true' query parameter to filter to public projects "
        "only. The primary inventory entry point for any registry "
        "workflow — all repositories and artifacts are scoped under "
        "a project. Supports pagination via 'page' and 'page_size' "
        "query parameters; large registries may return many projects."
    ),
    "output_shape": (
        "Array of Project objects; each carries id, name, owner_name, "
        "creation_time, update_time, repo_count, and metadata (including "
        "the 'public' flag as a string 'true'/'false')."
    ),
    "next_step": (
        "Pick a project name for harbor.project.info to get its full "
        "metadata, or harbor.project.summary for storage quota usage "
        "and member counts; or pass it as project_name to "
        "harbor.repository.list to enumerate its images."
    ),
}

_PROJECT_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read the full detail of one Harbor project by name. "
        "Returns the repository count and the full metadata dict "
        "including public flag and content trust, vulnerability "
        "scanning, and auto-scan settings. For storage quota usage "
        "(used vs. hard limit) and per-role member counts, call "
        "harbor.project.summary instead. Requires a project_name path "
        "parameter obtained from harbor.project.list."
    ),
    "output_shape": (
        "Project object with id, name, owner_name, repo_count, "
        "metadata (public, enable_content_trust, auto_scan, severity), "
        "creation_time, and update_time. The Project object carries no "
        "quota field — call harbor.project.summary for storage quota "
        "usage in bytes."
    ),
    "next_step": (
        "For storage quota usage against the hard limit, call "
        "harbor.project.summary. Proceed to harbor.repository.list "
        "to enumerate the project's images."
    ),
}

_REPOSITORY_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list repositories (image names) within a Harbor project. "
        "Requires project_name from harbor.project.list. Supports "
        "pagination via 'page' and 'page_size'; large projects may "
        "return many repositories. A large list is reduced to a "
        "JSONFlux handle with a bounded inline sample plus a "
        "``fetch_more`` envelope; page through with 'page' / "
        "'page_size' to read beyond the sample."
    ),
    "output_shape": (
        "Array of Repository objects; each carries id, name "
        "('{project}/{repo}' form), description, artifact_count, "
        "pull_count, and update_time."
    ),
    "next_step": (
        "Pick a repository name for harbor.repository.info for full "
        "metadata, or pass project_name + repository_name to "
        "harbor.artifact.list to enumerate tags and digests."
    ),
}

_REPOSITORY_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read the full detail of one Harbor repository by "
        "project_name and repository_name. Returns pull count, "
        "artifact count, description, and timestamps. The "
        "repository_name is the bare image name (without the project "
        "prefix), e.g. 'ubuntu' not 'library/ubuntu'."
    ),
    "output_shape": (
        "Repository object with id, name ('{project}/{repo}'), "
        "description, artifact_count, pull_count, creation_time, "
        "and update_time."
    ),
    "next_step": (
        "Proceed to harbor.artifact.list to enumerate the repository's "
        "tags, digests, and SBOM/signature status."
    ),
}

_ARTIFACT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list artifacts in a Harbor repository. Returns all "
        "tags associated with each digest, the digest itself, push "
        "time, SBOM accessor presence, and signature status. Requires "
        "project_name and repository_name from prior list calls. "
        "A large repository is reduced to a JSONFlux handle with a "
        "bounded inline sample plus a ``fetch_more`` envelope; page "
        "through with 'page' / 'page_size' to read beyond the sample."
    ),
    "output_shape": (
        "Array of Artifact objects; each carries digest (sha256:…), "
        "tags[] (name + push_time), size, push_time, media_type, and "
        "accessories[] (SBOM, signature, cosign entries). Per-artifact "
        "CVE detail is available via harbor.artifact.vulnerabilities."
    ),
    "next_step": (
        "Pick a tag or digest as the reference for harbor.artifact.info "
        "to get the full metadata including vulnerability summary and "
        "all labels. Confirm SBOM presence via accessories[].type == "
        "'build.sbom'."
    ),
}

_ARTIFACT_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read the full metadata of one Harbor artifact by "
        "project_name, repository_name, and reference (a tag name "
        "or digest 'sha256:…'). Returns all tags, labels, "
        "vulnerability summary (severity counts), SBOM accessors, and "
        "signature status. For the per-CVE vulnerability list (id, "
        "severity, package, fixed-in version) behind the scan, call "
        "harbor.artifact.vulnerabilities. Use when "
        "answering 'what vulnerabilities does image X:tag have', "
        "'is this artifact signed', or 'what SBOM is attached'."
    ),
    "output_shape": (
        "Artifact object with digest, tags[], size, push_time, "
        "media_type, labels[], accessories[] (SBOM + signature entries), "
        "and scan_overview (vulnerability counts by severity). The "
        "per-CVE detail behind those counts lives in "
        "harbor.artifact.vulnerabilities."
    ),
    "next_step": (
        "Surface scan_overview severity counts to the operator, then "
        "call harbor.artifact.vulnerabilities for the per-CVE list when "
        "a named-CVE or fixed-in-version answer is needed. For a signed "
        "artifact, confirm the signature in accessories[] "
        "where type == 'notation.signature' or 'cosign.signature'. "
        "Cross-reference the digest with harbor.robot.list if "
        "investigating which robot pushed the artifact."
    ),
}

_ROBOT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list Harbor system-level robot accounts. Returns "
        "account names, enabled status, expiry time, permissions, "
        "and creation metadata. **Never returns the robot secret** — "
        "Harbor only exposes the secret at robot-creation time (POST). "
        "Use when auditing which robots exist, checking for expired "
        "or near-expiry robots, or confirming a robot has the expected "
        "project-scoped pull/push permissions."
    ),
    "output_shape": (
        "Array of Robot objects; each carries id, name "
        "('robot$name'), description, level ('system' or 'project'), "
        "expires_at (Unix timestamp, -1 = never expires), "
        "editable (bool), disable (bool), and permissions[] (each "
        "entry has resource, access, and namespace)."
    ),
    "next_step": (
        "Surface any robot where disable=true or expires_at is "
        "within 30 days to the operator for rotation. For a "
        "project-scoped robot audit, cross-reference permissions[].namespace "
        "against harbor.project.list results."
    ),
}

#: Per-op ``llm_instructions`` keyed by the dot-form op id, consumed by
#: :mod:`meho_backplane.connectors.harbor.typed_ops`.
HARBOR_READ_LLM_INSTRUCTIONS: dict[str, dict[str, object]] = {
    "harbor.about": _ABOUT_INSTRUCTIONS,
    "harbor.health": _HEALTH_INSTRUCTIONS,
    "harbor.project.list": _PROJECT_LIST_INSTRUCTIONS,
    "harbor.project.info": _PROJECT_INFO_INSTRUCTIONS,
    "harbor.repository.list": _REPOSITORY_LIST_INSTRUCTIONS,
    "harbor.repository.info": _REPOSITORY_INFO_INSTRUCTIONS,
    "harbor.artifact.list": _ARTIFACT_LIST_INSTRUCTIONS,
    "harbor.artifact.info": _ARTIFACT_INFO_INSTRUCTIONS,
    "harbor.robot.list": _ROBOT_LIST_INSTRUCTIONS,
}

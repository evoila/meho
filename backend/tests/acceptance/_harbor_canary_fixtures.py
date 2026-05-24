# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.5 Harbor dispatch tests.

Two Harbor acceptance modules (dispatch smoke + JSONFlux force-handle)
share the same plumbing: a registered
:class:`~meho_backplane.connectors.harbor.HarborConnector` instance with a
stub credentials loader (so no Vault read is required), a probed
:class:`~meho_backplane.db.models.Target` row, the 9 curated
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`~meho_backplane.connectors.harbor.core_ops.HARBOR_CORE_OPS`, and a
:mod:`respx`-mocked Harbor REST surface answering each of the 9 curated
read ops.

Harbor uses HTTP Basic auth on every request — no session establish
or XSRF-token dance is needed. The stub credentials loader bypasses the
Vault-backed loader; the respx router matches requests by path.

Robot secret invariant
======================

The Harbor 2.x API guarantees that ``GET /api/v2.0/robots`` never returns
a ``secret`` field — the secret is only returned at robot-creation time
(``POST /api/v2.0/robots`` in #621). :data:`HARBOR_CANARY_ROBOTS` is
deliberately constructed with no ``secret`` key on any entry so the
dispatch smoke and JSONFlux force-handle tests exercise the same invariant
the acceptance bar requires.

Why a minimal direct-insert path (not full G0.7 canary ingest)
==============================================================

The full Harbor 2.x spec ingest via :class:`IngestionPipelineService`
needs the Harbor OpenAPI spec reachable on the CI runner plus a live LLM
for the grouping pass. Until the spec-shelf is wired to the meho-runners
pool, the dispatch leg is exercised against a minimal direct-insert path
that seeds the 9 curated endpoint_descriptor rows by hand. Same pattern
:mod:`tests.acceptance._nsx_canary_fixtures` and
:mod:`tests.acceptance._sddc_canary_fixtures` established.

``EndpointDescriptor.product`` note
====================================

Rows are inserted with ``product=HARBOR_PRODUCT="harbor"`` — the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives from
``"harbor-rest-2.x"`` (first hyphen-segment of impl_id). The
:class:`Target` row also uses ``product="harbor"`` so the resolver finds
:class:`HarborConnector` (registered with ``product="harbor"`` in the v2
registry). Unlike the SDDC Manager case, no product-key discrepancy exists
for Harbor.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import (
    HARBOR_CONNECTOR_ID,
    HARBOR_CORE_GROUPS,
    HARBOR_CORE_OPS,
    HARBOR_IMPL_ID,
    HARBOR_PRODUCT,
    HARBOR_VERSION,
    HarborConnector,
    HarborTargetLike,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "HARBOR_CANARY_ARTIFACTS",
    "HARBOR_CANARY_BASE_URL",
    "HARBOR_CANARY_FINGERPRINT",
    "HARBOR_CANARY_OPERATOR_TENANT",
    "HARBOR_CANARY_PROJECTS",
    "HARBOR_CANARY_REPOSITORIES",
    "HARBOR_CANARY_ROBOTS",
    "HARBOR_FORCE_HANDLE_LIST_OP_ID",
    "HARBOR_TARGET_NAME",
    "IngestedHarborCanary",
    "harbor_acceptance_operator",
    "ingested_harbor_canary",
]

#: Tenant the Harbor dispatch tests act under.
HARBOR_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fd")

#: Stable :class:`Target.name` for the seeded Harbor target.
HARBOR_TARGET_NAME: str = "harbor-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress fires.
HARBOR_CANARY_BASE_URL: str = "https://harbor-canary.test.invalid"

#: Persisted as ``Target.fingerprint`` so the resolver binds
#: :class:`HarborConnector` (``supported_version_range=">=2.0,<3.0"``).
HARBOR_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="harbor",
    version="v2.11.0",
    build="canary1234",
    reachable=True,
    probed_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
    probe_method="GET /api/v2.0/systeminfo",
    extras={
        "auth_mode": "db_auth",
        "registry_url": "harbor-canary.test.invalid",
        "external_url": "https://harbor-canary.test.invalid",
    },
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Artifact list is
#: the largest surface in a real Harbor deployment (many tags / digests per
#: repository), mirroring the NSX segment-list and SDDC host-list choices.
HARBOR_FORCE_HANDLE_LIST_OP_ID: str = (
    "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts"
)

#: Smoke-test path parameters for ``HARBOR_FORCE_HANDLE_LIST_OP_ID``.
HARBOR_FORCE_HANDLE_PARAMS: dict[str, str] = {
    "project_name": "library",
    "repository_name": "ubuntu",
}

#: Synthetic system info response.
HARBOR_CANARY_SYSTEMINFO: dict[str, object] = {
    "auth_mode": "db_auth",
    "external_url": "https://harbor-canary.test.invalid",
    "harbor_version": "v2.11.0-canary1234",
    "has_ca_root": False,
    "primary_auth_mode": False,
    "project_creation_restriction": "adminonly",
    "read_only": False,
    "registry_url": "harbor-canary.test.invalid",
    "self_registration": False,
    "with_notary": False,
}

#: Synthetic health response — all components healthy.
HARBOR_CANARY_HEALTH: dict[str, object] = {
    "status": "healthy",
    "components": [
        {"name": "core", "status": "healthy"},
        {"name": "database", "status": "healthy"},
        {"name": "jobservice", "status": "healthy"},
        {"name": "redis", "status": "healthy"},
        {"name": "registry", "status": "healthy"},
        {"name": "registryctl", "status": "healthy"},
    ],
}

#: Synthetic project list — one public project ("library").
HARBOR_CANARY_PROJECTS: list[dict[str, object]] = [
    {
        "id": 1,
        "name": "library",
        "owner_name": "admin",
        "creation_time": "2026-01-01T00:00:00.000Z",
        "update_time": "2026-01-01T00:00:00.000Z",
        "repo_count": 3,
        "registry_id": None,
        "metadata": {"public": "true", "auto_scan": "true"},
        "quota": {
            "used": {"storage": 1073741824},
            "hard": {"storage": -1},
        },
    }
]

#: Synthetic project detail for "library".
HARBOR_CANARY_PROJECT_DETAIL: dict[str, object] = {
    "id": 1,
    "name": "library",
    "owner_name": "admin",
    "creation_time": "2026-01-01T00:00:00.000Z",
    "update_time": "2026-01-01T00:00:00.000Z",
    "repo_count": 3,
    "chart_count": 0,
    "metadata": {
        "public": "true",
        "enable_content_trust": "false",
        "auto_scan": "true",
        "severity": "low",
        "reuse_sys_cve_allowlist": "true",
        "prevent_vul": "false",
        "retention_id": None,
    },
    "quota": {
        "used": {"storage": 1073741824},
        "hard": {"storage": -1},
    },
}

#: Synthetic repository list for "library" — 3 repos covering the common
#: pulled library images.
HARBOR_CANARY_REPOSITORIES: list[dict[str, object]] = [
    {
        "id": i + 1,
        "name": f"library/{name}",
        "description": f"Official {name} image.",
        "artifact_count": 5,
        "pull_count": (i + 1) * 100,
        "creation_time": "2026-01-01T00:00:00.000Z",
        "update_time": "2026-05-01T00:00:00.000Z",
    }
    for i, name in enumerate(["ubuntu", "alpine", "nginx"])
]

#: Synthetic repository detail for "library/ubuntu".
HARBOR_CANARY_REPOSITORY_DETAIL: dict[str, object] = {
    "id": 1,
    "name": "library/ubuntu",
    "description": "Official Ubuntu image.",
    "artifact_count": 5,
    "pull_count": 100,
    "creation_time": "2026-01-01T00:00:00.000Z",
    "update_time": "2026-05-01T00:00:00.000Z",
}

#: Synthetic artifact list for "library/ubuntu" — 5 artifacts (tagged releases).
#: Each artifact has tags, digest, size, and accessories; no `secret` field
#: appears at any level (robot-secret invariant applies at the robot endpoint,
#: not here, but the fixture is constructed cleanly regardless).
HARBOR_CANARY_ARTIFACTS: list[dict[str, object]] = [
    {
        "digest": f"sha256:{'a' * 63}{i}",
        "tags": [
            {
                "name": tag,
                "push_time": "2026-04-01T00:00:00.000Z",
                "pull_time": "2026-05-01T00:00:00.000Z",
                "immutable": False,
            }
            for tag in tags
        ],
        "size": 30 * 1024 * 1024,
        "push_time": "2026-04-01T00:00:00.000Z",
        "pull_time": "2026-05-01T00:00:00.000Z",
        "media_type": "application/vnd.docker.distribution.manifest.v2+json",
        "accessories": [
            {
                "type": "build.sbom",
                "digest": f"sha256:{'b' * 63}{i}",
                "size": 4096,
                "creation_time": "2026-04-01T00:00:00.000Z",
            }
        ],
        "labels": [],
        "addition_links": {
            "build_history": {
                "href": (
                    f"/api/v2.0/projects/library/repositories/ubuntu"
                    f"/artifacts/sha256:{'a' * 63}{i}/additions/build_history"
                )
            },
            "vulnerabilities": {
                "href": (
                    f"/api/v2.0/projects/library/repositories/ubuntu"
                    f"/artifacts/sha256:{'a' * 63}{i}/additions/vulnerabilities"
                )
            },
        },
    }
    for i, tags in enumerate(
        [
            ["22.04", "jammy"],
            ["20.04", "focal"],
            ["24.04", "noble"],
            ["latest"],
            ["rolling"],
        ]
    )
]

#: Synthetic artifact detail for "library/ubuntu:latest".
HARBOR_CANARY_ARTIFACT_DETAIL: dict[str, object] = {
    "digest": "sha256:" + "a" * 63 + "3",
    "tags": [{"name": "latest", "push_time": "2026-04-01T00:00:00.000Z", "immutable": False}],
    "size": 30 * 1024 * 1024,
    "push_time": "2026-04-01T00:00:00.000Z",
    "media_type": "application/vnd.docker.distribution.manifest.v2+json",
    "accessories": [
        {
            "type": "build.sbom",
            "digest": "sha256:" + "b" * 63 + "3",
            "size": 4096,
            "creation_time": "2026-04-01T00:00:00.000Z",
        },
        {
            "type": "notation.signature",
            "digest": "sha256:" + "c" * 63 + "3",
            "size": 1024,
            "creation_time": "2026-04-01T00:00:00.000Z",
        },
    ],
    "labels": [{"name": "approved", "color": "#28a745"}],
    "scan_overview": {
        "application/vnd.security.vulnerability.report; version=1.1": {
            "scan_status": "Success",
            "severity": "Low",
            "summary": {"total": 12, "fixable": 3, "summary": {"Low": 8, "Medium": 4}},
        }
    },
    "addition_links": {
        "vulnerabilities": {
            "href": "/api/v2.0/projects/library/repositories/ubuntu/artifacts/sha256:"
            + "a" * 63
            + "3/additions/vulnerabilities"
        }
    },
}

#: Synthetic robot list — 3 system robots, **no** ``secret`` field anywhere.
#: This is the canonical shape ``GET /api/v2.0/robots`` returns; Harbor never
#: includes the secret in list responses (only at robot-creation time).
HARBOR_CANARY_ROBOTS: list[dict[str, object]] = [
    {
        "id": i + 1,
        "name": f"robot${name}",
        "description": f"CI/CD robot for {purpose}.",
        "level": "system",
        "disable": False,
        "expires_at": -1,
        "editable": True,
        "creation_time": "2026-01-15T00:00:00.000Z",
        "update_time": "2026-01-15T00:00:00.000Z",
        "permissions": [
            {
                "resource": "repository",
                "access": [{"action": "push"}, {"action": "pull"}],
                "namespace": "library",
            }
        ],
    }
    for i, (name, purpose) in enumerate(
        [
            ("ci-pusher", "image pushes"),
            ("cd-puller", "deployment pulls"),
            ("scanner-bot", "vulnerability scanning"),
        ]
    )
]


@dataclass(frozen=True)
class IngestedHarborCanary:
    """Bundle returned by :func:`ingested_harbor_canary`."""

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


async def _insert_harbor_descriptors() -> None:
    """Seed the 9 curated Harbor core ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in :data:`HARBOR_CORE_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per
    entry in :data:`HARBOR_CORE_OPS` (``is_enabled=True``,
    ``source_kind='ingested'``, ``handler_ref=None``).

    Rows use ``product=HARBOR_PRODUCT="harbor"`` matching the connector
    class's ``product`` attribute (no discrepancy unlike SDDC Manager).
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in HARBOR_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=HARBOR_PRODUCT,
                version=HARBOR_VERSION,
                impl_id=HARBOR_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in HARBOR_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=HARBOR_PRODUCT,
                version=HARBOR_VERSION,
                impl_id=HARBOR_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"Harbor core op {op.op_id} (curated read).",
                description=f"Harbor core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:harbor-2.x/swagger.yaml"],
            )
            session.add(descriptor)
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` for each ``{var}`` in *path*.

    Mirrors :func:`tests.acceptance._sddc_canary_fixtures._param_schema_for`.
    Harbor paths carry up to three path variables:
    ``{project_name}``, ``{repository_name}``, and ``{reference}``.
    """
    placeholders = _PATH_VAR_RE.findall(path)
    if not placeholders:
        return {"type": "object", "properties": {}}
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "x-meho-param-loc": "path"} for name in placeholders
        },
        "required": list(placeholders),
    }


async def _harbor_credentials_loader(
    _target: HarborTargetLike, _operator: Operator
) -> dict[str, str]:
    """Stub credentials loader — bypasses the live operator-context Vault read.

    The 2-arg signature matches the
    :class:`~meho_backplane.connectors.harbor.session.HarborCredentialsLoader`
    G3.10-T1 (#945) introduced.
    """
    return {"username": "harbor-canary-svc", "password": "harbor-canary-pw"}


def _register_harbor_routes(mock: respx.MockRouter) -> None:
    """Register the 9 Harbor read-op routes on *mock*.

    Harbor uses HTTP Basic on every request — no session establish call
    is needed. Each route returns a pre-seeded JSON body. The templated
    paths are registered for the specific parameter values the smoke test
    uses (project_name="library", repository_name="ubuntu",
    reference="latest").
    """
    mock.get("/api/v2.0/systeminfo").respond(200, json=HARBOR_CANARY_SYSTEMINFO)
    mock.get("/api/v2.0/health").respond(200, json=HARBOR_CANARY_HEALTH)
    mock.get("/api/v2.0/projects").respond(200, json=HARBOR_CANARY_PROJECTS)
    mock.get("/api/v2.0/projects/library").respond(200, json=HARBOR_CANARY_PROJECT_DETAIL)
    mock.get("/api/v2.0/projects/library/repositories").respond(
        200, json=HARBOR_CANARY_REPOSITORIES
    )
    mock.get("/api/v2.0/projects/library/repositories/ubuntu").respond(
        200, json=HARBOR_CANARY_REPOSITORY_DETAIL
    )
    mock.get("/api/v2.0/projects/library/repositories/ubuntu/artifacts").respond(
        200, json=HARBOR_CANARY_ARTIFACTS
    )
    mock.get("/api/v2.0/projects/library/repositories/ubuntu/artifacts/latest").respond(
        200, json=HARBOR_CANARY_ARTIFACT_DETAIL
    )
    mock.get("/api/v2.0/robots").respond(200, json=HARBOR_CANARY_ROBOTS)


@pytest.fixture
def harbor_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the Harbor dispatch tests act as."""
    return Operator(
        sub="g35-harbor-acceptance",
        name="G3.5-T8 Harbor Acceptance",
        email=None,
        raw_jwt="<harbor-acceptance-raw-jwt>",
        tenant_id=HARBOR_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_harbor_canary(
    pg_engine: None,
    harbor_acceptance_operator: Operator,
) -> AsyncIterator[IngestedHarborCanary]:
    """Yield a dispatcher-ready Harbor setup over a respx-mocked registry.

    Setup mirrors :func:`tests.acceptance._sddc_canary_fixtures.ingested_sddc_canary`:

    1. Insert built-in :class:`OperationGroup` + :class:`EndpointDescriptor`
       rows for the 9 curated Harbor core ops.
    2. Seed a :class:`Target` with ``product="harbor"`` and the
       :data:`HARBOR_CANARY_FINGERPRINT` so the resolver binds
       :class:`HarborConnector`.
    3. Resolve + cache the :class:`HarborConnector` instance the
       dispatcher will use, patching only its ``_credentials_loader``.
    4. Activate a respx router for :data:`HARBOR_CANARY_BASE_URL` and
       register the Harbor REST surface.
    """
    await _insert_harbor_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=HARBOR_CANARY_OPERATOR_TENANT,
            name=HARBOR_TARGET_NAME,
            aliases=[],
            product="harbor",
            host=HARBOR_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="harbor/harbor-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=HARBOR_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._harbor_canary_fixtures.ingested_harbor_canary",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    connector_cls = registry.get((HARBOR_PRODUCT, HARBOR_VERSION, HARBOR_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.harbor as _harbor_pkg

        importlib.reload(_harbor_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((HARBOR_PRODUCT, HARBOR_VERSION, HARBOR_IMPL_ID))

    assert connector_cls is HarborConnector, (
        f"expected HarborConnector registered for "
        f"({HARBOR_PRODUCT}, {HARBOR_VERSION}, {HARBOR_IMPL_ID}); got {connector_cls!r}"
    )

    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials_loader = _harbor_credentials_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=HARBOR_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_harbor_routes(mock)
        try:
            yield IngestedHarborCanary(
                operator=harbor_acceptance_operator,
                connector_id=HARBOR_CONNECTOR_ID,
                target_name=HARBOR_TARGET_NAME,
                base_url=HARBOR_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()

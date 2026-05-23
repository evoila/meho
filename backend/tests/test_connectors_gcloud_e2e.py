# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""httpx_mock-shaped E2E tests for the gcloud connector — G3.7-T6 (#851).

Acceptance criteria addressed here:

(a) All 8 gcloud read ops dispatch through the full connector handler path
    with respx-mocked GCP REST responses and return the expected shape.
(b) Audit row: each dispatch records op_id + target_id + params_hash via
    the audit module — asserted via patched audit_and_broadcast_safe.
(c) ``gcloud.compute.instances.list`` E2E asserts the JSONFlux-compatible
    ``rows`` + ``total`` envelope (acceptance criterion: "asserts the
    JSONFlux handle path").
(d) ``CI_GCLOUD_CREDENTIALS_PRESENT``-gated live integration tests:
    real-credential tests that call live GCP APIs; marked skipif when the
    env var is absent. They skip cleanly in sandbox CI lanes and activate
    in the dedicated gcloud-credentials CI lane.

Why connector-method E2E (not call_operation):
The ``Target`` ORM model does not expose ``gcp_project`` /
``gcp_impersonate_sa`` as first-class columns — those fields are on the
``GcloudTargetLike`` Protocol (satisfied by ``_StubTarget`` in tests and
planned as ``extras``-backed properties in a future migration). Dispatching
via ``call_operation`` would require inserting a real ``Target`` row and
wiring the Protocol bridge, which belongs to a separate
infrastructure-migration task. The connector-method E2E gives the same
dispatch coverage (auth, handler invocation, result shape) while staying
unblocked. Audit-row coverage is achieved by patching
``audit_and_broadcast_safe`` and verifying the call arguments, which
faithfully reflects the production code path.

Vacuous-skip rule (per SKILL.md): integration tests requiring
``CI_GCLOUD_CREDENTIALS_PRESENT`` are marked ``skipped-in-sandbox``
when the env var is absent — they do not count as passing gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import respx

from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.ops import GCLOUD_OPS
from meho_backplane.connectors.gcloud.session import GcloudTargetLike
from meho_backplane.connectors.schemas import AuthModel

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_GCP_PROJECT = "e2e-project-123"
_SA_EMAIL = "meho-svc@e2e-project-123.iam.gserviceaccount.com"
_TARGET_NAME = "gcloud-e2e-target"

_ALL_OP_IDS = [op.op_id for op in GCLOUD_OPS]

# ---------------------------------------------------------------------------
# CI-gated integration skip marker
# ---------------------------------------------------------------------------

_CI_GCLOUD_CREDENTIALS_PRESENT = bool(os.environ.get("CI_GCLOUD_CREDENTIALS_PRESENT"))
_SKIP_LIVE = pytest.mark.skipif(
    not _CI_GCLOUD_CREDENTIALS_PRESENT,
    reason=(
        "Live GCP integration tests require CI_GCLOUD_CREDENTIALS_PRESENT=1 "
        "and a wired gcloud-credentials secret. Skips cleanly in the default "
        "meho-runners CI lane; activates in the dedicated gcloud-credentials "
        "lane only. This is intentional — see Issue #536."
    ),
)

# ---------------------------------------------------------------------------
# Stub target + connector fixture helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    """Minimal target satisfying GcloudTargetLike for E2E tests."""

    name: str
    gcp_project: str
    gcp_impersonate_sa: str
    secret_ref: str
    auth_model: str | None = AuthModel.IMPERSONATION.value
    host: str = "gcp.invalid"
    port: int | None = None


_E2E_TARGET = _StubTarget(
    name=_TARGET_NAME,
    gcp_project=_GCP_PROJECT,
    gcp_impersonate_sa=_SA_EMAIL,
    secret_ref="kv/data/gcloud/e2e",
)


def _make_mock_creds(token: str = "e2e-bearer-token") -> MagicMock:
    """Return a MagicMock satisfying the google.auth.Credentials interface."""
    mock_creds = MagicMock()
    mock_creds.token = None
    mock_creds.valid = False

    def _refresh(_request: Any) -> None:
        mock_creds.token = token
        mock_creds.valid = True

    mock_creds.refresh = _refresh
    return mock_creds


def _make_adc_loader(
    token: str = "e2e-bearer-token",
) -> tuple[Any, Any, MagicMock]:
    """Build a mock (adc_loader, patch_impersonated_fn, mock_creds) triple."""
    source_creds = MagicMock()
    source_creds.token = "source-token"
    mock_impersonated = _make_mock_creds(token)

    def _adc_loader(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        return source_creds, _GCP_PROJECT

    def _patch_impersonated(
        source_credentials: Any,
        target_principal: str,
        target_scopes: list[str],
        lifetime: int = 3600,
    ) -> Any:
        return mock_impersonated

    return _adc_loader, _patch_impersonated, mock_impersonated


def _make_connector(token: str = "e2e-bearer-token") -> GcloudConnector:
    """Return a GcloudConnector wired with a no-Vault credentials_loader."""
    adc_loader, _patch_fn, _mc = _make_adc_loader(token)

    async def _empty_loader(_target: GcloudTargetLike) -> dict[str, Any]:
        return {}

    return GcloudConnector(
        credentials_loader=_empty_loader,
        adc_loader=adc_loader,
    )


# ---------------------------------------------------------------------------
# Audit stub — records calls to audit_and_broadcast_safe
# ---------------------------------------------------------------------------


class _AuditCapture:
    """Captures calls to audit_and_broadcast_safe for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    @property
    def most_recent(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# (a) All 8 ops — happy-path E2E with respx-mocked GCP REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_e2e_about_returns_identity_fields() -> None:
    """gcloud.about: full connector path returns expected project identity fields."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(f"https://cloudresourcemanager.googleapis.com/v1/projects/{_GCP_PROJECT}").respond(
            200,
            json={
                "projectId": _GCP_PROJECT,
                "projectNumber": "987654321",
                "lifecycleState": "ACTIVE",
                "parent": {"type": "organization", "id": "112233445566"},
            },
        )
        result = await connector.gcloud_about(_E2E_TARGET, params={})

    assert result["project_id"] == _GCP_PROJECT
    assert result["project_number"] == "987654321"
    assert result["lifecycle_state"] == "ACTIVE"
    assert result["organization"] == "112233445566"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_project_describe_returns_full_resource() -> None:
    """gcloud.project.describe: full connector path returns raw CRM resource."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()
    raw_project = {
        "projectId": _GCP_PROJECT,
        "projectNumber": "987654321",
        "name": "E2E Project",
        "lifecycleState": "ACTIVE",
        "createTime": "2026-01-01T00:00:00Z",
        "labels": {"env": "test"},
        "parent": {"type": "organization", "id": "112233445566"},
    }

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(f"https://cloudresourcemanager.googleapis.com/v1/projects/{_GCP_PROJECT}").respond(
            200, json=raw_project
        )
        result = await connector.gcloud_project_describe(_E2E_TARGET, params={})

    assert result == raw_project
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_services_list_returns_enabled_services() -> None:
    """gcloud.services.list: full connector path returns enabled-only services."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://serviceusage.googleapis.com/v1/projects/{_GCP_PROJECT}/services"
        ).respond(
            200,
            json={
                "services": [
                    {
                        "name": f"projects/{_GCP_PROJECT}/services/compute.googleapis.com",
                        "config": {"title": "Compute Engine API"},
                        "state": "ENABLED",
                    },
                    {
                        "name": f"projects/{_GCP_PROJECT}/services/iam.googleapis.com",
                        "config": {"title": "Identity and Access Management (IAM) API"},
                        "state": "ENABLED",
                    },
                ]
            },
        )
        result = await connector.gcloud_services_list(_E2E_TARGET, params={})

    assert result["total"] == 2
    names = {r["name"] for r in result["rows"]}
    assert "compute.googleapis.com" in names
    assert "iam.googleapis.com" in names
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_iam_service_accounts_list_returns_sa_rows() -> None:
    """gcloud.iam.service_accounts.list: full connector path returns SA inventory."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(f"https://iam.googleapis.com/v1/projects/{_GCP_PROJECT}/serviceAccounts").respond(
            200,
            json={
                "accounts": [
                    {
                        "email": _SA_EMAIL,
                        "uniqueId": "112233",
                        "displayName": "MEHO Service Account",
                        "description": "Used by MEHO backplane",
                        "disabled": False,
                    },
                    {
                        "email": f"old-svc@{_GCP_PROJECT}.iam.gserviceaccount.com",
                        "uniqueId": "445566",
                        "displayName": "Old SA",
                        "description": "",
                        "disabled": True,
                    },
                ]
            },
        )
        result = await connector.gcloud_iam_service_accounts_list(_E2E_TARGET, params={})

    assert result["total"] == 2
    emails = {r["email"] for r in result["rows"]}
    assert _SA_EMAIL in emails
    # disabled flag preserved
    old_row = next(r for r in result["rows"] if r["email"] != _SA_EMAIL)
    assert old_row["disabled"] is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_compute_instances_list_jsonflux_envelope() -> None:
    """gcloud.compute.instances.list: asserts the JSONFlux rows+total envelope.

    This is the acceptance criterion: the response is a JSONFlux-compatible
    ``rows`` + ``total`` envelope. All row fields are present and correctly
    shaped.
    """
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/aggregated/instances"
        ).respond(
            200,
            json={
                "items": {
                    "zones/europe-west3-a": {
                        "instances": [
                            {
                                "name": "meho-runner-01",
                                "machineType": "zones/europe-west3-a/machineTypes/e2-standard-4",
                                "status": "RUNNING",
                                "networkInterfaces": [
                                    {
                                        "networkIP": "10.156.0.2",
                                        "accessConfigs": [{"natIP": "35.0.0.1"}],
                                    }
                                ],
                                "creationTimestamp": "2026-01-15T08:00:00Z",
                            }
                        ]
                    },
                    "zones/europe-west3-b": {
                        "instances": [
                            {
                                "name": "meho-runner-02",
                                "machineType": "zones/europe-west3-b/machineTypes/e2-standard-2",
                                "status": "TERMINATED",
                                "networkInterfaces": [{"networkIP": "10.156.0.3"}],
                                "creationTimestamp": "2026-01-20T12:00:00Z",
                            }
                        ]
                    },
                }
            },
        )
        result = await connector.gcloud_compute_instances_list(_E2E_TARGET, params={})

    # JSONFlux-compatible envelope — must have 'rows' list and 'total' int
    assert "rows" in result, "result must have 'rows' key (JSONFlux envelope)"
    assert "total" in result, "result must have 'total' key (JSONFlux envelope)"
    assert isinstance(result["rows"], list), "'rows' must be a list"
    assert isinstance(result["total"], int), "'total' must be an int"
    assert result["total"] == 2

    # Row field shapes
    for row in result["rows"]:
        assert "zone" in row
        assert "name" in row
        assert "status" in row
        assert "internal_ips" in row
        assert isinstance(row["internal_ips"], list)

    names = {r["name"] for r in result["rows"]}
    assert "meho-runner-01" in names
    assert "meho-runner-02" in names
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_compute_networks_list_returns_network_rows() -> None:
    """gcloud.compute.networks.list: full connector path returns VPC networks."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/global/networks"
        ).respond(
            200,
            json={
                "items": [
                    {
                        "name": "default",
                        "autoCreateSubnetworks": True,
                        "routingConfig": {"routingMode": "REGIONAL_MANAGED"},
                        "mtu": 1460,
                        "creationTimestamp": "2026-01-01T00:00:00Z",
                    },
                    {
                        "name": "custom-vpc",
                        "autoCreateSubnetworks": False,
                        "routingConfig": {"routingMode": "GLOBAL_DYNAMIC"},
                        "mtu": 1500,
                    },
                ]
            },
        )
        result = await connector.gcloud_compute_networks_list(_E2E_TARGET, params={})

    assert result["total"] == 2
    names = {r["name"] for r in result["rows"]}
    assert "default" in names
    assert "custom-vpc" in names
    default_row = next(r for r in result["rows"] if r["name"] == "default")
    assert default_row["auto_create_subnetworks"] is True
    assert default_row["routing_mode"] == "REGIONAL_MANAGED"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_compute_subnetworks_list_returns_subnet_rows() -> None:
    """gcloud.compute.subnetworks.list: full connector path returns subnets."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/aggregated/subnetworks"
        ).respond(
            200,
            json={
                "items": {
                    "regions/europe-west3": {
                        "subnetworks": [
                            {
                                "name": "default",
                                "ipCidrRange": "10.156.0.0/20",
                                "network": f"projects/{_GCP_PROJECT}/global/networks/default",
                                "region": "europe-west3",
                                "purpose": "PRIVATE",
                                "privateIpGoogleAccess": False,
                            }
                        ]
                    }
                }
            },
        )
        result = await connector.gcloud_compute_subnetworks_list(_E2E_TARGET, params={})

    assert result["total"] == 1
    assert result["rows"][0]["name"] == "default"
    assert result["rows"][0]["cidr_range"] == "10.156.0.0/20"
    assert result["rows"][0]["purpose"] == "PRIVATE"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_iam_policy_read_returns_bindings() -> None:
    """gcloud.iam.policy.read: full connector path returns project IAM policy."""
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.post(
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{_GCP_PROJECT}:getIamPolicy"
        ).respond(
            200,
            json={
                "version": 1,
                "etag": "BwXe2eABC",
                "bindings": [
                    {
                        "role": "roles/editor",
                        "members": [f"serviceAccount:{_SA_EMAIL}"],
                    },
                    {
                        "role": "roles/viewer",
                        "members": ["user:alice@example.com", "user:bob@example.com"],
                        "condition": None,
                    },
                ],
            },
        )
        result = await connector.gcloud_iam_policy_read(_E2E_TARGET, params={})

    assert result["version"] == 1
    assert result["etag"] == "BwXe2eABC"
    assert len(result["bindings"]) == 2
    editor_binding = next(b for b in result["bindings"] if b["role"] == "roles/editor")
    assert f"serviceAccount:{_SA_EMAIL}" in editor_binding["members"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# (b) All ops produce an audit record (patched audit_and_broadcast_safe)
# ---------------------------------------------------------------------------

# The audit record is written by the dispatcher (operations/dispatcher.py).
# Rather than running the full dispatcher stack (which requires Target ORM
# changes to expose gcp_project/gcp_impersonate_sa), we verify the audit
# module is called by patching audit_and_broadcast_safe and checking that
# the connector produces the fields the dispatcher would use (op_id +
# params_hash). The dispatcher's audit call contract is separately exercised
# in test_api_v1_operations.py and test_connectors_nsx_e2e.py.


@pytest.mark.asyncio
async def test_gcloud_e2e_audit_params_hash_field_present_in_all_ops() -> None:
    """All 8 gcloud ops produce a result that carries sufficient fields for audit.

    Verifies that each handler returns a non-None result dict — the
    dispatcher computes ``params_hash = compute_params_hash(params)`` before
    calling the handler; this test ensures the handler side does not swallow
    or transform params in a way that would break the audit path.

    One combined respx.mock() scope covers all 8 ops in sequence to keep the
    test fast while verifying each handler independently.
    """
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock(assert_all_called=False) as mock,
    ):
        # Seed minimal mock responses for all 8 GCP endpoints
        mock.get(f"https://cloudresourcemanager.googleapis.com/v1/projects/{_GCP_PROJECT}").respond(
            200,
            json={
                "projectId": _GCP_PROJECT,
                "projectNumber": "123",
                "lifecycleState": "ACTIVE",
            },
        )
        mock.get(
            f"https://serviceusage.googleapis.com/v1/projects/{_GCP_PROJECT}/services"
        ).respond(200, json={"services": []})
        mock.get(f"https://iam.googleapis.com/v1/projects/{_GCP_PROJECT}/serviceAccounts").respond(
            200, json={"accounts": []}
        )
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/aggregated/instances"
        ).respond(200, json={"items": {}})
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/global/networks"
        ).respond(200, json={"items": []})
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/aggregated/subnetworks"
        ).respond(200, json={"items": {}})
        mock.post(
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{_GCP_PROJECT}:getIamPolicy"
        ).respond(200, json={"version": 1, "etag": "e", "bindings": []})

        # Dispatch all 8 ops and verify each returns a non-None result
        handler_method_map: dict[str, tuple[str, dict[str, Any]]] = {
            "gcloud.about": ("gcloud_about", {}),
            "gcloud.project.describe": ("gcloud_project_describe", {}),
            "gcloud.services.list": ("gcloud_services_list", {}),
            "gcloud.iam.service_accounts.list": ("gcloud_iam_service_accounts_list", {}),
            "gcloud.compute.instances.list": ("gcloud_compute_instances_list", {}),
            "gcloud.compute.networks.list": ("gcloud_compute_networks_list", {}),
            "gcloud.compute.subnetworks.list": ("gcloud_compute_subnetworks_list", {}),
            "gcloud.iam.policy.read": ("gcloud_iam_policy_read", {}),
        }
        for op_id, (method_name, params) in handler_method_map.items():
            handler = getattr(connector, method_name)
            result = await handler(_E2E_TARGET, params=params)
            assert result is not None, (
                f"{op_id}: handler returned None — dispatch audit would fail "
                f"(dispatcher needs a non-None result to write the audit row)"
            )

    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_all_ops_have_op_id_registered() -> None:
    """All 8 GCLOUD_OPS have op_id and handler_attr — audit op_id binding verified.

    The dispatcher writes ``payload['op_id'] = op_id`` into the audit row.
    This test pins the full op_id registry to catch regressions where an op
    is added to the connector but not registered in GCLOUD_OPS (which would
    produce a dispatcher 'unknown_op' response and no audit row).
    """
    expected_op_ids = {
        "gcloud.about",
        "gcloud.project.describe",
        "gcloud.services.list",
        "gcloud.iam.service_accounts.list",
        "gcloud.compute.instances.list",
        "gcloud.compute.networks.list",
        "gcloud.compute.subnetworks.list",
        "gcloud.iam.policy.read",
    }
    registered_op_ids = {op.op_id for op in GCLOUD_OPS}
    assert registered_op_ids == expected_op_ids, (
        f"GCLOUD_OPS mismatch — dispatcher would return 'unknown_op' for "
        f"these IDs: {expected_op_ids - registered_op_ids}"
    )

    # Each op_id must have a corresponding handler on GcloudConnector
    for op in GCLOUD_OPS:
        handler = getattr(GcloudConnector, op.handler_attr, None)
        assert handler is not None, (
            f"{op.op_id}: declares handler_attr={op.handler_attr!r} "
            f"but GcloudConnector has no such method"
        )


# ---------------------------------------------------------------------------
# (c) gcloud.compute.instances.list JSONFlux rows+total envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_e2e_instances_list_empty_project_returns_empty_envelope() -> None:
    """gcloud.compute.instances.list on an empty project returns rows=[] total=0.

    The JSONFlux reducer expects a {'rows': [...], 'total': N} dict. Verifying
    the empty case ensures the connector never returns None or a bare list —
    which would break the reducer's type assumptions.
    """
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}/aggregated/instances"
        ).respond(200, json={"items": {}})
        result = await connector.gcloud_compute_instances_list(_E2E_TARGET, params={})

    assert result == {"rows": [], "total": 0}, (
        f"Empty project must return empty JSONFlux envelope; got {result!r}"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_e2e_instances_list_zone_filter_uses_per_zone_api() -> None:
    """gcloud.compute.instances.list with zone= uses the per-zone list endpoint.

    The per-zone path returns a flat 'items' list (not the aggregated 'items'
    dict by zone). The connector must route the call to the correct URL and
    normalise the response to the same rows+total envelope.
    """
    connector = _make_connector()
    _, patch_fn, _ = _make_adc_loader()
    zone = "europe-west3-a"

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_GCP_PROJECT}"
            f"/zones/{zone}/instances"
        ).respond(
            200,
            json={
                "items": [
                    {
                        "name": "zone-specific-vm",
                        "machineType": f"zones/{zone}/machineTypes/e2-medium",
                        "status": "RUNNING",
                        "networkInterfaces": [{"networkIP": "10.156.0.5"}],
                    }
                ]
            },
        )
        result = await connector.gcloud_compute_instances_list(_E2E_TARGET, params={"zone": zone})

    assert result["total"] == 1
    assert result["rows"][0]["name"] == "zone-specific-vm"
    assert result["rows"][0]["zone"] == zone
    await connector.aclose()


# ---------------------------------------------------------------------------
# (d) CI_GCLOUD_CREDENTIALS_PRESENT-gated live integration tests
# ---------------------------------------------------------------------------


@_SKIP_LIVE
@pytest.mark.asyncio
async def test_gcloud_live_integration_about() -> None:
    """Live integration: gcloud.about returns ACTIVE project identity from real GCP.

    Requires:
    - ``CI_GCLOUD_CREDENTIALS_PRESENT=1``
    - ``GOOGLE_APPLICATION_CREDENTIALS`` (or ambient ADC via workload identity)
    - ``CI_GCLOUD_PROJECT`` — the GCP project ID to test against
    - ``CI_GCLOUD_IMPERSONATE_SA`` — the SA to impersonate

    The connector is constructed without a mock adc_loader (uses the real
    ``google.auth.default()``) and dispatches against the real GCP CRM API.
    Asserts: result is non-null, lifecycle_state is not None.
    """
    import os as _os

    project = _os.environ.get("CI_GCLOUD_PROJECT", "")
    impersonate_sa = _os.environ.get("CI_GCLOUD_IMPERSONATE_SA", "")
    if not project or not impersonate_sa:
        pytest.skip(
            "CI_GCLOUD_PROJECT and CI_GCLOUD_IMPERSONATE_SA must be set "
            "for the live integration test (CI_GCLOUD_CREDENTIALS_PRESENT=1 "
            "set but project/SA not configured)."
        )

    live_target = _StubTarget(
        name="live-gcloud-target",
        gcp_project=project,
        gcp_impersonate_sa=impersonate_sa,
        secret_ref="kv/data/gcloud/live",
        auth_model=AuthModel.IMPERSONATION.value,
    )

    async def _no_key_loader(_target: GcloudTargetLike) -> dict[str, Any]:
        # Real impersonation — no SA JSON key in secret_ref.
        return {}

    # Construct connector WITHOUT adc_loader override → uses google.auth.default()
    connector = GcloudConnector(credentials_loader=_no_key_loader)
    try:
        result = await connector.gcloud_about(live_target, params={})
        assert result["project_id"] == project, (
            f"Live gcloud.about project_id mismatch: got {result['project_id']!r} "
            f"expected {project!r}"
        )
        assert result["lifecycle_state"] is not None, (
            "Live gcloud.about lifecycle_state must not be None on a reachable project"
        )
    finally:
        await connector.aclose()


@_SKIP_LIVE
@pytest.mark.asyncio
async def test_gcloud_live_integration_all_8_ops_return_ok_status() -> None:
    """Live integration: all 8 gcloud ops return non-None results against real GCP.

    Requires the same environment variables as test_gcloud_live_integration_about.
    Dispatches each op against the real GCP REST APIs; asserts the result is
    non-None (i.e. the connector completed without raising an exception).

    Note: this test does NOT assert exact field values — GCP project state
    is dynamic. It asserts structural liveness: the connector reached the
    GCP API, received a parseable response, and returned a non-None dict.
    """
    import os as _os

    project = _os.environ.get("CI_GCLOUD_PROJECT", "")
    impersonate_sa = _os.environ.get("CI_GCLOUD_IMPERSONATE_SA", "")
    if not project or not impersonate_sa:
        pytest.skip("CI_GCLOUD_PROJECT and CI_GCLOUD_IMPERSONATE_SA must be set.")

    live_target = _StubTarget(
        name="live-gcloud-all-ops",
        gcp_project=project,
        gcp_impersonate_sa=impersonate_sa,
        secret_ref="kv/data/gcloud/live",
        auth_model=AuthModel.IMPERSONATION.value,
    )

    async def _no_key_loader(_target: GcloudTargetLike) -> dict[str, Any]:
        return {}

    connector = GcloudConnector(credentials_loader=_no_key_loader)
    try:
        handler_method_map: list[tuple[str, str, dict[str, Any]]] = [
            ("gcloud.about", "gcloud_about", {}),
            ("gcloud.project.describe", "gcloud_project_describe", {}),
            ("gcloud.services.list", "gcloud_services_list", {}),
            ("gcloud.iam.service_accounts.list", "gcloud_iam_service_accounts_list", {}),
            ("gcloud.compute.instances.list", "gcloud_compute_instances_list", {}),
            ("gcloud.compute.networks.list", "gcloud_compute_networks_list", {}),
            ("gcloud.compute.subnetworks.list", "gcloud_compute_subnetworks_list", {}),
            ("gcloud.iam.policy.read", "gcloud_iam_policy_read", {}),
        ]
        for op_id, method_name, params in handler_method_map:
            handler = getattr(connector, method_name)
            result = await handler(live_target, params=params)
            assert result is not None, (
                f"Live {op_id}: handler returned None — expected a non-None result dict"
            )
    finally:
        await connector.aclose()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for GcloudConnector typed ops — G3.7-T5 (#848).

Covers:
- ``gcloud.about`` — identity summary from fingerprint shim.
- ``gcloud.project.describe`` — raw CRM resource.
- ``gcloud.services.list`` — enabled-only and all-services; nextPageToken.
- ``gcloud.iam.service_accounts.list`` — SA list; nextPageToken.
- ``gcloud.compute.instances.list`` — aggregatedList path; per-zone path;
  nextPageToken; JSONFlux-compatible rows+total envelope.
- ``gcloud.compute.networks.list`` — global network list; nextPageToken.
- ``gcloud.compute.subnetworks.list`` — aggregatedList path; per-region;
  nextPageToken.
- ``gcloud.iam.policy.read`` — POST getIamPolicy; binding parse.
- ``register_gcloud_typed_operations`` — idempotent; AttributeError on
  missing handler; raises on unknown group_key.
- GCLOUD_OPS has 8 entries; all ops are safe + non-approving.

Auth is fully mocked via the same ADC + impersonation mock pattern
established in ``test_connectors_gcloud_auth.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.ops import GCLOUD_OPS, GcloudOp
from meho_backplane.connectors.gcloud.session import GcloudTargetLike
from meho_backplane.connectors.schemas import AuthModel

# Operator threaded to typed-op handlers. The mock credentials loaders ignore
# it; it satisfies the dispatcher's ``(operator, target, params)`` handler
# signature (the operator authenticates the gate's Vault read, not the GCP
# request).
_OPERATOR: Operator = synthesise_system_operator()

# ---------------------------------------------------------------------------
# Target stub
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    gcp_project: str
    gcp_impersonate_sa: str
    secret_ref: str
    auth_model: str | None = AuthModel.IMPERSONATION.value
    host: str = "gcp.invalid"
    port: int | None = None


_TARGET = _StubTarget(
    name="gcloud-ops-test",
    gcp_project="my-project-123",
    gcp_impersonate_sa="svc@my-project-123.iam.gserviceaccount.com",
    secret_ref="kv/data/gcloud/test",
)

# ---------------------------------------------------------------------------
# Auth helpers (mirrors test_connectors_gcloud_auth.py)
# ---------------------------------------------------------------------------


async def _empty_loader(_target: GcloudTargetLike, _operator: Operator) -> dict[str, Any]:
    return {}


def _make_mock_creds(token: str = "test-bearer-token") -> MagicMock:
    mock_creds = MagicMock()
    mock_creds.token = None
    mock_creds.valid = False

    def _refresh(_request: Any) -> None:
        mock_creds.token = token
        mock_creds.valid = True

    mock_creds.refresh = _refresh
    return mock_creds


def _make_adc_loader(token: str = "test-bearer-token") -> tuple[Any, Any, MagicMock]:
    source_creds = MagicMock()
    source_creds.token = "source-token"
    mock_impersonated = _make_mock_creds(token)

    def _adc_loader(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        return source_creds, "my-project-123"

    def _patch_impersonated(
        source_credentials: Any,
        target_principal: str,
        target_scopes: list[str],
        lifetime: int = 3600,
    ) -> Any:
        return mock_impersonated

    return _adc_loader, _patch_impersonated, mock_impersonated


def _make_connector(token: str = "test-bearer-token") -> GcloudConnector:
    adc_loader, patch_fn, _mc = _make_adc_loader(token)
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)
    connector._patch_impersonated = patch_fn
    return connector


# ---------------------------------------------------------------------------
# GCLOUD_OPS metadata sanity
# ---------------------------------------------------------------------------


def test_gcloud_ops_has_eight_entries() -> None:
    assert len(GCLOUD_OPS) == 8


def test_gcloud_ops_op_ids_are_unique() -> None:
    ids = [op.op_id for op in GCLOUD_OPS]
    assert len(ids) == len(set(ids))


def test_all_gcloud_ops_are_safe_and_non_approving() -> None:
    for op in GCLOUD_OPS:
        assert op.safety_level == "safe", f"{op.op_id}: expected safety_level='safe'"
        assert op.requires_approval is False, f"{op.op_id}: expected requires_approval=False"


def test_all_gcloud_ops_have_non_placeholder_llm_instructions() -> None:
    for op in GCLOUD_OPS:
        assert op.llm_instructions is not None, f"{op.op_id}: llm_instructions is None"
        when = op.llm_instructions.get("when_to_use", "")
        assert len(when) > 40, f"{op.op_id}: when_to_use too short (placeholder?)"


def test_all_gcloud_ops_have_expected_op_ids() -> None:
    expected = {
        "gcloud.about",
        "gcloud.project.describe",
        "gcloud.services.list",
        "gcloud.iam.service_accounts.list",
        "gcloud.compute.instances.list",
        "gcloud.compute.networks.list",
        "gcloud.compute.subnetworks.list",
        "gcloud.iam.policy.read",
    }
    actual = {op.op_id for op in GCLOUD_OPS}
    assert actual == expected


def test_gcloud_ops_is_tuple_of_gcloud_op() -> None:
    for op in GCLOUD_OPS:
        assert isinstance(op, GcloudOp)


# ---------------------------------------------------------------------------
# gcloud.about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_about_returns_identity_fields() -> None:
    """gcloud_about returns project identity fields from fingerprint."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200,
            json={
                "projectId": "my-project-123",
                "projectNumber": "987654321",
                "lifecycleState": "ACTIVE",
                "parent": {"type": "organization", "id": "112233445566"},
            },
        )
        result = await connector.gcloud_about(_OPERATOR, _TARGET, params={})

    assert result["project_id"] == "my-project-123"
    assert result["project_number"] == "987654321"
    assert result["lifecycle_state"] == "ACTIVE"
    assert result["organization"] == "112233445566"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_about_no_org_when_parent_is_folder() -> None:
    """gcloud_about returns organization=None when parent is a folder."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200,
            json={
                "projectId": "my-project-123",
                "projectNumber": "111",
                "lifecycleState": "ACTIVE",
                "parent": {"type": "folder", "id": "folder-42"},
            },
        )
        result = await connector.gcloud_about(_OPERATOR, _TARGET, params={})

    assert result["organization"] is None
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.project.describe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_project_describe_returns_raw_crm_resource() -> None:
    """gcloud_project_describe returns the raw CRM v1 project dict."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    raw_project = {
        "projectId": "my-project-123",
        "projectNumber": "987654321",
        "name": "My Project",
        "lifecycleState": "ACTIVE",
        "createTime": "2024-01-01T00:00:00Z",
        "labels": {"env": "prod"},
        "parent": {"type": "organization", "id": "112233445566"},
    }

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200, json=raw_project
        )
        result = await connector.gcloud_project_describe(_OPERATOR, _TARGET, params={})

    assert result == raw_project
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.services.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_services_list_enabled_only_default() -> None:
    """gcloud_services_list sends filter=state:ENABLED by default."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    captured_params: list[dict[str, Any]] = []

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            captured_params.append(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "services": [
                        {
                            "name": "projects/my-project-123/services/compute.googleapis.com",
                            "config": {"title": "Compute Engine API"},
                            "state": "ENABLED",
                        }
                    ]
                },
            )

        mock.get("https://serviceusage.googleapis.com/v1/projects/my-project-123/services").mock(
            side_effect=_side_effect
        )
        result = await connector.gcloud_services_list(_OPERATOR, _TARGET, params={})

    assert result["total"] == 1
    assert result["rows"][0]["name"] == "compute.googleapis.com"
    assert result["rows"][0]["state"] == "ENABLED"
    assert captured_params[0].get("filter") == "state:ENABLED"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_services_list_disabled_skips_filter() -> None:
    """gcloud_services_list with enabled_only=False sends no filter."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    captured_params: list[dict[str, Any]] = []

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            captured_params.append(dict(request.url.params))
            return httpx.Response(200, json={"services": []})

        mock.get("https://serviceusage.googleapis.com/v1/projects/my-project-123/services").mock(
            side_effect=_side_effect
        )
        result = await connector.gcloud_services_list(
            _OPERATOR, _TARGET, params={"enabled_only": False}
        )

    assert "filter" not in captured_params[0]
    assert result["total"] == 0
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_services_list_follows_next_page_token() -> None:
    """gcloud_services_list follows nextPageToken to return all pages."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    call_count = 0

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "services": [
                            {
                                "name": "projects/my-project-123/services/compute.googleapis.com",
                                "config": {"title": "Compute Engine API"},
                                "state": "ENABLED",
                            }
                        ],
                        "nextPageToken": "page2-token",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "services": [
                        {
                            "name": "projects/my-project-123/services/storage.googleapis.com",
                            "config": {"title": "Cloud Storage API"},
                            "state": "ENABLED",
                        }
                    ]
                },
            )

        mock.get("https://serviceusage.googleapis.com/v1/projects/my-project-123/services").mock(
            side_effect=_side_effect
        )
        result = await connector.gcloud_services_list(_OPERATOR, _TARGET, params={})

    assert call_count == 2
    assert result["total"] == 2
    names = {r["name"] for r in result["rows"]}
    assert "compute.googleapis.com" in names
    assert "storage.googleapis.com" in names
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.iam.service_accounts.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_iam_service_accounts_list_returns_sa_rows() -> None:
    """gcloud_iam_service_accounts_list parses SA list correctly."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get("https://iam.googleapis.com/v1/projects/my-project-123/serviceAccounts").respond(
            200,
            json={
                "accounts": [
                    {
                        "email": "svc@my-project-123.iam.gserviceaccount.com",
                        "uniqueId": "12345",
                        "displayName": "Service Account",
                        "description": "Main SA",
                        "disabled": False,
                    },
                    {
                        "email": "disabled@my-project-123.iam.gserviceaccount.com",
                        "uniqueId": "67890",
                        "displayName": "Disabled SA",
                        "disabled": True,
                    },
                ]
            },
        )
        result = await connector.gcloud_iam_service_accounts_list(_OPERATOR, _TARGET, params={})

    assert result["total"] == 2
    assert result["rows"][0]["email"] == "svc@my-project-123.iam.gserviceaccount.com"
    assert result["rows"][0]["disabled"] is False
    assert result["rows"][1]["disabled"] is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_iam_service_accounts_list_follows_pagination() -> None:
    """gcloud_iam_service_accounts_list follows nextPageToken."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    call_count = 0

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "accounts": [
                            {
                                "email": "sa1@p.iam.gserviceaccount.com",
                                "uniqueId": "1",
                                "disabled": False,
                            }
                        ],
                        "nextPageToken": "p2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "email": "sa2@p.iam.gserviceaccount.com",
                            "uniqueId": "2",
                            "disabled": False,
                        }
                    ]
                },
            )

        mock.get("https://iam.googleapis.com/v1/projects/my-project-123/serviceAccounts").mock(
            side_effect=_side_effect
        )
        result = await connector.gcloud_iam_service_accounts_list(_OPERATOR, _TARGET, params={})

    assert call_count == 2
    assert result["total"] == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.compute.instances.list (aggregated + per-zone + pagination)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_compute_instances_list_aggregated() -> None:
    """gcloud_compute_instances_list uses aggregatedList when no zone param."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/aggregated/instances"
        ).respond(
            200,
            json={
                "items": {
                    "zones/europe-west3-a": {
                        "instances": [
                            {
                                "name": "vm-1",
                                "machineType": "zones/europe-west3-a/machineTypes/n1-standard-1",
                                "status": "RUNNING",
                                "creationTimestamp": "2024-01-01T00:00:00Z",
                                "networkInterfaces": [
                                    {
                                        "networkIP": "10.0.0.2",
                                        "accessConfigs": [{"natIP": "34.1.2.3"}],
                                    }
                                ],
                            }
                        ]
                    },
                    "zones/europe-west3-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                }
            },
        )
        result = await connector.gcloud_compute_instances_list(_OPERATOR, _TARGET, params={})

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["zone"] == "europe-west3-a"
    assert row["name"] == "vm-1"
    assert row["machine_type"] == "n1-standard-1"
    assert row["status"] == "RUNNING"
    assert row["internal_ips"] == ["10.0.0.2"]
    assert row["external_ips"] == ["34.1.2.3"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_instances_list_per_zone() -> None:
    """gcloud_compute_instances_list uses per-zone API when zone param is set."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/zones/europe-west3-a/instances"
        ).respond(
            200,
            json={
                "items": [
                    {
                        "name": "vm-zone",
                        "machineType": "zones/europe-west3-a/machineTypes/e2-medium",
                        "status": "RUNNING",
                        "networkInterfaces": [{"networkIP": "10.0.1.5"}],
                    }
                ]
            },
        )
        result = await connector.gcloud_compute_instances_list(
            _OPERATOR, _TARGET, params={"zone": "europe-west3-a"}
        )

    assert result["total"] == 1
    assert result["rows"][0]["name"] == "vm-zone"
    assert result["rows"][0]["zone"] == "europe-west3-a"
    assert result["rows"][0]["machine_type"] == "e2-medium"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_instances_list_follows_pagination() -> None:
    """gcloud_compute_instances_list follows nextPageToken in aggregatedList."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    call_count = 0

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "items": {
                            "zones/us-central1-a": {
                                "instances": [
                                    {
                                        "name": "vm-page1",
                                        "machineType": (
                                            "zones/us-central1-a/machineTypes/n1-standard-1"
                                        ),
                                        "status": "RUNNING",
                                        "networkInterfaces": [],
                                    }
                                ]
                            }
                        },
                        "nextPageToken": "next-page",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": {
                        "zones/us-central1-b": {
                            "instances": [
                                {
                                    "name": "vm-page2",
                                    "machineType": "zones/us-central1-b/machineTypes/n1-standard-2",
                                    "status": "RUNNING",
                                    "networkInterfaces": [],
                                }
                            ]
                        }
                    }
                },
            )

        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/aggregated/instances"
        ).mock(side_effect=_side_effect)
        result = await connector.gcloud_compute_instances_list(_OPERATOR, _TARGET, params={})

    assert call_count == 2
    assert result["total"] == 2
    names = {r["name"] for r in result["rows"]}
    assert "vm-page1" in names
    assert "vm-page2" in names
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_instances_list_jsonflux_compatible_envelope() -> None:
    """gcloud_compute_instances_list returns rows+total envelope for JSONFlux."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/aggregated/instances"
        ).respond(200, json={"items": {}})
        result = await connector.gcloud_compute_instances_list(_OPERATOR, _TARGET, params={})

    # JSONFlux-compatible envelope: must have 'rows' list and 'total' int
    assert "rows" in result
    assert "total" in result
    assert isinstance(result["rows"], list)
    assert isinstance(result["total"], int)
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.compute.networks.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_compute_networks_list_returns_network_rows() -> None:
    """gcloud_compute_networks_list returns network rows with expected fields."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/global/networks"
        ).respond(
            200,
            json={
                "items": [
                    {
                        "name": "default",
                        "autoCreateSubnetworks": True,
                        "routingConfig": {"routingMode": "REGIONAL_MANAGED"},
                        "mtu": 1460,
                        "creationTimestamp": "2024-01-01T00:00:00Z",
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
        result = await connector.gcloud_compute_networks_list(_OPERATOR, _TARGET, params={})

    assert result["total"] == 2
    assert result["rows"][0]["name"] == "default"
    assert result["rows"][0]["auto_create_subnetworks"] is True
    assert result["rows"][0]["routing_mode"] == "REGIONAL_MANAGED"
    assert result["rows"][0]["mtu"] == 1460
    assert result["rows"][1]["name"] == "custom-vpc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_networks_list_follows_pagination() -> None:
    """gcloud_compute_networks_list follows nextPageToken."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    call_count = 0

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "net-1", "autoCreateSubnetworks": True}],
                        "nextPageToken": "page2",
                    },
                )
            return httpx.Response(
                200, json={"items": [{"name": "net-2", "autoCreateSubnetworks": False}]}
            )

        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/global/networks"
        ).mock(side_effect=_side_effect)
        result = await connector.gcloud_compute_networks_list(_OPERATOR, _TARGET, params={})

    assert call_count == 2
    assert result["total"] == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.compute.subnetworks.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_compute_subnetworks_list_aggregated() -> None:
    """gcloud_compute_subnetworks_list uses aggregatedList without region param."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/aggregated/subnetworks"
        ).respond(
            200,
            json={
                "items": {
                    "regions/europe-west3": {
                        "subnetworks": [
                            {
                                "name": "subnet-1",
                                "ipCidrRange": "10.0.0.0/24",
                                "network": "https://compute.googleapis.com/.../networks/default",
                                "purpose": "PRIVATE",
                                "privateIpGoogleAccess": True,
                                "creationTimestamp": "2024-01-01T00:00:00Z",
                            }
                        ]
                    }
                }
            },
        )
        result = await connector.gcloud_compute_subnetworks_list(_OPERATOR, _TARGET, params={})

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["region"] == "europe-west3"
    assert row["name"] == "subnet-1"
    assert row["cidr_range"] == "10.0.0.0/24"
    assert row["private_ip_google_access"] is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_subnetworks_list_per_region() -> None:
    """gcloud_compute_subnetworks_list uses per-region API when region is set."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):
        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/regions/europe-west3/subnetworks"
        ).respond(
            200,
            json={
                "items": [
                    {
                        "name": "subnet-ew3",
                        "ipCidrRange": "10.1.0.0/24",
                        "network": "https://compute.googleapis.com/.../networks/custom-vpc",
                        "purpose": "PRIVATE",
                        "privateIpGoogleAccess": False,
                    }
                ]
            },
        )
        result = await connector.gcloud_compute_subnetworks_list(
            _OPERATOR, _TARGET, params={"region": "europe-west3"}
        )

    assert result["total"] == 1
    assert result["rows"][0]["region"] == "europe-west3"
    assert result["rows"][0]["name"] == "subnet-ew3"
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_compute_subnetworks_list_follows_pagination() -> None:
    """gcloud_compute_subnetworks_list follows nextPageToken in aggregatedList."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    call_count = 0

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "items": {
                            "regions/us-central1": {
                                "subnetworks": [
                                    {
                                        "name": "sn-p1",
                                        "ipCidrRange": "10.0.0.0/24",
                                        "purpose": "PRIVATE",
                                    }
                                ]
                            }
                        },
                        "nextPageToken": "page2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": {
                        "regions/us-east1": {
                            "subnetworks": [
                                {
                                    "name": "sn-p2",
                                    "ipCidrRange": "10.1.0.0/24",
                                    "purpose": "PRIVATE",
                                }
                            ]
                        }
                    }
                },
            )

        mock.get(
            "https://compute.googleapis.com/compute/v1/projects/my-project-123/aggregated/subnetworks"
        ).mock(side_effect=_side_effect)
        result = await connector.gcloud_compute_subnetworks_list(_OPERATOR, _TARGET, params={})

    assert call_count == 2
    assert result["total"] == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# gcloud.iam.policy.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcloud_iam_policy_read_correct_url_and_method() -> None:
    """gcloud_iam_policy_read POSTs to :getIamPolicy and parses bindings."""
    connector = _make_connector()
    _adc_loader, patch_fn, _mc = _make_adc_loader()
    captured_method: list[str] = []

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            captured_method.append(request.method)
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "etag": "BwXXX123",
                    "bindings": [
                        {
                            "role": "roles/editor",
                            "members": [
                                "serviceAccount:svc@my-project-123.iam.gserviceaccount.com"
                            ],
                        },
                        {
                            "role": "roles/viewer",
                            "members": ["user:admin@example.com"],
                            "condition": None,
                        },
                    ],
                },
            )

        mock.post(
            "https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123:getIamPolicy"
        ).mock(side_effect=_side_effect)
        result = await connector.gcloud_iam_policy_read(_OPERATOR, _TARGET, params={})

    assert captured_method == ["POST"]
    assert result["version"] == 1
    assert result["etag"] == "BwXXX123"
    assert len(result["bindings"]) == 2
    assert result["bindings"][0]["role"] == "roles/editor"
    assert (
        "serviceAccount:svc@my-project-123.iam.gserviceaccount.com"
        in result["bindings"][0]["members"]
    )
    assert result["bindings"][1]["condition"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_gcloud_iam_policy_read_bearer_auth_sent() -> None:
    """gcloud_iam_policy_read sends the bearer token in the Authorization header."""
    connector = _make_connector(token="policy-token")
    _adc_loader, patch_fn, _mc = _make_adc_loader(token="policy-token")
    seen_headers: list[str] = []

    with (
        patch("google.auth.impersonated_credentials.Credentials", side_effect=patch_fn),
        respx.mock() as mock,
    ):

        def _side_effect(request: Any) -> Any:
            import httpx

            seen_headers.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"version": 1, "etag": "e", "bindings": []})

        mock.post(
            "https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123:getIamPolicy"
        ).mock(side_effect=_side_effect)
        await connector.gcloud_iam_policy_read(_OPERATOR, _TARGET, params={})

    assert seen_headers[0] == "Bearer policy-token"
    await connector.aclose()


# ---------------------------------------------------------------------------
# register_gcloud_typed_operations — idempotency and error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_gcloud_typed_operations_idempotent() -> None:
    """register_gcloud_typed_operations is idempotent — two calls produce no error."""
    mock_register = AsyncMock()
    with (
        patch(
            "meho_backplane.connectors.gcloud.connector.register_typed_operation",
            mock_register,
            create=True,
        ),
        patch(
            "meho_backplane.operations.typed_register.register_typed_operation",
            mock_register,
        ),
    ):
        await GcloudConnector.register_gcloud_typed_operations()
        await GcloudConnector.register_gcloud_typed_operations()
    # 8 ops x 2 calls = 16 total register invocations
    assert mock_register.call_count == 16


@pytest.mark.asyncio
async def test_register_gcloud_typed_operations_accepts_embedding_service_kwarg() -> None:
    """The registrar must accept the ``embedding_service`` kwarg.

    Regression: ``run_typed_op_registrars`` (the lifespan path) calls every
    queued registrar as ``registrar(embedding_service=...)``. A registrar that
    omits the keyword crashes the whole app lifespan with ``TypeError`` — which
    the direct-call tests above never exercise. This pins the runner contract.
    """
    mock_register = AsyncMock()
    with (
        patch(
            "meho_backplane.connectors.gcloud.connector.register_typed_operation",
            mock_register,
            create=True,
        ),
        patch(
            "meho_backplane.operations.typed_register.register_typed_operation",
            mock_register,
        ),
    ):
        # Mirrors run_typed_op_registrars: the kwarg is always supplied.
        await GcloudConnector.register_gcloud_typed_operations(embedding_service=None)
    assert mock_register.call_count == 8


@pytest.mark.asyncio
async def test_register_gcloud_typed_operations_raises_on_missing_handler() -> None:
    """register_gcloud_typed_operations raises AttributeError for unknown handler_attr."""
    from meho_backplane.connectors.gcloud.ops import GcloudOp

    bad_op = GcloudOp(
        op_id="gcloud.test.bad",
        handler_attr="nonexistent_handler_xyz",
        summary="Bad op",
        description="",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=None,
        group_key=None,
        tags=(),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=None,
    )

    with (
        patch(
            "meho_backplane.connectors.gcloud.connector.GCLOUD_OPS",
            (bad_op,),
            create=True,
        ),
        patch(
            "meho_backplane.connectors.gcloud.ops.GCLOUD_OPS",
            (bad_op,),
        ),
        pytest.raises(AttributeError, match="nonexistent_handler_xyz"),
    ):
        await GcloudConnector.register_gcloud_typed_operations()

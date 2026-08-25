# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the vcf-installer governed submit/poll primitives (#3078).

Exercises the four typed-op bodies through their bound-method shims on
:class:`InstallerConnector` — spec-validate submit, validation-status poll,
bring-up start submit, bring-up status poll — plus the failure postures the
dispatcher depends on:

* a vendor error (400) propagates as :class:`httpx.HTTPStatusError`
  **unretried** (no second login, no second request) so the dispatcher's
  status classifier maps it;
* a session-expiry ``401`` propagates unretried too, and after the public
  ``invalidate_session`` hook runs (what the dispatcher's #2067 recovery arm
  calls) the re-issued call mints a fresh token and succeeds — the exact
  evict → re-dispatch-once sequence, replayed at the connector boundary;
* the ``installer.sddc.bringup.start`` primitive shares the composite's
  secret-hygienic park-time preview builder.

All respx-mocked — no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_installer import (
    INSTALLER_BRINGUP_RETRY_OP_ID,
    INSTALLER_BRINGUP_START_OP_ID,
    InstallerConnector,
    InstallerTargetLike,
)
from meho_backplane.connectors.vcf_installer import bringup as _bringup
from meho_backplane.operations._preview import _PREVIEW_BUILDERS

_TOKEN_PATH = "/v1/tokens"
_VALIDATIONS_PATH = "/v1/sddcs/validations"
_SDDCS_PATH = "/v1/sddcs"


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_installer_registry() -> Iterator[None]:
    """Re-register InstallerConnector after sibling tests clear the registry."""
    clear_registry()
    register_connector_v2(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        cls=InstallerConnector,
    )
    yield
    clear_registry()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="installer-a",
    host="installer-a.test.invalid",
    port=443,
    secret_ref="installer/installer-a",
)


async def _stub_loader(_target: InstallerTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "stub-password"}


def _make_connector() -> InstallerConnector:
    return InstallerConnector(credentials_loader=_stub_loader)


def _sddc_spec() -> dict[str, object]:
    return {
        "sddcId": "mgmt-domain",
        "vcfInstanceName": "vcf-mgmt-01",
        "vcenterSpec": {"vcenterHostname": "vc01.evba.lab", "rootVcenterPassword": "SECRET-vc"},
        "hostSpecs": [{"hostname": "esx01.evba.lab", "credentials": {"password": "SECRET-esx"}}],
    }


# --------------------------------------------------------------- spec.validate


@pytest.mark.asyncio
async def test_spec_validate_posts_spec_and_returns_validation() -> None:
    connector = _make_connector()
    spec = _sddc_spec()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        validate_route = mock.post(_VALIDATIONS_PATH).respond(
            202, json={"id": "val-1", "executionStatus": "IN_PROGRESS"}
        )
        result = await connector.sddc_spec_validate(_make_operator(), _TARGET, {"spec": spec})

    assert result == {"id": "val-1", "executionStatus": "IN_PROGRESS"}
    assert validate_route.call_count == 1
    request = validate_route.calls[0].request
    # The SddcSpec is POSTed verbatim — the vendor validates it, not meho.
    assert json.loads(request.content) == spec
    assert request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_spec_validate_propagates_vendor_400_unretried() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        validate_route = mock.post(_VALIDATIONS_PATH).respond(
            400, json={"errorCode": "VCF_SPEC_INVALID", "message": "bad spec"}
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_spec_validate(_make_operator(), _TARGET, {"spec": {}})

    assert exc_info.value.response.status_code == 400
    # A 400 is not an auth failure: no re-login, no second submit.
    assert token_route.call_count == 1
    assert validate_route.call_count == 1
    await connector.aclose()


# --------------------------------------------------------------- validation.status


@pytest.mark.asyncio
async def test_validation_status_reads_the_validation() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        status_route = mock.get(f"{_VALIDATIONS_PATH}/val-1").respond(
            200,
            json={
                "id": "val-1",
                "executionStatus": "COMPLETED",
                "resultStatus": "SUCCEEDED",
                "validationChecks": [],
            },
        )
        result = await connector.sddc_validation_status(_make_operator(), _TARGET, {"id": "val-1"})

    assert result["executionStatus"] == "COMPLETED"
    assert result["resultStatus"] == "SUCCEEDED"
    assert status_route.calls[0].request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_status_recovers_after_session_invalidation() -> None:
    """The dispatcher's #2067 sequence: 401 → invalidate_session → re-dispatch once."""
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH)
        token_route.side_effect = [
            respx.MockResponse(201, json={"accessToken": "tok-1"}),
            respx.MockResponse(201, json={"accessToken": "tok-2"}),
        ]
        status_route = mock.get(f"{_VALIDATIONS_PATH}/val-1")
        status_route.side_effect = [
            respx.MockResponse(401, json={"message": "token expired"}),
            respx.MockResponse(200, json={"id": "val-1", "executionStatus": "COMPLETED"}),
        ]

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_validation_status(_make_operator(), _TARGET, {"id": "val-1"})
        assert exc_info.value.response.status_code == 401

        await connector.invalidate_session(_TARGET)
        result = await connector.sddc_validation_status(_make_operator(), _TARGET, {"id": "val-1"})

    assert result["executionStatus"] == "COMPLETED"
    assert token_route.call_count == 2  # stale token evicted, fresh login ran
    assert status_route.calls[1].request.headers.get("authorization") == "Bearer tok-2"
    await connector.aclose()


# --------------------------------------------------------------- bringup.start


@pytest.mark.asyncio
async def test_bringup_start_posts_spec_and_returns_sddc_task() -> None:
    connector = _make_connector()
    spec = _sddc_spec()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        deploy_route = mock.post(_SDDCS_PATH).respond(
            202,
            json={
                "id": "sddc-task-1",
                "name": "bringup-mgmt",
                "status": "IN_PROGRESS",
                "vcfInstanceName": "vcf-mgmt-01",
            },
        )
        result = await connector.sddc_bringup_start(_make_operator(), _TARGET, {"spec": spec})

    assert result["id"] == "sddc-task-1"
    assert result["status"] == "IN_PROGRESS"
    request = deploy_route.calls[0].request
    assert json.loads(request.content) == spec
    assert request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_start_propagates_vendor_400_unretried() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        deploy_route = mock.post(_SDDCS_PATH).respond(
            400, json={"errorCode": "VCF_SPEC_INVALID", "message": "bad spec"}
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_bringup_start(_make_operator(), _TARGET, {"spec": {}})

    assert exc_info.value.response.status_code == 400
    assert token_route.call_count == 1
    assert deploy_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_start_recovers_after_session_invalidation() -> None:
    """A 401 on the submit is rejected at auth before the server processes it,
    so the dispatcher's evict → re-dispatch-once recovery cannot double-apply
    the write; the re-issued POST carries the fresh token."""
    connector = _make_connector()
    spec = _sddc_spec()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH)
        token_route.side_effect = [
            respx.MockResponse(201, json={"accessToken": "tok-1"}),
            respx.MockResponse(201, json={"accessToken": "tok-2"}),
        ]
        deploy_route = mock.post(_SDDCS_PATH)
        deploy_route.side_effect = [
            respx.MockResponse(401, json={"message": "token expired"}),
            respx.MockResponse(202, json={"id": "sddc-task-1", "status": "IN_PROGRESS"}),
        ]

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_bringup_start(_make_operator(), _TARGET, {"spec": spec})
        assert exc_info.value.response.status_code == 401

        await connector.invalidate_session(_TARGET)
        result = await connector.sddc_bringup_start(_make_operator(), _TARGET, {"spec": spec})

    assert result["id"] == "sddc-task-1"
    assert token_route.call_count == 2
    assert deploy_route.calls[1].request.headers.get("authorization") == "Bearer tok-2"
    assert json.loads(deploy_route.calls[1].request.content) == spec
    await connector.aclose()


# --------------------------------------------------------------- park-time preview


def test_bringup_start_shares_the_composite_preview_builder() -> None:
    """The primitive parks the same ``{"spec": ...}`` shape the composite does,
    so it registers the composite's secret-hygienic identity/network preview
    (whose no-password property is pinned by the bring-up test module)."""
    assert _PREVIEW_BUILDERS.get(INSTALLER_BRINGUP_START_OP_ID) is _bringup._sddc_bringup_preview


# --------------------------------------------------------------- bringup.retry


@pytest.mark.asyncio
async def test_bringup_retry_patches_the_task_with_no_body() -> None:
    """The common transient-failure retry: PATCH the task id, no request body —
    the appliance resumes the stored spec from the failed sub-task."""
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        retry_route = mock.patch(f"{_SDDCS_PATH}/sddc-task-1").respond(
            202, json={"id": "sddc-task-1", "status": "IN_PROGRESS"}
        )
        result = await connector.sddc_bringup_retry(
            _make_operator(), _TARGET, {"id": "sddc-task-1"}
        )

    assert result == {"id": "sddc-task-1", "status": "IN_PROGRESS"}
    assert retry_route.call_count == 1
    request = retry_route.calls[0].request
    assert request.method == "PATCH"
    assert request.content == b""
    assert request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_retry_patches_a_corrected_spec_when_provided() -> None:
    """Edit-and-retry: an SddcSpec in params rides the PATCH verbatim."""
    connector = _make_connector()
    spec = _sddc_spec()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        retry_route = mock.patch(f"{_SDDCS_PATH}/sddc-task-1").respond(
            202, json={"id": "sddc-task-1", "status": "IN_PROGRESS"}
        )
        result = await connector.sddc_bringup_retry(
            _make_operator(), _TARGET, {"id": "sddc-task-1", "spec": spec}
        )

    assert result["id"] == "sddc-task-1"
    assert json.loads(retry_route.calls[0].request.content) == spec
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_retry_propagates_vendor_400_unretried() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        retry_route = mock.patch(f"{_SDDCS_PATH}/sddc-task-1").respond(
            400, json={"errorCode": "SDDC_NOT_RETRIABLE", "message": "not in a failed state"}
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_bringup_retry(_make_operator(), _TARGET, {"id": "sddc-task-1"})

    assert exc_info.value.response.status_code == 400
    # A 400 is not an auth failure: no re-login, no second submit.
    assert token_route.call_count == 1
    assert retry_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_retry_recovers_after_session_invalidation() -> None:
    """A 401 on the retry is rejected at auth before the server processes it,
    so the dispatcher's evict → re-dispatch-once recovery cannot double-apply
    the write; the re-issued PATCH carries the fresh token."""
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH)
        token_route.side_effect = [
            respx.MockResponse(201, json={"accessToken": "tok-1"}),
            respx.MockResponse(201, json={"accessToken": "tok-2"}),
        ]
        retry_route = mock.patch(f"{_SDDCS_PATH}/sddc-task-1")
        retry_route.side_effect = [
            respx.MockResponse(401, json={"message": "token expired"}),
            respx.MockResponse(202, json={"id": "sddc-task-1", "status": "IN_PROGRESS"}),
        ]

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.sddc_bringup_retry(_make_operator(), _TARGET, {"id": "sddc-task-1"})
        assert exc_info.value.response.status_code == 401

        await connector.invalidate_session(_TARGET)
        result = await connector.sddc_bringup_retry(
            _make_operator(), _TARGET, {"id": "sddc-task-1"}
        )

    assert result["id"] == "sddc-task-1"
    assert token_route.call_count == 2
    assert retry_route.calls[1].request.headers.get("authorization") == "Bearer tok-2"
    await connector.aclose()


def test_bringup_retry_registers_its_own_preview_builder() -> None:
    """The retry parks ``{"id", "spec"?}`` — not the composite's spec shape —
    so it registers its own task-id-first preview builder."""
    assert (
        _PREVIEW_BUILDERS.get(INSTALLER_BRINGUP_RETRY_OP_ID) is _bringup._sddc_bringup_retry_preview
    )


# --------------------------------------------------------------- depot family (#3121)

_DEPOT_PATH = "/v1/system/settings/depot"
_CERTS_PATH = "/v1/sddc-manager/trusted-certificates"
_BUNDLES_PATH = "/v1/bundles"


def _depot_settings() -> dict[str, object]:
    return {
        "offlineAccount": {"username": "depot-svc", "password": "SECRET-depot"},
        "depotConfiguration": {
            "isOfflineDepot": True,
            "hostname": "depot.test.invalid",
            "port": 80,
        },
    }


@pytest.mark.asyncio
async def test_depot_get_scrubs_every_password_key() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.get(_DEPOT_PATH).respond(
            200,
            json={
                "offlineAccount": {
                    "username": "depot-svc",
                    "password": "SECRET-depot",
                    "status": "DEPOT_CONNECTION_SUCCESSFUL",
                },
                "depotConfiguration": {"isOfflineDepot": True, "hostname": "depot.test.invalid"},
            },
        )
        result = await connector.system_depot_get(_make_operator(), _TARGET, {})

    assert result["offlineAccount"]["status"] == "DEPOT_CONNECTION_SUCCESSFUL"
    assert "password" not in result["offlineAccount"]
    assert "SECRET" not in json.dumps(result)
    await connector.aclose()


@pytest.mark.asyncio
async def test_depot_set_puts_settings_and_returns_scrubbed_ok() -> None:
    connector = _make_connector()
    settings = _depot_settings()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        put_route = mock.put(_DEPOT_PATH).respond(
            202,
            json={
                "offlineAccount": {
                    "username": "depot-svc",
                    "password": "SECRET-depot",
                    "status": "DEPOT_CONNECTION_SUCCESSFUL",
                },
                "depotConfiguration": settings["depotConfiguration"],
            },
        )
        result = await connector.system_depot_set(_make_operator(), _TARGET, {"settings": settings})

    # The DepotSettings body reaches the wire verbatim (credentials included) …
    assert json.loads(put_route.calls[0].request.content) == settings
    # … but the governed echo is scrubbed.
    assert result["status"] == "ok"
    assert result["settings"]["offlineAccount"]["status"] == "DEPOT_CONNECTION_SUCCESSFUL"
    assert "SECRET" not in json.dumps(result)
    await connector.aclose()


@pytest.mark.asyncio
async def test_depot_set_maps_vendor_rejection_to_depot_error() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.put(_DEPOT_PATH).respond(
            500,
            json={
                "errorCode": "VMWARE_DEPOT_CONNECT_FAILURE",
                "message": "Secure protocol communication error",
            },
        )
        result = await connector.system_depot_set(
            _make_operator(), _TARGET, {"settings": _depot_settings()}
        )

    assert result["status"] == "depot_error"
    assert result["http_status"] == 500
    assert result["error_code"] == "VMWARE_DEPOT_CONNECT_FAILURE"
    assert "trusted-certificates.add" in result["remediation"]
    # A vendor rejection is not an auth failure: no re-login.
    assert token_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_depot_set_propagates_401_for_session_recovery() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.put(_DEPOT_PATH).respond(401, json={"message": "token expired"})
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.system_depot_set(
                _make_operator(), _TARGET, {"settings": _depot_settings()}
            )

    # The raw 401 must reach the dispatcher's #2067 recovery arm, never map
    # to depot_error.
    assert exc_info.value.response.status_code == 401
    await connector.aclose()


@pytest.mark.asyncio
async def test_trusted_certificates_list_reads_store() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.get(_CERTS_PATH).respond(200, json={"elements": []})
        result = await connector.system_trusted_certificates_list(_make_operator(), _TARGET, {})

    assert result == {"elements": []}
    await connector.aclose()


@pytest.mark.asyncio
async def test_trusted_certificate_add_defaults_usage_type() -> None:
    connector = _make_connector()
    pem = "-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n"
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        add_route = mock.post(_CERTS_PATH).respond(200, json={})
        result = await connector.system_trusted_certificate_add(
            _make_operator(), _TARGET, {"certificate": pem}
        )

    assert json.loads(add_route.calls[0].request.content) == {
        "certificate": pem,
        "certificateUsageType": "TRUSTED_FOR_OUTBOUND",
    }
    assert result["status"] == "ok"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bundles_list_reads_inventory() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.get(_BUNDLES_PATH).respond(
            200, json={"elements": [{"id": "bundle-1", "downloadStatus": "PENDING"}]}
        )
        result = await connector.bundles_list(_make_operator(), _TARGET, {})

    assert result["elements"][0]["id"] == "bundle-1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bundles_download_collects_partial_failures() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        ok_route = mock.patch(f"{_BUNDLES_PATH}/bundle-1").respond(
            202, json={"id": "task-1", "name": "Download BUNDLE - X"}
        )
        mock.patch(f"{_BUNDLES_PATH}/bundle-2").respond(
            400, json={"errorCode": "BUNDLE_ALREADY_DOWNLOADED", "message": "already local"}
        )
        result = await connector.bundles_download(
            _make_operator(), _TARGET, {"bundle_ids": ["bundle-1", "bundle-2"]}
        )

    assert json.loads(ok_route.calls[0].request.content) == {
        "bundleDownloadSpec": {"downloadNow": True}
    }
    assert result["status"] == "partial"
    assert result["accepted"] == 1
    assert result["failed"] == 1
    assert result["results"][0] == {
        "id": "bundle-1",
        "status": "accepted",
        "task_id": "task-1",
        "task_name": "Download BUNDLE - X",
    }
    assert result["results"][1]["error_code"] == "BUNDLE_ALREADY_DOWNLOADED"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bundles_download_propagates_401_for_session_recovery() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.patch(f"{_BUNDLES_PATH}/bundle-1").respond(401, json={"message": "token expired"})
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.bundles_download(
                _make_operator(), _TARGET, {"bundle_ids": ["bundle-1"]}
            )

    assert exc_info.value.response.status_code == 401
    await connector.aclose()


def test_depot_family_registers_preview_builders() -> None:
    """Each #3121 write parks — each registers its own preview builder."""
    from meho_backplane.connectors.vcf_installer import typed_writes as _tw

    assert _PREVIEW_BUILDERS.get(_tw.INSTALLER_DEPOT_SET_OP_ID) is _bringup._depot_set_preview
    assert (
        _PREVIEW_BUILDERS.get(_tw.INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID)
        is _bringup._trusted_certificate_add_preview
    )
    assert (
        _PREVIEW_BUILDERS.get(_tw.INSTALLER_BUNDLES_DOWNLOAD_OP_ID)
        is _bringup._bundles_download_preview
    )

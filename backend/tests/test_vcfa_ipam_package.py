# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The IPAM provider package import of the Automation connectors.

Drives ``vcfa.tenant.ipam_package.import`` through the real dispatcher: the
download from a URL, the vendor's resumable upload (tus), the import call,
the clean-up of MEHO's temporary file, and the preview of this destructive
op. All names, hosts and ids are made up.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vmware_shared import vcfa_ipam_package
from meho_backplane.connectors.vmware_shared.vcfa_ipam_package import (
    TEMP_PREFIX,
    remove_leftovers,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests import _vcfa_integration_support as _support
from tests._vcfa_integration_support import (
    BASE_URL,
    IMPORT,
    PROVIDERS,
    body,
    mount_logins_full,
)
from tests._vcfa_provisioning_support import OPERATOR, resolved_target, run

# Fixtures shared through the support module.
_env_and_caches = _support._env_and_caches
vcfa = _support.vcfa


PACKAGE_URL = "https://files.example.test/packages/example-ipam.zip"
UPLOAD = f"{IMPORT}/upload-1"


def _package(version: str = "1.5.4") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("registration.yaml", f'name: "example-ipam"\nversion: "{version}"\n')
        archive.writestr("bundle.zip", b"x" * 500)
    return buffer.getvalue()


PACKAGE = _package()


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcfa_ipam_package, "UPLOAD_CHUNK_BYTES", 256)


def _patch_ok(request: httpx.Request) -> httpx.Response:
    offset = int(request.headers["Upload-Offset"]) + len(request.content)
    return httpx.Response(204, headers={"Upload-Offset": str(offset), "Tus-Resumable": "1.0.0"})


def _mount_upload(
    mock: respx.MockRouter, *, providers_after: list[dict[str, Any]]
) -> dict[str, Any]:
    routes = {
        "download": mock.get(PACKAGE_URL).respond(200, content=PACKAGE),
        "providers": mock.get(PROVIDERS).mock(
            side_effect=[
                httpx.Response(200, json={"content": []}),
                httpx.Response(200, json={"content": providers_after}),
            ]
        ),
        "import": mock.post(IMPORT).mock(
            side_effect=[
                httpx.Response(201, headers={"Location": UPLOAD}),
                httpx.Response(
                    201,
                    json={
                        "providerId": "prov-new",
                        "providerName": "example-ipam",
                        "providerVersion": "1.5.4",
                    },
                ),
            ]
        ),
        "patch": mock.patch(UPLOAD).mock(side_effect=_patch_ok),
    }
    return routes


_NEW_PROVIDER = {"id": "prov-new", "name": "example-ipam", "version": "1.5.4"}


async def test_import_waits_for_a_person_and_a_preview(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        download = mock.get(PACKAGE_URL)
        result = await run(
            "vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL}, approved=False
        )
    assert result["status"] == "denied", result
    assert result["extras"]["error_code"] == "preview_binding_required"
    assert not download.called


async def test_a_good_import(vcfa: VcfAutomationConnector) -> None:
    sha = hashlib.sha256(PACKAGE).hexdigest()
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        routes = _mount_upload(mock, providers_after=[_NEW_PROVIDER])
        result = await run(
            "vcfa.tenant.ipam_package.import",
            {"package_url": PACKAGE_URL, "expected_sha256": sha.upper()},
        )
    data = result["result"]
    assert data["status"] == "imported", data
    assert data["provider"] == _NEW_PROVIDER
    assert data["package"] == {
        "file_name": "example-ipam.zip",
        "size": len(PACKAGE),
        "sha256": sha,
        "source": PACKAGE_URL,
        "target": "/provisioning/ipam/api/providers/packages/import",
        "provider_name": "example-ipam",
        "provider_version": "1.5.4",
    }
    assert data["temp_file_removed"] is True
    assert all(step["result"] == "done" for step in data["steps"])
    start, finish = routes["import"].calls
    assert finish.request.headers["Tus-Resumable"] == "1.0.0"
    assert start.request.headers["Upload-Length"] == str(len(PACKAGE))
    assert start.request.headers["Tus-Resumable"] == "1.0.0"
    assert body(routes["import"]) == {"bundleId": "upload-1"}
    assert routes["patch"].call_count == -(-len(PACKAGE) // 256)
    sent = b"".join(call.request.content for call in routes["patch"].calls)
    assert sent == PACKAGE
    assert "Authorization" not in routes["download"].calls.last.request.headers


async def test_a_second_import_of_the_same_version(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(PACKAGE_URL).respond(200, content=PACKAGE)
        mock.get(PROVIDERS).respond(200, json=[_NEW_PROVIDER])
        start = mock.post(IMPORT)
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL})
    data = result["result"]
    assert data["status"] == "unchanged"
    assert data["provider"]["id"] == "prov-new"
    assert not start.called
    assert data["temp_file_removed"] is True


async def test_an_upload_that_expired(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        routes = _mount_upload(mock, providers_after=[])
        routes["patch"].mock(side_effect=[httpx.Response(404)])
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL})
    data = result["result"]
    assert data["status"] == "failed"
    steps = {step["step"]: step for step in data["steps"]}
    assert steps["upload"]["result"] == "failed"
    assert "dropped the upload" in steps["upload"]["error"]
    assert steps["import"]["result"] == "not_run"
    assert data["temp_file_removed"] is True


async def test_the_login_expires_during_the_upload(vcfa: VcfAutomationConnector) -> None:
    """A long upload logs in again when the vendor login expires part-way."""
    calls = {"n": 0}

    def patch(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(401)
        return _patch_ok(request)

    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        login = mock.post(f"{BASE_URL}/iaas/api/login").respond(200, json={"token": "t"})
        routes = _mount_upload(mock, providers_after=[_NEW_PROVIDER])
        routes["patch"].mock(side_effect=patch)
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL})
    assert result["result"]["status"] == "imported", result
    assert login.call_count == 2


async def test_a_failure_in_the_middle_still_cleans_up(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        routes = _mount_upload(mock, providers_after=[])
        routes["import"].mock(
            side_effect=[
                httpx.Response(201, headers={"Location": UPLOAD}),
                httpx.Response(500, json={"message": "Provider bundle is not valid"}),
            ]
        )
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL})
    data = result["result"]
    assert data["status"] == "failed"
    assert [s["result"] for s in data["steps"]] == ["done", "done", "done", "failed", "not_run"]
    assert "not valid" in data["steps"][3]["error"]
    assert data["package"]["sha256"]
    assert data["temp_file_removed"] is True


async def test_a_redirect_is_not_followed(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(PACKAGE_URL).respond(302, headers={"Location": "https://other.example.test/x"})
        other = mock.get("https://other.example.test/x")
        start = mock.post(IMPORT)
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": PACKAGE_URL})
    data = result["result"]
    assert data["status"] == "failed"
    assert "redirect" in data["steps"][0]["error"]
    assert "other.example.test" not in str(data)
    assert not other.called
    assert not start.called


async def test_a_wrong_sha256_stops_before_the_upload(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(PACKAGE_URL).respond(200, content=PACKAGE)
        start = mock.post(IMPORT)
        result = await run(
            "vcfa.tenant.ipam_package.import",
            {"package_url": PACKAGE_URL, "expected_sha256": "0" * 64},
        )
    data = result["result"]
    assert data["status"] == "failed"
    assert "SHA-256" in data["steps"][1]["error"]
    assert not start.called


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@files.example.test/p.zip",
        "https://files.example.test/p.zip?sig=abc",
        "http://files.example.test/p.zip",
    ],
)
async def test_a_url_with_a_credential_is_refused(vcfa: VcfAutomationConnector, url: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        download = mock.get(url)
        result = await run("vcfa.tenant.ipam_package.import", {"package_url": url})
    assert result["status"] == "error"
    assert "pw" not in str(result.get("error"))
    assert not download.called


def _ctx(vcfa: VcfAutomationConnector, target: Any) -> Any:
    return SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=target,
        params={"package_url": PACKAGE_URL},
        descriptor=SimpleNamespace(op_id="vcfa.tenant.ipam_package.import"),
    )


async def test_preview_fills_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.ipam_package.import"]
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.head(PACKAGE_URL).respond(200, headers={"Content-Length": "1234"})
        mock.get(PROVIDERS).respond(200, json={"content": [_NEW_PROVIDER]})
        preview = await builder(_ctx(vcfa, await resolved_target()))
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "ipam_provider_package",
        "source": PACKAGE_URL,
        "size": "1234",
    }
    assert blast["children"] == [{"kind": "ipam_provider", **_NEW_PROVIDER}]
    assert blast["irreversibility"]


async def test_preview_refuses_a_missing_package(vcfa: VcfAutomationConnector) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.ipam_package.import"]
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.head(PACKAGE_URL).respond(404)
        with pytest.raises(LookupError):
            await builder(_ctx(vcfa, await resolved_target()))


def test_leftovers_of_a_dead_run_are_removed(tmp_path: Path) -> None:
    old = tmp_path / f"{TEMP_PREFIX}dead.zip.part"
    new = tmp_path / f"{TEMP_PREFIX}running.zip.part"
    other = tmp_path / "someone-else.zip"
    for path in (old, new, other):
        path.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(other, (two_hours_ago, two_hours_ago))
    assert remove_leftovers(str(tmp_path)) == [old.name]
    assert not old.exists()
    assert new.exists()
    assert other.exists()

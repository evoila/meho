# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Harbor CVE-detail read typed op (#2857).

Covers ``harbor.artifact.vulnerabilities`` — the per-artifact vulnerability
list behind ``harbor.artifact.info``'s ``scan_overview`` severity counts:

* The handler projects Harbor's native scan report (pluggable-scanner-spec
  v1.1) down to ``{id, package, version, fix_version, severity, description,
  links}`` — the acceptance-criterion shape (a CVE id field is present).
* The ``X-Accept-Vulnerabilities`` header pins the current native report
  format.
* Path resolution matches ``harbor.artifact.info`` (project / repository /
  reference percent-encoded; a nested ``repository_name`` slash and a
  ``sha256:`` digest ``:`` are encoded).
* The MIME-keyed report envelope is unwrapped (and a bare report tolerated).
* A clean (scanned, zero-finding) artifact yields an empty list; a 404
  (never-scanned) propagates as ``httpx.HTTPStatusError``.
* The dispatched operator threads into the handler via ``dispatch_typed``.
* ``classify_op`` maps the op-id to ``read`` (broadcast sensitivity).

Auth follows the robot-op suite: HTTP Basic (shared service account), the
dispatched :class:`Operator` forwarded through
:meth:`HarborConnector.auth_headers` to the operator-context Vault read
against the in-process Vault fake (``install_fake_client``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.operations._branches import dispatch_typed
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CANARY_USERNAME = "svc-harbor-cve-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-cve-harbor"

_ACCEPT_HEADER = "x-accept-vulnerabilities"
_ACCEPT_VALUE = "application/vnd.security.vulnerability.report; version=1.1"


# ---------------------------------------------------------------------------
# Isolation fixtures (mirror test_connectors_harbor_robot.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    """Re-register HarborConnector before each test, clear after."""
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the chassis env vars Settings reads (Vault client construction)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="targets/harbor/harbor-test",
)


def _make_operator(raw_jwt: str = "op.cve.harbor.jwt") -> Any:
    from meho_backplane.auth.operator import Operator, TenantRole

    return Operator(
        sub="op-cve-harbor",
        name="Harbor CVE Operator",
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c5"),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_connector() -> HarborConnector:
    """Connector with the DEFAULT (live) loader — no injected stub."""
    return HarborConnector()


def _vuln_url(project: str, repository: str, reference: str) -> str:
    return (
        f"https://harbor.test.invalid/api/v2.0/projects/{project}"
        f"/repositories/{repository}/artifacts/{reference}/additions/vulnerabilities"
    )


def _native_report(vulnerabilities: list[dict[str, Any]], *, severity: str = "Critical") -> dict:
    """A HarborVulnerabilityReport keyed by the resolved MIME type."""
    return {
        _ACCEPT_VALUE: {
            "generated_at": "2026-08-01T12:00:00Z",
            "scanner": {"name": "Trivy", "vendor": "Aqua Security", "version": "0.52.0"},
            "severity": severity,
            "vulnerabilities": vulnerabilities,
        }
    }


_TWO_VULNS = [
    {
        "id": "CVE-2024-3094",
        "package": "xz-utils",
        "version": "5.6.0",
        "fix_version": "5.6.2",
        "severity": "Critical",
        "description": "Malicious backdoor in the upstream xz release tarballs.",
        "links": ["https://nvd.nist.gov/vuln/detail/CVE-2024-3094"],
        "cwe_ids": ["CWE-506"],
    },
    {
        "id": "CVE-2023-45853",
        "package": "zlib",
        "version": "1.2.13",
        "fix_version": "",
        "severity": "High",
        "description": "MiniZip integer overflow.",
        "links": [],
    },
]


# ---------------------------------------------------------------------------
# Projection — the acceptance-criterion shape (CVE id field present)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_projects_native_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler projects each native VulnerabilityItem to the agent shape,
    including the CVE id, and surfaces the overall severity + scanner."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_vuln_url("library", "nginx", "v1.0.0")).mock(
            return_value=respx.MockResponse(200, json=_native_report(_TWO_VULNS))
        )
        result = await connector.artifact_vulnerabilities(
            _make_operator(),
            _TARGET,
            {"project_name": "library", "repository_name": "nginx", "reference": "v1.0.0"},
        )

    assert result["severity"] == "Critical"
    assert result["scanner"] == {"name": "Trivy", "vendor": "Aqua Security", "version": "0.52.0"}
    assert result["generated_at"] == "2026-08-01T12:00:00Z"
    assert [v["id"] for v in result["vulnerabilities"]] == ["CVE-2024-3094", "CVE-2023-45853"]
    first = result["vulnerabilities"][0]
    # Exactly the projected keys — no scanner-native extras (cwe_ids) leak.
    assert set(first) == {
        "id",
        "package",
        "version",
        "fix_version",
        "severity",
        "description",
        "links",
    }
    assert first["package"] == "xz-utils"
    assert first["version"] == "5.6.0"
    assert first["fix_version"] == "5.6.2"
    assert first["severity"] == "Critical"
    assert first["description"].startswith("Malicious backdoor")
    assert first["links"] == ["https://nvd.nist.gov/vuln/detail/CVE-2024-3094"]
    # A scanner that omits links yields [] rather than None.
    assert result["vulnerabilities"][1]["links"] == []
    await connector.aclose()


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_sends_accept_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read pins X-Accept-Vulnerabilities to the current native format."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured.update(dict(request.headers))
        return respx.MockResponse(200, json=_native_report([]))

    with respx.mock() as mock:
        mock.get(_vuln_url("library", "nginx", "v1.0.0")).mock(side_effect=_capture)
        await connector.artifact_vulnerabilities(
            _make_operator(),
            _TARGET,
            {"project_name": "library", "repository_name": "nginx", "reference": "v1.0.0"},
        )

    assert captured[_ACCEPT_HEADER] == _ACCEPT_VALUE
    assert captured["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_encodes_path_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested repository_name slash and sha256: reference colon are encoded —
    the same resolution harbor.artifact.info uses (OpenAPI simple style)."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_url: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured_url["path"] = request.url.raw_path.decode()
        return respx.MockResponse(200, json=_native_report([]))

    with respx.mock() as mock:
        mock.get(url__regex=r".*/additions/vulnerabilities$").mock(side_effect=_capture)
        await connector.artifact_vulnerabilities(
            _make_operator(),
            _TARGET,
            {
                "project_name": "library",
                "repository_name": "team/nginx",
                "reference": "sha256:abc123",
            },
        )

    assert "team%2Fnginx" in captured_url["path"]
    assert "sha256%3Aabc123" in captured_url["path"]
    # The encoded separators must not leak as literal path structure.
    assert "team/nginx" not in captured_url["path"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_handles_bare_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report NOT keyed by MIME type (bare shape) is still unwrapped."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    bare = {
        "severity": "Low",
        "scanner": {"name": "Trivy"},
        "vulnerabilities": [
            {"id": "CVE-2020-0001", "package": "p", "version": "1", "severity": "Low"}
        ],
    }
    with respx.mock() as mock:
        mock.get(_vuln_url("library", "nginx", "v1.0.0")).mock(
            return_value=respx.MockResponse(200, json=bare)
        )
        result = await connector.artifact_vulnerabilities(
            _make_operator(),
            _TARGET,
            {"project_name": "library", "repository_name": "nginx", "reference": "v1.0.0"},
        )

    assert result["severity"] == "Low"
    assert result["vulnerabilities"][0]["id"] == "CVE-2020-0001"
    # A missing field (fix_version) projects to None, links to [].
    assert result["vulnerabilities"][0]["fix_version"] is None
    assert result["vulnerabilities"][0]["links"] == []
    await connector.aclose()


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_empty_report_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean (scanned, zero-finding) artifact yields an empty list."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_vuln_url("library", "nginx", "clean")).mock(
            return_value=respx.MockResponse(200, json=_native_report([], severity="None"))
        )
        result = await connector.artifact_vulnerabilities(
            _make_operator(),
            _TARGET,
            {"project_name": "library", "repository_name": "nginx", "reference": "clean"},
        )

    assert result["vulnerabilities"] == []
    assert result["severity"] == "None"
    await connector.aclose()


@pytest.mark.asyncio
async def test_artifact_vulnerabilities_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-scanned artifact (Harbor 404) propagates httpx.HTTPStatusError."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_vuln_url("library", "nginx", "unscanned")).mock(
            return_value=respx.MockResponse(404, json={"errors": [{"code": "NOT_FOUND"}]})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.artifact_vulnerabilities(
                _make_operator(),
                _TARGET,
                {"project_name": "library", "repository_name": "nginx", "reference": "unscanned"},
            )
    await connector.aclose()


@pytest.mark.asyncio
async def test_dispatch_typed_threads_operator_into_artifact_vulnerabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dispatcher threads the dispatched operator into the handler."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()
    with respx.mock() as mock:
        mock.get(_vuln_url("library", "nginx", "v1.0.0")).mock(
            return_value=respx.MockResponse(200, json=_native_report(_TWO_VULNS))
        )
        result = await dispatch_typed(
            handler=connector.artifact_vulnerabilities,
            operator=operator,
            target=_TARGET,
            params={"project_name": "library", "repository_name": "nginx", "reference": "v1.0.0"},
        )

    assert [v["id"] for v in result["vulnerabilities"]] == ["CVE-2024-3094", "CVE-2023-45853"]
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    await connector.aclose()


# ---------------------------------------------------------------------------
# Broadcast classification — a non-mutating read
# ---------------------------------------------------------------------------


def test_classify_op_harbor_artifact_vulnerabilities_is_read() -> None:
    """harbor.artifact.vulnerabilities classifies as read, like its info sibling."""
    assert classify_op("harbor.artifact.vulnerabilities") == "read"
    assert classify_op("harbor.artifact.info") == "read"

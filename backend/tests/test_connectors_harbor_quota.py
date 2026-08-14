# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Harbor storage-quota read typed ops (#2858).

Covers the two standalone typed reads plus the ``harbor.project.info``
overpromise fix:

* ``harbor.project.summary`` — per-project storage-quota occupancy. The
  handler projects Harbor's native ``ProjectSummary`` to
  ``{repo_count, quota: {hard, used}, member_counts}`` (``quota.hard.storage``
  / ``quota.used.storage`` in bytes) and sends the ``X-Is-Resource-Name``
  header so a project *name* is never misread as an id.
* ``harbor.quota.list`` — fleet-wide project quotas. The handler sends
  ``reference=project`` + a ``-used.storage`` default sort and projects each
  ``Quota`` to ``{id, ref, hard, used}`` as a bare list (JSONFlux-reducible).
* Both op-ids classify as ``read`` (broadcast sensitivity), matching the
  ``harbor.project.info`` sibling.
* The overpromise fix: ``harbor.project.info``'s ``llm_instructions`` no
  longer claim ``quota`` / ``chart_count``, and the acceptance ``Project``
  fixtures carry neither field.

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
from meho_backplane.connectors.harbor.typed_reads import HARBOR_READ_LLM_INSTRUCTIONS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.operations._branches import dispatch_typed
from meho_backplane.settings import get_settings
from tests.acceptance._harbor_canary_fixtures import (
    HARBOR_CANARY_PROJECT_DETAIL,
    HARBOR_CANARY_PROJECTS,
)

from ._vault_fakes import install_fake_client

_CANARY_USERNAME = "svc-harbor-quota-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-quota-harbor"

_RESOURCE_NAME_HEADER = "x-is-resource-name"


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


def _make_operator(raw_jwt: str = "op.quota.harbor.jwt") -> Any:
    from meho_backplane.auth.operator import Operator, TenantRole

    return Operator(
        sub="op-quota-harbor",
        name="Harbor Quota Operator",
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d5"),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_connector() -> HarborConnector:
    """Connector with the DEFAULT (live) loader — no injected stub."""
    return HarborConnector()


def _summary_url(project: str) -> str:
    return f"https://harbor.test.invalid/api/v2.0/projects/{project}/summary"


_QUOTAS_URL = "https://harbor.test.invalid/api/v2.0/quotas"


def _project_summary(*, used: int, hard: int, repo_count: int = 3) -> dict[str, Any]:
    """A native Harbor ProjectSummary keyed as the vendor returns it."""
    return {
        "repo_count": repo_count,
        "project_admin_count": 1,
        "maintainer_count": 2,
        "developer_count": 3,
        "guest_count": 4,
        "limited_guest_count": 0,
        "quota": {"hard": {"storage": hard}, "used": {"storage": used}},
        "registry": None,
    }


def _quota_row(quota_id: int, name: str, *, used: int, hard: int) -> dict[str, Any]:
    """A native Harbor Quota object (reference=project)."""
    return {
        "id": quota_id,
        "ref": {"id": quota_id, "name": name, "owner_name": "admin"},
        "hard": {"storage": hard},
        "used": {"storage": used},
        "creation_time": "2026-01-01T00:00:00.000Z",
        "update_time": "2026-08-01T00:00:00.000Z",
    }


# ---------------------------------------------------------------------------
# harbor.project.summary — projection + quota bytes (AC2 / AC5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_summary_projects_quota_and_member_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler surfaces repo_count, quota.hard/used storage bytes, and
    the per-role member counts, dropping the proxy-only registry field."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_summary_url("library")).mock(
            return_value=respx.MockResponse(
                200, json=_project_summary(used=1073741824, hard=5368709120)
            )
        )
        result = await connector.project_summary(
            _make_operator(), _TARGET, {"project_name": "library"}
        )

    assert result["repo_count"] == 3
    assert result["quota"]["used"]["storage"] == 1073741824
    assert result["quota"]["hard"]["storage"] == 5368709120
    assert result["member_counts"] == {
        "project_admin": 1,
        "maintainer": 2,
        "developer": 3,
        "guest": 4,
        "limited_guest": 0,
    }
    # The projection is exactly the three curated keys — no registry leak.
    assert set(result) == {"repo_count", "quota", "member_counts"}
    assert "registry" not in result
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_summary_sends_resource_name_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read pins X-Is-Resource-Name so a name is never misread as an id."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured.update(dict(request.headers))
        return respx.MockResponse(200, json=_project_summary(used=0, hard=-1))

    with respx.mock() as mock:
        mock.get(_summary_url("library")).mock(side_effect=_capture)
        await connector.project_summary(_make_operator(), _TARGET, {"project_name": "library"})

    assert captured[_RESOURCE_NAME_HEADER] == "true"
    assert captured["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_summary_encodes_numeric_looking_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numeric-looking project name is sent as a path segment (+ the header),
    so Harbor resolves it as a name, not a project id."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_url: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured_url["path"] = request.url.raw_path.decode()
        return respx.MockResponse(200, json=_project_summary(used=1, hard=2))

    with respx.mock() as mock:
        mock.get(url__regex=r".*/summary$").mock(side_effect=_capture)
        await connector.project_summary(_make_operator(), _TARGET, {"project_name": "12345"})

    assert captured_url["path"].endswith("/projects/12345/summary")
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_summary_tolerates_missing_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary without a quota object yields empty hard/used maps, not a crash."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_summary_url("library")).mock(
            return_value=respx.MockResponse(200, json={"repo_count": 0})
        )
        result = await connector.project_summary(
            _make_operator(), _TARGET, {"project_name": "library"}
        )

    assert result["quota"] == {"hard": {}, "used": {}}
    assert result["repo_count"] == 0
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_summary_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown project (Harbor 404) propagates httpx.HTTPStatusError."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_summary_url("ghost")).mock(
            return_value=respx.MockResponse(404, json={"errors": [{"code": "NOT_FOUND"}]})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.project_summary(_make_operator(), _TARGET, {"project_name": "ghost"})
    await connector.aclose()


@pytest.mark.asyncio
async def test_dispatch_typed_threads_operator_into_project_summary(
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
        mock.get(_summary_url("library")).mock(
            return_value=respx.MockResponse(200, json=_project_summary(used=10, hard=100))
        )
        result = await dispatch_typed(
            handler=connector.project_summary,
            operator=operator,
            target=_TARGET,
            params={"project_name": "library"},
        )

    assert result["quota"]["used"]["storage"] == 10
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    await connector.aclose()


# ---------------------------------------------------------------------------
# harbor.quota.list — fleet-wide projection + query params (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_list_projects_rows_and_sends_default_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler returns {quotas: [{id, ref, hard, used}, ...]}, and sends
    reference=project + the -used.storage default sort."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_params: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured_params.update(dict(request.url.params))
        return respx.MockResponse(
            200,
            json=[
                _quota_row(1, "library", used=1073741824, hard=-1),
                _quota_row(2, "sandbox", used=536870912, hard=1073741824),
            ],
        )

    with respx.mock() as mock:
        mock.get(_QUOTAS_URL).mock(side_effect=_capture)
        result = await connector.quota_list(_make_operator(), _TARGET, {})

    assert captured_params["reference"] == "project"
    assert captured_params["sort"] == "-used.storage"
    rows = result["quotas"]
    assert [row["ref"]["name"] for row in rows] == ["library", "sandbox"]
    first = rows[0]
    assert set(first) == {"id", "ref", "hard", "used"}
    assert first["used"]["storage"] == 1073741824
    assert first["hard"]["storage"] == -1
    # creation_time / update_time are dropped from the projection.
    assert "creation_time" not in first
    await connector.aclose()


@pytest.mark.asyncio
async def test_quota_list_passes_custom_sort_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom sort / page / page_size reach the vendor query string."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_params: dict[str, str] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        captured_params.update(dict(request.url.params))
        return respx.MockResponse(200, json=[])

    with respx.mock() as mock:
        mock.get(_QUOTAS_URL).mock(side_effect=_capture)
        await connector.quota_list(
            _make_operator(),
            _TARGET,
            {"sort": "used.storage", "page": 2, "page_size": 25},
        )

    assert captured_params["sort"] == "used.storage"
    assert captured_params["page"] == "2"
    assert captured_params["page_size"] == "25"
    assert captured_params["reference"] == "project"
    await connector.aclose()


@pytest.mark.asyncio
async def test_quota_list_empty_fleet_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No project quotas yields an empty list (not None, not a crash)."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_QUOTAS_URL).mock(return_value=respx.MockResponse(200, json=[]))
        result = await connector.quota_list(_make_operator(), _TARGET, {})

    assert result == {"quotas": []}
    await connector.aclose()


@pytest.mark.asyncio
async def test_quota_list_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor 5xx propagates httpx.HTTPStatusError."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.get(_QUOTAS_URL).mock(return_value=respx.MockResponse(500))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.quota_list(_make_operator(), _TARGET, {})
    await connector.aclose()


@pytest.mark.asyncio
async def test_dispatch_typed_threads_operator_into_quota_list(
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
        mock.get(_QUOTAS_URL).mock(
            return_value=respx.MockResponse(200, json=[_quota_row(1, "library", used=42, hard=-1)])
        )
        result = await dispatch_typed(
            handler=connector.quota_list,
            operator=operator,
            target=_TARGET,
            params={},
        )

    assert result["quotas"][0]["ref"]["name"] == "library"
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    await connector.aclose()


# ---------------------------------------------------------------------------
# Broadcast classification — both reads match the info sibling
# ---------------------------------------------------------------------------


def test_classify_op_harbor_quota_reads_are_read() -> None:
    """Both new ops classify as read, like harbor.project.info."""
    assert classify_op("harbor.project.summary") == "read"
    assert classify_op("harbor.quota.list") == "read"
    assert classify_op("harbor.project.info") == "read"


# ---------------------------------------------------------------------------
# Overpromise fix — project.info + the acceptance fixtures (AC1 / AC4 / AC5)
# ---------------------------------------------------------------------------


def _project_info_instructions() -> dict[str, object]:
    # The read core was promoted to typed ops (#2915/#2856): the
    # harbor.project.info curation now lives in typed_reads, keyed by the
    # dot-form op id, not the retired HARBOR_CORE_OPS METHOD:/path table.
    return HARBOR_READ_LLM_INSTRUCTIONS["harbor.project.info"]


def test_project_info_llm_instructions_drop_quota_and_chart_count() -> None:
    """harbor.project.info no longer advertises quota / chart_count fields the
    real Harbor Project object does not carry."""
    blob = " ".join(str(v) for v in _project_info_instructions().values())
    # No chart_count field claim (in either spelling) — Harbor 2.x has none.
    assert "chart_count" not in blob
    assert "chart count" not in blob
    # The old output_shape's "quota (used/hard storage in bytes)" claim is gone;
    # quota is now only mentioned as the repoint to the summary op.
    assert "quota (used/hard storage in bytes)" not in blob
    assert "harbor.project.summary" in blob


def test_project_info_repoints_quota_at_summary_op() -> None:
    """The overpromised quota guidance now points at harbor.project.summary."""
    instructions = _project_info_instructions()
    assert "harbor.project.summary" in str(instructions["next_step"])
    assert "harbor.project.summary" in str(instructions["output_shape"])


def test_acceptance_project_fixtures_carry_no_quota_or_chart_count() -> None:
    """The Project canary fixtures stop masking the real (quota-less) shape."""
    assert "quota" not in HARBOR_CANARY_PROJECT_DETAIL
    assert "chart_count" not in HARBOR_CANARY_PROJECT_DETAIL
    for project in HARBOR_CANARY_PROJECTS:
        assert "quota" not in project
        assert "chart_count" not in project

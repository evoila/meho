# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``harbor.artifact.copy`` with a faked Harbor API."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops_artifact_copy import (
    _ARTIFACT_COPY_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_BASE = "https://harbor.test.invalid/api/v2.0/projects"


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
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


def _operator() -> Operator:
    return Operator(
        sub="op-artifact-copy",
        name="Harbor Copy Operator",
        email=None,
        raw_jwt="op.artifact.copy.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b4"),
        tenant_role=TenantRole.OPERATOR,
    )


def _install_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(
        monkeypatch,
        secret={"username": "svc-harbor-canary", "password": "p4ss-canary-artifact-copy"},
    )


_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_SRC_TAG_URL = f"{_BASE}/cache/repositories/library%2Fnginx/artifacts/1.27"
_DST_ARTIFACTS_URL = f"{_BASE}/apps/repositories/nginx/artifacts"
_PARAMS_TAG = {"project": "apps", "repository": "nginx", "from": "cache/library/nginx:1.27"}
_PARAMS_DIGEST = {
    "project": "apps",
    "repository": "nginx",
    "from": f"cache/library/nginx@{_DIGEST}",
}


def _dst_url(reference: str) -> str:
    return f"{_DST_ARTIFACTS_URL}/{reference.replace(':', '%3A')}"


@pytest.mark.asyncio
async def test_copy_by_tag_posts_from_and_reports_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    sent: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> respx.MockResponse:
        sent.append(request)
        return respx.MockResponse(201)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_SRC_TAG_URL).mock(return_value=respx.MockResponse(200, json={"digest": _DIGEST}))
        mock.get(_dst_url("1.27")).mock(return_value=respx.MockResponse(404))
        mock.post(_DST_ARTIFACTS_URL).mock(side_effect=_capture)
        result = await connector.artifact_copy(_operator(), _TARGET, _PARAMS_TAG)

    assert len(sent) == 1
    assert sent[0].url.params["from"] == "cache/library/nginx:1.27"
    assert result == {
        "project": "apps",
        "repository": "nginx",
        "from": "cache/library/nginx:1.27",
        "digest": _DIGEST,
        "copied": True,
        "read_back": "harbor.artifact.list",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_copy_by_digest_skips_source_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_dst_url(_DIGEST)).mock(return_value=respx.MockResponse(404))
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        result = await connector.artifact_copy(_operator(), _TARGET, _PARAMS_DIGEST)

    assert post.calls[0].request.url.params["from"] == f"cache/library/nginx@{_DIGEST}"
    assert result["copied"] is True
    assert result["digest"] == _DIGEST
    await connector.aclose()


@pytest.mark.asyncio
async def test_second_call_reports_copied_false_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_dst_url(_DIGEST)).mock(
            return_value=respx.MockResponse(200, json={"digest": _DIGEST})
        )
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        result = await connector.artifact_copy(_operator(), _TARGET, _PARAMS_DIGEST)

    assert post.call_count == 0
    assert result["copied"] is False
    assert result["digest"] == _DIGEST
    await connector.aclose()


@pytest.mark.asyncio
async def test_tag_held_with_same_digest_is_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_SRC_TAG_URL).mock(return_value=respx.MockResponse(200, json={"digest": _DIGEST}))
        mock.get(_dst_url("1.27")).mock(
            return_value=respx.MockResponse(200, json={"digest": _DIGEST})
        )
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        result = await connector.artifact_copy(_operator(), _TARGET, _PARAMS_TAG)

    assert post.call_count == 0
    assert result["copied"] is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_tag_held_with_other_digest_is_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_SRC_TAG_URL).mock(return_value=respx.MockResponse(200, json={"digest": _DIGEST}))
        mock.get(_dst_url("1.27")).mock(
            return_value=respx.MockResponse(200, json={"digest": _OTHER_DIGEST})
        )
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        result = await connector.artifact_copy(_operator(), _TARGET, _PARAMS_TAG)

    assert post.call_count == 1
    assert result["copied"] is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_missing_source_raises_and_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_SRC_TAG_URL).mock(return_value=respx.MockResponse(404))
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        with pytest.raises(LookupError):
            await connector.artifact_copy(_operator(), _TARGET, _PARAMS_TAG)
    assert post.call_count == 0
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500, 502])
async def test_failed_target_read_is_raised_not_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_dst_url(_DIGEST)).mock(return_value=respx.MockResponse(status))
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.artifact_copy(_operator(), _TARGET, _PARAMS_DIGEST)
    assert post.call_count == 0
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500])
async def test_copy_post_errors_are_raised_once(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_dst_url(_DIGEST)).mock(return_value=respx.MockResponse(404))
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(status))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.artifact_copy(_operator(), _TARGET, _PARAMS_DIGEST)
    assert post.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_target_without_digest_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_dst_url(_DIGEST)).mock(return_value=respx.MockResponse(200, json={}))
        post = mock.post(_DST_ARTIFACTS_URL).mock(return_value=respx.MockResponse(201))
        with pytest.raises(ValueError):
            await connector.artifact_copy(_operator(), _TARGET, _PARAMS_DIGEST)
    assert post.call_count == 0
    await connector.aclose()


def test_artifact_copy_schema_refuses_bad_input() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_ARTIFACT_COPY_PARAMETER_SCHEMA)
    assert not list(validator.iter_errors(_PARAMS_TAG))
    assert not list(validator.iter_errors(_PARAMS_DIGEST))
    for bad in (
        {},
        {**_PARAMS_TAG, "from": "no-slash:1"},
        {**_PARAMS_TAG, "from": "cache/library/nginx"},
        {**_PARAMS_TAG, "from": "cache/library/nginx@"},
        {**_PARAMS_TAG, "project": " "},
        {**_PARAMS_TAG, "extra": 1},
    ):
        assert list(validator.iter_errors(bad)), bad

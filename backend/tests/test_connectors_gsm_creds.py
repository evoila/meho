# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the GCP Secret Manager credential backend (#2230).

These pin the **secret-free** contract of
:class:`meho_backplane.connectors._shared.gsm_creds.GcpSecretManagerBackend`
without any live GCP: ``google.auth.default`` is replaced through the
backend's ``adc_loader`` seam and ``SecretManagerServiceClient`` through
its ``client_factory`` seam, so a test supplies a canned payload and
asserts the parse / decode / impersonation / error / no-secret-in-logs
behaviour in-process.

What these cover:

* ref grammar — bare (latest), pinned ``/versions/<n>``, ``#field``
  fragment, and malformed refs raising a clear error (not ``IndexError``);
* a bare ref returns the whole JSON object; a ``#field`` ref returns just
  that key; a missing field / non-JSON / non-object payload raises
  :class:`GcpSecretManagerReadError`;
* the read runs under ADC and wraps it in impersonated credentials only
  when a SA is configured (constructor override or ``Settings``);
* access-denied / not-found / transport failures surface as a distinct,
  actionable error, never a bare ``google.api_core`` exception;
* an empty / unresolvable ADC source fails closed;
* no credential value reaches a structlog event;
* the shared loader dispatches a ``gsm:`` ref to this backend end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared import credential_backend as cb
from meho_backplane.connectors._shared.gsm_creds import (
    GcpSecretManagerBackend,
    GcpSecretManagerReadError,
    _WifConfig,
)
from meho_backplane.connectors._shared.vault_creds import (
    load_basic_credentials,
    load_vault_secret_data,
)
from meho_backplane.settings import get_settings

# Canary values that must never appear in a log event.
_CANARY_PASSWORD = "p4ssw0rd-canary-must-not-leak"
_CANARY_USERNAME = "svc-canary"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``get_settings`` reads and clear its cache.

    The end-to-end dispatch tests reach ``get_settings()`` (for the default
    backend + the impersonation SA), so pin the same minimal Keycloak env
    the sibling vault-creds suite pins. ``DATABASE_URL`` is provided by the
    autouse conftest fixture.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("GSM_IMPERSONATE_SA", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test doubles for the ADC + client seams
# ---------------------------------------------------------------------------


class _FakePayload:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeResponse:
    def __init__(self, data: bytes, name: str) -> None:
        self.payload = _FakePayload(data)
        self.name = name


class _FakeClient:
    """Stub ``SecretManagerServiceClient`` — records the accessed name."""

    def __init__(
        self,
        *,
        payload: bytes,
        resolved_name: str = "",
        raises: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._resolved_name = resolved_name
        self._raises = raises
        self.credentials: Any = None
        self.accessed_name: str | None = None

    def access_secret_version(self, *, name: str) -> _FakeResponse:
        self.accessed_name = name
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._payload, self._resolved_name)


def _factory_for(client: _FakeClient):
    """A ``client_factory`` that records the credentials and returns *client*."""

    def factory(*, credentials: Any) -> _FakeClient:
        client.credentials = credentials
        return client

    return factory


class _SourceCreds:
    """Opaque sentinel standing in for an ADC source-credentials object."""


def _adc_loader_returning(creds: Any):
    """An ``adc_loader`` double recording its ``scopes`` kwarg."""
    calls: list[dict[str, Any]] = []

    def loader(**kwargs: Any) -> tuple[Any, str]:
        calls.append(kwargs)
        return creds, "adc-project"

    loader.calls = calls  # type: ignore[attr-defined]
    return loader


def _json_secret(**fields: str) -> bytes:
    return json.dumps(fields).encode("utf-8")


@dataclass
class _Target:
    """Minimal target satisfying ``BasicCredentialsTargetLike``."""

    name: str = "gcp-lab-01"
    host: str = "gcp-lab-01.example.test"
    secret_ref: str | None = "gsm:my-project/db-creds"


def _make_operator(jwt: str = "op.jwt.value") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Ref grammar
# ---------------------------------------------------------------------------


async def test_bare_ref_returns_whole_json_payload() -> None:
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    data = await backend.load_secret_data(
        "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
    )

    assert data == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    assert client.accessed_name == "projects/my-project/secrets/db-creds/versions/latest"


async def test_field_fragment_selects_single_key() -> None:
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    data = await backend.load_secret_data(
        "my-project/db-creds#password", _make_operator(), target_name="gcp-lab-01"
    )

    assert data == {"password": _CANARY_PASSWORD}


async def test_pinned_version_is_addressed() -> None:
    client = _FakeClient(payload=_json_secret(token="abc"))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    await backend.load_secret_data(
        "my-project/db-creds/versions/5#token", _make_operator(), target_name="gcp-lab-01"
    )

    assert client.accessed_name == "projects/my-project/secrets/db-creds/versions/5"


@pytest.mark.parametrize(
    "ref",
    [
        "just-one-segment",
        "proj/secret/extra",
        "proj/secret/notversions/5",
        "my-project/db-creds#",
        "#password",
        "/db-creds",
    ],
)
async def test_malformed_ref_raises_clear_error(ref: str) -> None:
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(_FakeClient(payload=b"{}")),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(ref, _make_operator(), target_name="gcp-lab-01")

    assert "gcp-lab-01" in str(exc.value)


# ---------------------------------------------------------------------------
# Payload decoding errors
# ---------------------------------------------------------------------------


async def test_missing_field_raises_clear_error() -> None:
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds#password", _make_operator(), target_name="gcp-lab-01"
        )

    msg = str(exc.value)
    assert "password" in msg
    assert "my-project/db-creds" in msg
    assert _CANARY_USERNAME not in msg


async def test_non_json_payload_raises_clear_error() -> None:
    client = _FakeClient(payload=b"not-json-at-all")
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "JSON" in str(exc.value)


async def test_non_object_json_payload_raises_clear_error() -> None:
    client = _FakeClient(payload=b'["a", "list"]')
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "JSON object" in str(exc.value)


# ---------------------------------------------------------------------------
# ADC + impersonation
# ---------------------------------------------------------------------------


async def test_reads_under_adc_without_impersonation() -> None:
    source = _SourceCreds()
    loader = _adc_loader_returning(source)
    client = _FakeClient(payload=_json_secret(k="v"))
    backend = GcpSecretManagerBackend(
        adc_loader=loader,
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    await backend.load_secret_data(
        "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
    )

    # No impersonation SA → the client runs under the raw ADC source, with
    # the cloud-platform scope requested.
    assert client.credentials is source
    assert loader.calls[-1]["scopes"] == [  # type: ignore[attr-defined]
        "https://www.googleapis.com/auth/cloud-platform"
    ]


async def test_impersonates_when_sa_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth.impersonated_credentials

    captured: dict[str, Any] = {}
    impersonated_sentinel = object()

    def _fake_impersonated(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return impersonated_sentinel

    monkeypatch.setattr(google.auth.impersonated_credentials, "Credentials", _fake_impersonated)

    source = _SourceCreds()
    client = _FakeClient(payload=_json_secret(k="v"))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(source),
        client_factory=_factory_for(client),
        impersonate_sa="reader@my-project.iam.gserviceaccount.com",
    )

    await backend.load_secret_data(
        "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
    )

    assert captured["source_credentials"] is source
    assert captured["target_principal"] == "reader@my-project.iam.gserviceaccount.com"
    assert captured["target_scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    # The impersonated credentials, not the raw source, reach the client.
    assert client.credentials is impersonated_sentinel


async def test_impersonate_sa_read_from_settings_when_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.auth.impersonated_credentials

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        google.auth.impersonated_credentials,
        "Credentials",
        lambda **kw: captured.update(kw) or object(),
    )
    monkeypatch.setenv("GSM_IMPERSONATE_SA", "from-settings@proj.iam.gserviceaccount.com")
    get_settings.cache_clear()

    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(_FakeClient(payload=_json_secret(k="v"))),
        # impersonate_sa left as None → resolved from Settings per-call.
    )

    await backend.load_secret_data(
        "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
    )

    assert captured["target_principal"] == "from-settings@proj.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Access / transport errors, fail-closed ADC
# ---------------------------------------------------------------------------


async def test_access_denied_surfaces_actionable_error() -> None:
    import google.api_core.exceptions as gcp_exceptions

    client = _FakeClient(
        payload=b"{}", raises=gcp_exceptions.PermissionDenied("caller lacks access")
    )
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "access denied" in str(exc.value).lower()
    assert "secretAccessor" in str(exc.value)


async def test_not_found_surfaces_actionable_error() -> None:
    import google.api_core.exceptions as gcp_exceptions

    client = _FakeClient(payload=b"{}", raises=gcp_exceptions.NotFound("nope"))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "not found" in str(exc.value).lower()


async def test_transport_error_surfaces_actionable_error() -> None:
    import google.api_core.exceptions as gcp_exceptions

    client = _FakeClient(payload=b"{}", raises=gcp_exceptions.ServiceUnavailable("backend down"))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "could not be" in str(exc.value).lower()


async def test_empty_adc_fails_closed() -> None:
    from google.auth import exceptions as auth_exceptions

    def _raising_loader(**kwargs: Any) -> tuple[Any, str]:
        raise auth_exceptions.DefaultCredentialsError("no ADC")

    backend = GcpSecretManagerBackend(
        adc_loader=_raising_loader,
        client_factory=_factory_for(_FakeClient(payload=b"{}")),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    assert "Application Default Credentials" in str(exc.value)


async def test_none_adc_credentials_fails_closed() -> None:
    def _none_loader(**kwargs: Any) -> tuple[Any, str]:
        return None, "proj"

    backend = GcpSecretManagerBackend(
        adc_loader=_none_loader,
        client_factory=_factory_for(_FakeClient(payload=b"{}")),
        impersonate_sa="",
    )

    with pytest.raises(GcpSecretManagerReadError):
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )


# ---------------------------------------------------------------------------
# No secret in logs
# ---------------------------------------------------------------------------


async def test_no_secret_value_in_log_events() -> None:
    client = _FakeClient(
        payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD),
        resolved_name="projects/my-project/secrets/db-creds/versions/7",
    )
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )

    with capture_logs() as captured:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(), target_name="gcp-lab-01"
        )

    blob = repr(captured)
    assert _CANARY_PASSWORD not in blob
    assert _CANARY_USERNAME not in blob
    # The attribution fields are present (non-secret only).
    event = next(e for e in captured if e["event"] == "gsm_secret_accessed")
    assert event["project"] == "my-project"
    assert event["secret_name"] == "db-creds"
    assert event["target"] == "gcp-lab-01"


# ---------------------------------------------------------------------------
# End-to-end dispatch through the shared loader / registry
# ---------------------------------------------------------------------------


@pytest.fixture
def _gsm_backend_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FakeClient]:
    """Swap the registered ``gsm`` backend for one wired to test doubles.

    The real singleton is registered at import; swapping the registry entry
    is the clean way to drive the shared-loader dispatch path against a
    canned payload. Restored on teardown so sibling tests see the real one.
    """
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    fake_backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
        impersonate_sa="",
    )
    original = cb.CREDENTIAL_BACKEND_REGISTRY["gsm"]
    cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = fake_backend
    try:
        yield client
    finally:
        cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = original


async def test_gsm_backend_registered_under_gsm_kind() -> None:
    backend = cb.resolve_credential_backend("gsm")
    assert isinstance(backend, GcpSecretManagerBackend)


async def test_dispatch_gsm_ref_end_to_end_via_load_vault_secret_data(
    _gsm_backend_with_fakes: _FakeClient,
) -> None:
    target = _Target(secret_ref="gsm:my-project/db-creds")

    data = await load_vault_secret_data(target, _make_operator())

    assert data == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    assert (
        _gsm_backend_with_fakes.accessed_name
        == "projects/my-project/secrets/db-creds/versions/latest"
    )


async def test_dispatch_gsm_ref_end_to_end_via_load_basic_credentials(
    _gsm_backend_with_fakes: _FakeClient,
) -> None:
    target = _Target(secret_ref="gsm:my-project/db-creds")

    creds = await load_basic_credentials(target, _make_operator())

    assert creds == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}


# ---------------------------------------------------------------------------
# Workload Identity Federation (per-operator, #2232)
# ---------------------------------------------------------------------------

_WIF_AUDIENCE = (
    "//iam.googleapis.com/projects/123456/locations/global/"
    "workloadIdentityPools/meho-pool/providers/keycloak"
)
_OPERATOR_JWT = "operator.keycloak.jwt-canary"


def _wif_config(
    *,
    audience: str = _WIF_AUDIENCE,
    pool_id: str = "meho-pool",
    provider_id: str = "keycloak",
    service_account: str = "",
    subject_token_type: str = "urn:ietf:params:oauth:token-type:jwt",
) -> _WifConfig:
    return _WifConfig(
        audience=audience,
        pool_id=pool_id,
        provider_id=provider_id,
        service_account=service_account,
        subject_token_type=subject_token_type,
    )


class _StsResponse:
    """A ``google.auth.transport.Response``-shaped STS reply."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.status = 200
        self.data = json.dumps(body).encode("utf-8")
        self.headers: dict[str, str] = {}


def _set_wif_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Pin the GSM_WIF_* env the settings factory reads, then clear the cache."""
    env = {
        "GSM_WIF_AUDIENCE": _WIF_AUDIENCE,
        "GSM_WIF_POOL_ID": "meho-pool",
        "GSM_WIF_PROVIDER_ID": "keycloak",
    }
    env.update(overrides)
    for key, value in env.items():
        if value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _wif_factory_returning(sentinel: Any) -> Any:
    """A ``wif_credentials_factory`` double recording each call's kwargs."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return sentinel

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


async def test_wif_builds_identity_pool_creds_and_exchanges_operator_jwt() -> None:
    """AC1: a WIF read builds identity_pool.Credentials from the operator JWT
    and does the STS exchange (STS endpoint mocked — no live GCP)."""
    backend = GcpSecretManagerBackend()
    creds = backend._build_wif_credentials(_OPERATOR_JWT, "gcp-lab-01", _wif_config())

    from google.auth import identity_pool

    assert isinstance(creds, identity_pool.Credentials)
    assert creds._audience == _WIF_AUDIENCE

    captured: dict[str, Any] = {}

    def fake_request(url: str, method: str = "GET", body: Any = None, **_: Any) -> _StsResponse:
        captured["url"] = url
        captured["body"] = body
        return _StsResponse(
            {
                "access_token": "federated-token-xyz",
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )

    creds.refresh(fake_request)

    assert creds.token == "federated-token-xyz"
    assert captured["url"] == "https://sts.googleapis.com/v1/token"
    # The operator's JWT is the subject token in the STS exchange body.
    assert _OPERATOR_JWT.encode("utf-8") in captured["body"]


async def test_wif_selected_when_audience_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: an install with WIF configured routes the operator-context read
    through the WIF path, not the SA-direct ADC path."""
    _set_wif_env(monkeypatch)
    sentinel_creds = object()
    wif_factory = _wif_factory_returning(sentinel_creds)
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    adc = _adc_loader_returning(_SourceCreds())
    backend = GcpSecretManagerBackend(
        adc_loader=adc,
        client_factory=_factory_for(client),
        wif_credentials_factory=wif_factory,
    )

    data = await backend.load_secret_data(
        "my-project/db-creds", _make_operator(_OPERATOR_JWT), target_name="gcp-lab-01"
    )

    assert data == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    # The WIF credential (not the ADC source) reached the client.
    assert client.credentials is sentinel_creds
    # The operator JWT was forwarded to the WIF builder; ADC was untouched.
    assert wif_factory.calls[-1]["operator_jwt"] == _OPERATOR_JWT  # type: ignore[attr-defined]
    assert adc.calls == []  # type: ignore[attr-defined]


async def test_wif_credential_minted_fresh_per_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: a fresh federated credential is built per read — no cross-request
    caching of the operator-scoped credential."""
    _set_wif_env(monkeypatch)
    built: list[object] = []

    def wif_factory(**_: Any) -> object:
        cred = object()
        built.append(cred)
        return cred

    client = _FakeClient(payload=_json_secret(k="v"))
    backend = GcpSecretManagerBackend(
        client_factory=_factory_for(client),
        wif_credentials_factory=wif_factory,
    )

    await backend.load_secret_data("p/s", _make_operator(_OPERATOR_JWT), target_name="t")
    first = client.credentials
    await backend.load_secret_data("p/s", _make_operator(_OPERATOR_JWT), target_name="t")
    second = client.credentials

    # Two reads → two distinct credential objects; nothing cached on the backend.
    assert len(built) == 2
    assert first is not second
    assert not hasattr(backend, "_cached_wif_credentials")


async def test_wif_impersonation_url_set_when_sa_configured() -> None:
    """AC3: SA impersonation is applied when a service account is configured."""
    backend = GcpSecretManagerBackend()
    creds = backend._build_wif_credentials(
        _OPERATOR_JWT,
        "gcp-lab-01",
        _wif_config(service_account="reader@my-project.iam.gserviceaccount.com"),
    )

    assert creds._service_account_impersonation_url == (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "reader@my-project.iam.gserviceaccount.com:generateAccessToken"
    )


async def test_wif_impersonation_skipped_when_sa_absent() -> None:
    """AC3: SA impersonation is skipped when no service account is configured."""
    backend = GcpSecretManagerBackend()
    creds = backend._build_wif_credentials(_OPERATOR_JWT, "gcp-lab-01", _wif_config())

    assert creds._service_account_impersonation_url is None


async def test_sa_direct_used_when_wif_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: an install without WIF configured cleanly uses the Phase-1
    SA-direct ADC path (no behaviour change)."""
    _set_wif_env(monkeypatch, GSM_WIF_AUDIENCE="", GSM_WIF_POOL_ID="", GSM_WIF_PROVIDER_ID="")
    source = _SourceCreds()
    adc = _adc_loader_returning(source)
    client = _FakeClient(payload=_json_secret(k="v"))
    wif_factory = _wif_factory_returning(object())
    backend = GcpSecretManagerBackend(
        adc_loader=adc,
        client_factory=_factory_for(client),
        impersonate_sa="",
        wif_credentials_factory=wif_factory,
    )

    await backend.load_secret_data("p/s", _make_operator(_OPERATOR_JWT), target_name="t")

    # SA-direct ADC ran; the WIF builder was never called.
    assert client.credentials is source
    assert adc.calls  # type: ignore[attr-defined]
    assert wif_factory.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("pool_id", "provider_id"),
    [
        ("wrong-pool", "keycloak"),
        ("meho-pool", "wrong-provider"),
    ],
)
async def test_wif_audience_pool_provider_mismatch_fails_closed(
    pool_id: str, provider_id: str
) -> None:
    """AC4: a declared pool / provider that disagrees with the audience fails
    closed with an actionable error."""
    backend = GcpSecretManagerBackend()

    with pytest.raises(GcpSecretManagerReadError) as exc:
        backend._build_wif_credentials(
            _OPERATOR_JWT,
            "gcp-lab-01",
            _wif_config(pool_id=pool_id, provider_id=provider_id),
        )

    msg = str(exc.value)
    assert "gcp-lab-01" in msg
    assert "inconsistent" in msg


async def test_wif_empty_operator_jwt_fails_closed() -> None:
    """The WIF path fails closed on an empty operator JWT (defence in depth
    behind the shared loader's pre-dispatch guard)."""
    backend = GcpSecretManagerBackend()

    with pytest.raises(GcpSecretManagerReadError) as exc:
        backend._build_wif_credentials("", "gcp-lab-01", _wif_config())

    assert "no operator JWT" in str(exc.value)


async def test_wif_log_event_carries_auth_path_and_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WIF read logs auth_path/pool/provider attribution — never a token."""
    _set_wif_env(monkeypatch)
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    backend = GcpSecretManagerBackend(
        client_factory=_factory_for(client),
        wif_credentials_factory=_wif_factory_returning(object()),
    )

    with capture_logs() as captured:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(_OPERATOR_JWT), target_name="gcp-lab-01"
        )

    blob = repr(captured)
    assert _OPERATOR_JWT not in blob
    assert _CANARY_PASSWORD not in blob
    event = next(e for e in captured if e["event"] == "gsm_secret_accessed")
    assert event["auth_path"] == "wif"
    assert event["wif_pool"] == "meho-pool"
    assert event["wif_provider"] == "keycloak"


async def test_wif_end_to_end_via_shared_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: a gsm: ref with WIF configured resolves through the shared loader
    end-to-end under the operator path."""
    _set_wif_env(monkeypatch)
    client = _FakeClient(payload=_json_secret(username=_CANARY_USERNAME, password=_CANARY_PASSWORD))
    sentinel = object()
    fake_backend = GcpSecretManagerBackend(
        client_factory=_factory_for(client),
        wif_credentials_factory=_wif_factory_returning(sentinel),
    )
    original = cb.CREDENTIAL_BACKEND_REGISTRY["gsm"]
    cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = fake_backend
    try:
        target = _Target(secret_ref="gsm:my-project/db-creds")
        creds = await load_basic_credentials(target, _make_operator(_OPERATOR_JWT))
    finally:
        cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = original

    assert creds == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    assert client.credentials is sentinel


# ---------------------------------------------------------------------------
# Background dispatch: SA-direct fallback + backend-neutral errors (#2642)
# ---------------------------------------------------------------------------


async def test_sa_direct_fallback_taken_when_wif_configured_and_no_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: WIF configured + ``raw_jwt=""`` + ambient ADC -> SA-direct read.

    The system-initiated case (a background sensor evaluation, a scheduled
    topology refresh). Health and readiness probes are deliberately not the
    example: they never resolve a per-target ``secret_ref`` and so never
    reach this loader. There is no operator JWT to federate, but the pod's
    own ADC can serve the read, so the backend falls back instead of
    failing closed. The
    WIF factory must never be called -- taking the WIF path with an empty
    subject token is what used to make every credentialed Sensor ``unknown``.
    """
    _set_wif_env(monkeypatch)
    source = _SourceCreds()
    adc = _adc_loader_returning(source)
    client = _FakeClient(payload=_json_secret(username="u", password="p"))
    wif_factory = _wif_factory_returning(object())
    backend = GcpSecretManagerBackend(
        adc_loader=adc,
        client_factory=_factory_for(client),
        wif_credentials_factory=wif_factory,
    )

    data = await backend.load_secret_data(
        "my-project/db-creds", _make_operator(""), target_name="gcp-lab-01"
    )

    assert data == {"username": "u", "password": "p"}
    assert client.credentials is source
    assert wif_factory.calls == []


async def test_sa_direct_fallback_logs_its_own_auth_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is auditable: a distinct ``auth_path`` label, no pool/provider.

    A read GCP attributed to MEHO's own identity must be distinguishable in
    the log from one attributed to the calling operator.
    """
    _set_wif_env(monkeypatch)
    client = _FakeClient(payload=_json_secret(username="u", password=_CANARY_PASSWORD))
    backend = GcpSecretManagerBackend(
        adc_loader=_adc_loader_returning(_SourceCreds()),
        client_factory=_factory_for(client),
    )

    with capture_logs() as captured:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(""), target_name="gcp-lab-01"
        )

    event = next(e for e in captured if e["event"] == "gsm_secret_accessed")
    assert event["auth_path"] == "sa_direct_fallback"
    assert event["wif_pool"] is None
    assert event["wif_provider"] is None
    assert _CANARY_PASSWORD not in repr(captured)


async def test_system_call_without_ambient_adc_fails_closed_naming_both_remedies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No operator JWT and no ambient ADC -> fail closed, but actionably.

    The on-prem per-operator-WIF deploy. Neither identity is available, so
    the read must still fail -- with a message that names the two ways out
    rather than only the GKE one.
    """
    _set_wif_env(monkeypatch)

    def empty_adc(**_: Any) -> tuple[Any, str]:
        return None, "adc-project"

    backend = GcpSecretManagerBackend(adc_loader=empty_adc)

    with pytest.raises(GcpSecretManagerReadError) as exc:
        await backend.load_secret_data(
            "my-project/db-creds", _make_operator(""), target_name="gcp-lab-01"
        )

    msg = str(exc.value)
    assert "CHECK_RUNNER_CLIENT_ID" in msg
    assert "ambient GCP identity" in msg


async def test_operator_jwt_still_takes_the_wif_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback does not weaken the operator path: a JWT still federates."""
    _set_wif_env(monkeypatch)
    sentinel = object()
    wif_factory = _wif_factory_returning(sentinel)
    client = _FakeClient(payload=_json_secret(username="u", password="p"))
    backend = GcpSecretManagerBackend(
        client_factory=_factory_for(client),
        wif_credentials_factory=wif_factory,
    )

    await backend.load_secret_data(
        "my-project/db-creds", _make_operator(_OPERATOR_JWT), target_name="gcp-lab-01"
    )

    assert client.credentials is sentinel
    assert wif_factory.calls[0]["operator_jwt"] == _OPERATOR_JWT


async def test_system_call_on_gsm_deploy_never_raises_a_vault_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: a GSM credential-read failure carries no ``Vault`` in its class name.

    The shared loader used to fail-close every system-initiated call with
    ``VaultCredentialsReadError`` *before* resolving a backend, so a deploy
    running no Vault at all reported a Vault error. Drive the failure through
    the public loader (the path a dispatch takes) on a ``gsm:`` ref.
    """
    _set_wif_env(monkeypatch)

    def empty_adc(**_: Any) -> tuple[Any, str]:
        return None, "adc-project"

    fake_backend = GcpSecretManagerBackend(adc_loader=empty_adc)
    original = cb.CREDENTIAL_BACKEND_REGISTRY["gsm"]
    cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = fake_backend
    try:
        with pytest.raises(cb.CredentialsReadError) as exc:
            await load_basic_credentials(
                _Target(secret_ref="gsm:my-project/db-creds"), _make_operator("")
            )
    finally:
        cb.CREDENTIAL_BACKEND_REGISTRY["gsm"] = original

    assert type(exc.value).__name__ == "GcpSecretManagerReadError"
    assert "Vault" not in type(exc.value).__name__
    assert "vault" not in str(exc.value).lower()


async def test_gsm_read_error_is_a_backend_neutral_credentials_read_error() -> None:
    """The two backends' read errors share one catchable base (#2642).

    Connector probe paths catch "the credential could not be read" to report
    ``auth_failed`` instead of crashing; before the shared base, a GSM error
    sailed straight through an ``except VaultCredentialsReadError``.
    """
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

    assert issubclass(GcpSecretManagerReadError, cb.CredentialsReadError)
    assert issubclass(VaultCredentialsReadError, cb.CredentialsReadError)
    assert not issubclass(GcpSecretManagerReadError, VaultCredentialsReadError)

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
    import json

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

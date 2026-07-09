# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backend-agnostic federation-proof tests for ``GET /api/v1/health`` (#2231).

The federation proof is dispatched on ``config.credentialBackend`` /
``CREDENTIAL_BACKEND``: a Vault install takes the unchanged
``vault.kv.read`` path (covered by ``test_api_v1_health.py`` /
``test_api_v1_health_split.py``); any other backend reads its designated
probe secret through the credential-backend seam. This suite pins the
non-Vault path — the ``gsm`` backend — at the
:func:`~meho_backplane.api.v1.health._probe_federation` boundary, so it
needs no live GCP, no DB, and no JWT round-trip: the seam's
``resolve_credential_backend`` is patched to a fake backend and the
returned :class:`~meho_backplane.api.v1.health.VaultStatus` is asserted
against each failure axis (healthy read, read error, unconfigured probe,
unknown backend kind). The never-a-5xx contract holds — every axis is a
structured status, never a raised exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
import structlog

from meho_backplane.api.v1 import health as health_module
from meho_backplane.api.v1.health import VaultStatus, _probe_federation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.credential_backend import (
    UnknownCredentialBackendError,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis-required env vars; individual tests set backend knobs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("CREDENTIAL_BACKEND", raising=False)
    monkeypatch.delenv("GSM_PROJECT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    """A minimal OPERATOR-rank principal for the probe under test."""
    return Operator(
        sub="op-alice",
        name="Alice",
        email="alice@example.com",
        raw_jwt="header.payload.sig",
        tenant_id=UUID(int=1),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingBackend:
    """Fake credential backend recording the ref it was asked to read."""

    def __init__(self, *, result: dict[str, object] | None = None, error: Exception | None = None):
        self._result = result if result is not None else {"ok": "true"}
        self._error = error
        self.calls: list[tuple[str, str, str]] = []

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str = "",
    ) -> dict[str, object]:
        self.calls.append((secret_ref, target_name, mount))
        if self._error is not None:
            raise self._error
        return self._result


async def test_gsm_backend_federation_reports_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GSM install reads ``gsm:<project>/meho-test-federation`` and reports healthy."""
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    monkeypatch.setenv("GSM_PROJECT", "my-gcp-project")
    get_settings.cache_clear()

    backend = _RecordingBackend(result={"ok": "true"})
    monkeypatch.setattr(health_module, "resolve_credential_backend", lambda _kind: backend)

    status = await _probe_federation(_operator(), structlog.get_logger())

    assert status == VaultStatus(reachable=True, read_ok=True, detail="ok")
    # The seam is handed the scheme-stripped store ref, the probe target
    # name, and an empty mount (a Vault-KV concept the GSM backend ignores).
    assert backend.calls == [("my-gcp-project/meho-test-federation", "health-federation-proof", "")]


async def test_gsm_backend_read_error_never_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend read failure surfaces as read_ok=False + a class-name detail, never a raise."""
    from meho_backplane.connectors._shared.gsm_creds import GcpSecretManagerReadError

    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    monkeypatch.setenv("GSM_PROJECT", "my-gcp-project")
    get_settings.cache_clear()

    backend = _RecordingBackend(error=GcpSecretManagerReadError("secret projects/... not found"))
    monkeypatch.setattr(health_module, "resolve_credential_backend", lambda _kind: backend)

    status = await _probe_federation(_operator(), structlog.get_logger())

    assert status == VaultStatus(
        reachable=True, read_ok=False, detail="read_failed: GcpSecretManagerReadError"
    )
    # Detail carries only the exception class name — never the message, so an
    # operator-controllable substring can't leak into a 200 response body.
    assert "not found" not in (status.detail or "")


async def test_gsm_backend_unconfigured_project_reports_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CREDENTIAL_BACKEND=gsm`` without ``GSM_PROJECT`` fails config-side, not a store read."""
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    monkeypatch.delenv("GSM_PROJECT", raising=False)
    get_settings.cache_clear()

    # resolve must never be reached — the probe ref is unbuildable first.
    def _fail(_kind: str) -> object:  # pragma: no cover - asserts non-invocation
        raise AssertionError("resolve_credential_backend must not run when project is unset")

    monkeypatch.setattr(health_module, "resolve_credential_backend", _fail)

    status = await _probe_federation(_operator(), structlog.get_logger())

    assert status == VaultStatus(reachable=False, read_ok=False, detail="config_error: gsm")


async def test_unknown_backend_kind_reports_unknown_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered backend kind surfaces as reachable=False + ``unknown_backend``."""
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    monkeypatch.setenv("GSM_PROJECT", "my-gcp-project")
    get_settings.cache_clear()

    def _unknown(_kind: str) -> object:
        raise UnknownCredentialBackendError("no credential backend registered for kind 'gsm'")

    monkeypatch.setattr(health_module, "resolve_credential_backend", _unknown)

    status = await _probe_federation(_operator(), structlog.get_logger())

    assert status == VaultStatus(reachable=False, read_ok=False, detail="unknown_backend: gsm")


async def test_vault_backend_takes_the_unchanged_dispatch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (``vault``) backend routes to the unchanged ``_probe_vault_federation``."""
    monkeypatch.setenv("CREDENTIAL_BACKEND", "vault")
    get_settings.cache_clear()

    calls: list[str] = []

    async def _fake_vault_probe(operator: Operator, log: object) -> VaultStatus:
        calls.append(operator.sub)
        return VaultStatus(reachable=True, read_ok=True, detail="version=7")

    monkeypatch.setattr(health_module, "_probe_vault_federation", _fake_vault_probe)
    # A GSM read must never be attempted on the Vault path.
    monkeypatch.setattr(
        health_module,
        "resolve_credential_backend",
        lambda _kind: (_ for _ in ()).throw(AssertionError("seam must not run on the vault path")),
    )

    status = await _probe_federation(_operator(), structlog.get_logger())

    assert status == VaultStatus(reachable=True, read_ok=True, detail="version=7")
    assert calls == ["op-alice"]

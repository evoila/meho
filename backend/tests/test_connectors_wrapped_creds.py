# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for per-work-item wrapped-credential brokering (#3191).

Mechanism 3 of the satellite write path: the centre brokers a short-lived,
single-target-scoped, response-wrapped credential; the runner unwraps it
just-in-time under the single-use token (not the acting operator's empty
JWT); a second unwrap fails closed; a remote-write item without a wrapped
credential is refused at the edge. No live Vault — hvac and the seam read
are faked.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import hvac
import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors._shared import wrapped_creds
from meho_backplane.connectors._shared.vault_creds import load_vault_secret_data
from meho_backplane.connectors._shared.wrapped_creds import (
    WRAPPED_CREDENTIAL_SCHEME,
    WrappedCredential,
    WrappedCredentialBackend,
    WrappedCredentialError,
    broker_wrapped_credential,
    is_wrapped_credential_ref,
    screen_remote_write_credential,
)

_ADDR_ENV = "MEHO_RUNNER_VAULT_ADDR"


def _operator(*, raw_jwt: str = "operator-jwt") -> Operator:
    return Operator(
        sub="op-1",
        raw_jwt=raw_jwt,
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=PrincipalKind.SERVICE,
    )


@dataclass
class _Target:
    """Minimal ``BasicCredentialsTargetLike`` for the broker/screen tests."""

    name: str = "vc-a"
    host: str = "vc-a.example.internal"
    secret_ref: str | None = "targets/vc-a"


# ---------------------------------------------------------------------------
# Fake Vault client
# ---------------------------------------------------------------------------


class _FakeSys:
    def __init__(self, client: _FakeVaultClient) -> None:
        self._client = client

    def wrap(self, payload: dict[str, object] | None = None, ttl: int = 60) -> dict[str, object]:
        self._client.wrap_payload = payload
        self._client.wrap_ttl = ttl
        return {"wrap_info": {"token": self._client.mint_token, "ttl": ttl}}

    def unwrap(self, token: str | None = None) -> dict[str, object]:
        self._client.unwrap_calls += 1
        self._client.unwrap_body_token = token
        if self._client.unwrap_error is not None:
            raise self._client.unwrap_error
        # Vault consumes a wrapping token on first unwrap — model single-use.
        self._client.unwrap_error = hvac.exceptions.InvalidRequest(
            "wrapping token is not valid or does not exist"
        )
        return self._client.unwrap_result


class _FakeVaultClient:
    def __init__(
        self,
        *,
        url: str | None = None,
        namespace: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        mint_token: str = "hvs.WRAPTOKEN",
        unwrap_result: dict[str, object] | None = None,
        unwrap_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.namespace = namespace
        self.timeout = timeout
        self.token = token
        self.mint_token = mint_token
        self.unwrap_result = unwrap_result or {"data": {"username": "svc", "password": "pw-A"}}
        self.unwrap_error = unwrap_error
        self.unwrap_calls = 0
        self.unwrap_body_token: str | None = None
        self.wrap_payload: dict[str, object] | None = None
        self.wrap_ttl: int | None = None
        self.sys = _FakeSys(self)


# ---------------------------------------------------------------------------
# is_wrapped_credential_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("wrapped:hvs.TOKEN", True),
        ("wrapped:s.abc123", True),
        ("wrapped:", False),  # empty token
        ("targets/vc-a", False),  # schemeless standing ref
        ("vault:targets/vc-a", False),  # explicit standing ref
        ("gsm:proj/secret#pw", False),
        (None, False),
        ("", False),
    ],
)
def test_is_wrapped_credential_ref(ref: str | None, expected: bool) -> None:
    assert is_wrapped_credential_ref(ref) is expected


# ---------------------------------------------------------------------------
# Broker (centre side)
# ---------------------------------------------------------------------------


def _patch_broker_vault(
    monkeypatch: pytest.MonkeyPatch, *, secret: dict[str, object], client: _FakeVaultClient
) -> None:
    async def _fake_load(
        target: object, operator: object, *, mount: str = "secret"
    ) -> dict[str, object]:
        return dict(secret)

    @contextlib.asynccontextmanager
    async def _fake_client(operator: object) -> AsyncIterator[_FakeVaultClient]:
        yield client

    monkeypatch.setattr(wrapped_creds, "load_vault_secret_data", _fake_load)
    monkeypatch.setattr(wrapped_creds, "vault_client_for_operator", _fake_client)


async def test_broker_wraps_exactly_one_targets_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Single-target-scoped: the wrapped payload is exactly the one target's
    # secret fields, and the returned reference carries only the token — never
    # the credential value.
    secret = {"username": "svc", "password": "pw-A"}
    client = _FakeVaultClient(mint_token="hvs.TOKEN-A")
    _patch_broker_vault(monkeypatch, secret=secret, client=client)
    now = datetime.now(UTC)

    wc = await broker_wrapped_credential(
        _Target(), _operator(), capability_expires_at=now + timedelta(minutes=5), now=now
    )

    assert isinstance(wc, WrappedCredential)
    assert wc.unwrap_ref == "wrapped:hvs.TOKEN-A"
    assert client.wrap_payload == secret  # exactly this target's fields
    assert "pw-A" not in wc.unwrap_ref  # value never rides the reference


async def test_broker_bounds_ttl_to_capability_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    # TTL ≤ capability expiry: a generous requested TTL is capped to the
    # headroom, and expires_at never exceeds the capability's.
    client = _FakeVaultClient()
    _patch_broker_vault(monkeypatch, secret={"username": "svc"}, client=client)
    now = datetime.now(UTC)
    expiry = now + timedelta(seconds=30)

    wc = await broker_wrapped_credential(
        _Target(), _operator(), capability_expires_at=expiry, requested_ttl_seconds=3600, now=now
    )

    assert wc.wrap_ttl_seconds == 30
    assert client.wrap_ttl == 30
    assert wc.expires_at <= expiry


async def test_broker_honours_shorter_requested_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeVaultClient()
    _patch_broker_vault(monkeypatch, secret={"username": "svc"}, client=client)
    now = datetime.now(UTC)

    wc = await broker_wrapped_credential(
        _Target(),
        _operator(),
        capability_expires_at=now + timedelta(hours=1),
        requested_ttl_seconds=45,
        now=now,
    )

    assert wc.wrap_ttl_seconds == 45


async def test_broker_refuses_already_expired_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_broker_vault(monkeypatch, secret={"username": "svc"}, client=_FakeVaultClient())
    now = datetime.now(UTC)

    with pytest.raises(WrappedCredentialError, match="not in the future"):
        await broker_wrapped_credential(
            _Target(), _operator(), capability_expires_at=now - timedelta(seconds=1), now=now
        )


async def test_broker_refuses_non_positive_requested_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_broker_vault(monkeypatch, secret={"username": "svc"}, client=_FakeVaultClient())
    now = datetime.now(UTC)

    with pytest.raises(WrappedCredentialError, match="must be positive"):
        await broker_wrapped_credential(
            _Target(),
            _operator(),
            capability_expires_at=now + timedelta(minutes=5),
            requested_ttl_seconds=0,
            now=now,
        )


async def test_broker_refuses_when_vault_returns_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoTokenSys(_FakeSys):
        def wrap(
            self, payload: dict[str, object] | None = None, ttl: int = 60
        ) -> dict[str, object]:
            return {"wrap_info": {}}  # missing token

    client = _FakeVaultClient()
    client.sys = _NoTokenSys(client)
    _patch_broker_vault(monkeypatch, secret={"username": "svc"}, client=client)
    now = datetime.now(UTC)

    with pytest.raises(WrappedCredentialError, match="no wrapping token"):
        await broker_wrapped_credential(
            _Target(), _operator(), capability_expires_at=now + timedelta(minutes=5), now=now
        )


# ---------------------------------------------------------------------------
# Runner unwrap backend (edge side)
# ---------------------------------------------------------------------------


def _patch_hvac_client(monkeypatch: pytest.MonkeyPatch, client: _FakeVaultClient) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs: object) -> _FakeVaultClient:
        captured.update(kwargs)
        client.url = kwargs.get("url")  # type: ignore[assignment]
        client.namespace = kwargs.get("namespace")  # type: ignore[assignment]
        client.timeout = kwargs.get("timeout")  # type: ignore[assignment]
        client.token = kwargs.get("token")  # type: ignore[assignment]
        return client

    monkeypatch.setattr(hvac, "Client", _factory)


async def test_backend_unwraps_under_token_not_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC#4: the runner presents the single-use wrapping token, NOT the acting
    # operator — the reconstructed runner operator has an empty raw_jwt, and
    # the unwrap still succeeds, closing the empty-JWT gap (design §1.4).
    monkeypatch.setenv(_ADDR_ENV, "https://vault.central:8200/")
    client = _FakeVaultClient(unwrap_result={"data": {"username": "svc", "password": "pw-A"}})
    _patch_hvac_client(monkeypatch, client)

    creds = await WrappedCredentialBackend().load_secret_data(
        "hvs.WRAPTOKEN", _operator(raw_jwt=""), target_name="vc-a"
    )

    assert creds == {"username": "svc", "password": "pw-A"}
    # The client authenticated AS the wrapping token (self-authorising unwrap).
    assert client.token == "hvs.WRAPTOKEN"
    assert client.url == "https://vault.central:8200"  # trailing slash trimmed
    assert client.unwrap_calls == 1  # unwrapped exactly once


async def test_backend_second_unwrap_of_consumed_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Single-use: Vault consumes the token on first unwrap, so a replay /
    # spool redelivery unwraps a second time and Vault refuses.
    monkeypatch.setenv(_ADDR_ENV, "https://vault.central:8200")
    client = _FakeVaultClient()
    _patch_hvac_client(monkeypatch, client)
    backend = WrappedCredentialBackend()

    first = await backend.load_secret_data("hvs.WRAPTOKEN", _operator(), target_name="vc-a")
    assert first["username"] == "svc"

    with pytest.raises(WrappedCredentialError, match="expired, already consumed, or invalid"):
        await backend.load_secret_data("hvs.WRAPTOKEN", _operator(), target_name="vc-a")


async def test_backend_maps_unreachable_vault_to_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ADDR_ENV, "https://vault.central:8200")
    client = _FakeVaultClient(unwrap_error=requests.exceptions.ConnectionError("boom"))
    _patch_hvac_client(monkeypatch, client)

    with pytest.raises(WrappedCredentialError, match="Vault was unreachable"):
        await WrappedCredentialBackend().load_secret_data(
            "hvs.WRAPTOKEN", _operator(), target_name="vc-a"
        )


async def test_backend_rejects_malformed_unwrap_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ADDR_ENV, "https://vault.central:8200")
    client = _FakeVaultClient(unwrap_result={"not_data": {}})
    _patch_hvac_client(monkeypatch, client)

    with pytest.raises(WrappedCredentialError, match="malformed payload"):
        await WrappedCredentialBackend().load_secret_data(
            "hvs.WRAPTOKEN", _operator(), target_name="vc-a"
        )


async def test_backend_fails_closed_without_vault_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ADDR_ENV, raising=False)

    with pytest.raises(WrappedCredentialError, match=_ADDR_ENV):
        await WrappedCredentialBackend().load_secret_data(
            "hvs.WRAPTOKEN", _operator(), target_name="vc-a"
        )


# ---------------------------------------------------------------------------
# Edge fail-closed screen (standing broad creds rejected on the write tier)
# ---------------------------------------------------------------------------


def test_screen_permits_a_wrapped_credential() -> None:
    assert screen_remote_write_credential(_Target(secret_ref="wrapped:hvs.TOKEN")) is None


@pytest.mark.parametrize("ref", ["targets/vc-a", "vault:targets/vc-a", "gsm:proj/s#pw", None])
def test_screen_refuses_standing_or_missing_credential(ref: str | None) -> None:
    reason = screen_remote_write_credential(_Target(secret_ref=ref))
    assert reason is not None
    assert "no standing runner credential" in reason


def test_screen_refuses_when_target_descriptor_is_none() -> None:
    reason = screen_remote_write_credential(None)
    assert reason is not None
    assert WRAPPED_CREDENTIAL_SCHEME in reason


async def test_executor_refuses_remote_write_without_wrapped_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end edge enforcement: with the composed gate permitting (its real
    # wiring is #3189), a remote-write item whose target carries a standing
    # secret_ref is still refused at the edge by the mechanism-3 credential
    # screen — no standing runner credential ever rides the write tier.
    from meho_backplane.runner import executor
    from meho_backplane.runner.satellite_tier import RemoteWriteGateDecision
    from meho_backplane.runner.wire import (
        ResolvedTargetDescriptor,
        RunnerPrincipal,
        RunnerWorkItem,
    )

    monkeypatch.setattr(
        executor,
        "evaluate_remote_write_gate",
        lambda **_kw: RemoteWriteGateDecision(permitted=True, reason="permitted-in-test"),
    )

    descriptor = ResolvedTargetDescriptor(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="vc-a",
        product="vmware",
        host="vc-a.example.internal",
        secret_ref="targets/vc-a",  # standing/broad — not a wrapped token
    )
    item = RunnerWorkItem(
        check_ref="chk-rw",
        op_id="vmware.vm.tag_set",
        product="vmware",
        handler_ref="meho_backplane.connectors.vmware.ops.tag_set",
        safety_level="caution",
        principal=RunnerPrincipal(
            sub="runner-svc", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
        target_descriptor=descriptor,
    )

    result = await executor.execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "no standing runner credential" in (result.error or "")


# ---------------------------------------------------------------------------
# Seam lazy default — the runner resolves a wrapped: ref with no chassis Settings
# ---------------------------------------------------------------------------


async def test_wrapped_ref_resolves_without_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The DB-free runner cannot build the chassis Settings. An explicit
    # ``wrapped:`` ref must resolve through the seam without a get_settings()
    # call — prove it by making get_settings() raise and asserting resolution
    # still dispatches to the wrapped backend.
    calls = {"get_settings": 0}

    def _boom() -> object:
        calls["get_settings"] += 1
        raise RuntimeError("chassis Settings unavailable on the runner")

    async def _fake_load_secret_data(
        self: object, secret_ref: str, operator: object, *, target_name: str, mount: str
    ) -> dict[str, object]:
        return {"resolved_token": secret_ref}

    monkeypatch.setattr("meho_backplane.connectors._shared.vault_creds.get_settings", _boom)
    monkeypatch.setattr(WrappedCredentialBackend, "load_secret_data", _fake_load_secret_data)

    data = await load_vault_secret_data(_Target(secret_ref="wrapped:hvs.TOKEN"), _operator())

    assert data == {"resolved_token": "hvs.TOKEN"}  # scheme stripped, no settings read
    assert calls["get_settings"] == 0


async def test_schemeless_ref_still_consults_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contrast: a schemeless ref keeps today's behaviour and routes through
    # the deployment default backend (a get_settings() read).
    calls = {"get_settings": 0}

    def _raise_after_count() -> object:
        calls["get_settings"] += 1
        raise RuntimeError("routed through settings")

    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.get_settings", _raise_after_count
    )

    with pytest.raises(RuntimeError, match="routed through settings"):
        await load_vault_secret_data(_Target(secret_ref="targets/vc-a"), _operator())

    assert calls["get_settings"] == 1

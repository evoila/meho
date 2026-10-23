# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the per-call guest credential override helpers (#3364).

Covers the allowed-prefix bound a ``guest_secret_ref`` must satisfy (default
parent namespace of the target's ``secret_ref``, the ``extras`` override,
backend-kind matching, traversal segments) and the override read itself: the
prefix check runs before any store read, a store denial propagates as the
param-ref error with no fallback, and a secret missing a guest field raises
without echoing a value.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import hvac.exceptions
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import (
    SecretRefReadError,
    VaultCredentialsReadError,
)
from meho_backplane.connectors.vmware_rest.composites import _guest_secret_ref as mod
from meho_backplane.settings import get_settings

_FIELDS = ("guest_username", "guest_password")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings a schemeless ref's backend resolution reads (default ``vault``)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("CREDENTIAL_BACKEND", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="agent-guest-ops",
        name="Guest Ops Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id="11111111-1111-1111-1111-111111111111",
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.example.test"
    secret_ref: str | None = "targets/vc"
    extras: dict[str, Any] = field(default_factory=dict)


class _StoreRecorder:
    """Stub for ``load_param_secret_data`` -- records the ref it was handed."""

    def __init__(self, data: dict[str, Any] | None = None, *, raises: Exception | None = None):
        self._data = data or {}
        self._raises = raises
        self.calls: list[tuple[str | None, str]] = []

    async def __call__(self, target: Any, operator: Operator, *, param: str) -> dict[str, Any]:
        self.calls.append((target.secret_ref, param))
        if self._raises is not None:
            raise self._raises
        return dict(self._data)


# ---------------------------------------------------------------------------
# allowed prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (_Target(secret_ref="targets/vc"), ("vault", "targets/")),
        (_Target(secret_ref="vault:tenants/acme/vc-01"), ("vault", "tenants/acme/")),
        (_Target(secret_ref="gsm:proj/vc-secret#password"), ("gsm", "proj/")),
        (_Target(secret_ref="gsm:proj/vc-secret/versions/4"), ("gsm", "proj/")),
        (_Target(secret_ref="gsm:proj/vc-secret/versions/4#password"), ("gsm", "proj/")),
        (_Target(secret_ref="vault:tenants/acme/vc-01#password"), ("vault", "tenants/acme/")),
        (
            _Target(secret_ref="targets/vc", extras={"guest_secret_prefix": "guests/vc"}),
            ("vault", "guests/vc/"),
        ),
        (
            _Target(secret_ref="targets/vc", extras={"guest_secret_prefix": "gsm:proj/guests/"}),
            ("gsm", "proj/guests/"),
        ),
        (
            _Target(secret_ref="targets/vc", extras={"guest_secret_prefix": "   "}),
            ("vault", "targets/"),
        ),
        (_Target(secret_ref=None), None),
        (_Target(secret_ref="vc-only"), None),
        (_Target(secret_ref="  "), None),
    ],
)
def test_allowed_prefix_resolution(target: _Target, expected: tuple[str, str] | None) -> None:
    assert mod.allowed_guest_secret_prefix(target) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "targets/app-vm-01",
        "vault:targets/app-vm-01",
        "targets/team-a/app-vm-01",
    ],
)
def test_check_accepts_refs_under_the_default_prefix(ref: str) -> None:
    mod.check_guest_secret_ref(ref, _Target())


@pytest.mark.parametrize(
    "ref",
    [
        "other/app-vm-01",
        "targetsx/app-vm-01",
        "gsm:targets/app-vm-01",
        "targets/../other/app-vm-01",
        "targets/./app-vm-01",
        "targets//app-vm-01",
        "targets/",
        "targets",
    ],
)
def test_check_rejects_refs_outside_the_default_prefix(ref: str) -> None:
    with pytest.raises(mod.GuestSecretRefScopeError, match="outside the allowed"):
        mod.check_guest_secret_ref(ref, _Target())


@pytest.mark.parametrize(
    "ref",
    [
        "gsm:proj/app-vm-01",
        "gsm:proj/app-vm-01#password",
        "gsm:proj/app-vm-01/versions/2",
        "gsm:proj/app-vm-01/versions/2#password",
    ],
)
def test_version_pinned_gsm_target_allows_the_whole_project(ref: str) -> None:
    """A ``/versions/<n>`` pin on the target secret must not narrow the namespace."""
    pinned = _Target(secret_ref="gsm:proj/vc-secret/versions/4")
    mod.check_guest_secret_ref(ref, pinned)


@pytest.mark.parametrize("ref", ["gsm:other/app-vm-01", "proj/app-vm-01"])
def test_version_pinned_gsm_target_still_rejects_other_namespaces(ref: str) -> None:
    pinned = _Target(secret_ref="gsm:proj/vc-secret/versions/4")
    with pytest.raises(mod.GuestSecretRefScopeError, match="outside the allowed"):
        mod.check_guest_secret_ref(ref, pinned)


def test_check_rejects_when_no_prefix_is_resolvable() -> None:
    with pytest.raises(mod.GuestSecretRefScopeError, match="no allowed guest-secret prefix"):
        mod.check_guest_secret_ref("targets/app-vm-01", _Target(secret_ref="vc-only"))


def test_check_honours_the_extras_override() -> None:
    narrowed = _Target(extras={"guest_secret_prefix": "targets/team-a"})
    mod.check_guest_secret_ref("targets/team-a/app-vm-01", narrowed)
    with pytest.raises(mod.GuestSecretRefScopeError):
        mod.check_guest_secret_ref("targets/app-vm-01", narrowed)


def test_scope_error_message_cuts_the_ref_to_its_prefix() -> None:
    with pytest.raises(mod.GuestSecretRefScopeError) as excinfo:
        mod.check_guest_secret_ref("other/team-b/app-vm-01", _Target())
    assert "team-b" not in str(excinfo.value)
    assert "other/…" in str(excinfo.value)


# ---------------------------------------------------------------------------
# override read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_reads_the_override_ref_under_the_param_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StoreRecorder({"guest_username": " vm-guest ", "guest_password": "pw-1\n"})
    monkeypatch.setattr(mod, "load_param_secret_data", store)
    creds = await mod.load_guest_override_credentials(
        " targets/app-vm-01 ", _Target(), _operator(), fields=_FIELDS
    )
    assert creds == {"guest_username": "vm-guest", "guest_password": "pw-1"}
    assert store.calls == [("targets/app-vm-01", "guest_secret_ref")]


@pytest.mark.asyncio
async def test_load_rejects_out_of_prefix_before_any_store_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StoreRecorder({"guest_username": "x", "guest_password": "y"})
    monkeypatch.setattr(mod, "load_param_secret_data", store)
    with pytest.raises(mod.GuestSecretRefScopeError):
        await mod.load_guest_override_credentials(
            "other/app-vm-01", _Target(), _operator(), fields=_FIELDS
        )
    assert store.calls == []


@pytest.mark.asyncio
async def test_load_propagates_a_store_denial_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = SecretRefReadError(
        param="guest_secret_ref",
        secret_ref="targets/app-vm-01",
        cause=hvac.exceptions.Forbidden("permission denied"),
    )
    store = _StoreRecorder(raises=denied)
    monkeypatch.setattr(mod, "load_param_secret_data", store)
    with pytest.raises(SecretRefReadError) as excinfo:
        await mod.load_guest_override_credentials(
            "targets/app-vm-01", _Target(), _operator(), fields=_FIELDS
        )
    assert excinfo.value is denied
    assert excinfo.value.cause_class == "Forbidden"
    assert "app-vm-01" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_missing_field_raises_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StoreRecorder({"guest_username": "vm-guest"})
    monkeypatch.setattr(mod, "load_param_secret_data", store)
    with pytest.raises(VaultCredentialsReadError) as excinfo:
        await mod.load_guest_override_credentials(
            "targets/app-vm-01", _Target(), _operator(), fields=_FIELDS
        )
    message = str(excinfo.value)
    assert "missing the 'guest_password' field" in message
    assert "vm-guest" not in message
    assert "app-vm-01" not in message

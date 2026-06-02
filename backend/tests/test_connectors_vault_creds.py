# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared Vault KV-v2 basic-credentials helper.

These cover the **secret-free** contract of
:func:`meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
against the in-process Vault fake (``install_fake_client`` patches
``meho_backplane.auth.vault._build_client`` so ``vault_client_for_operator``
runs its real JWT/OIDC login path against the fake hvac client). The
*live* KV-v2 round-trip against a real Vault dev-mode container is in
``tests/integration/test_connectors_vault_creds_dev_e2e.py`` — the
rubric State-2 bar; that suite is deselected by the unit CI lane
(``pytest --ignore=tests/integration``) and run by the integration lane.

What these unit tests pin:

* the happy path returns the requested fields as a flat ``{field: value}``
  dict, with the operator's JWT forwarded to Vault's login;
* empty ``operator.raw_jwt`` fails closed (the system-call carve-out)
  *before* Vault is touched;
* an unset ``target.secret_ref`` raises a clear error;
* a KV-v2 secret missing a requested field raises
  :class:`VaultCredentialsReadError` naming the target + field — never a
  bare ``KeyError``;
* a malformed hvac payload raises the helper error, not a ``KeyError``;
* login-phase failures (``VaultRoleDeniedError``) propagate verbatim so
  callers can distinguish login phase from read phase;
* no credential value appears in any structlog event (asserted via
  ``capture_logs``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultRoleDeniedError
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_BASIC_CREDENTIAL_FIELDS,
    DEFAULT_KV_MOUNT,
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# A throwaway password value used only to assert it never reaches a log
# event. Generated nowhere near a real secret; the string itself is the
# canary the no-secret-in-logs test searches for.
_CANARY_PASSWORD = "p4ssw0rd-canary-must-not-leak"
_CANARY_USERNAME = "svc-canary"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars Settings reads at construction time.

    ``vault_client_for_operator`` calls ``get_settings()`` which eagerly
    reads ``KEYCLOAK_*`` / ``VAULT_*`` from the environment. Same pinning
    shape as ``test_connectors_vault_auth.py``'s autouse fixture.
    """
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
class _Target:
    """Minimal target satisfying ``BasicCredentialsTargetLike``."""

    name: str = "vc-lab-01"
    host: str = "vc-lab-01.example.test"
    secret_ref: str | None = "targets/vc-lab-01"


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """Request-scoped operator carrying the bearer token forwarded to Vault."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_returns_requested_fields_and_forwards_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default fields come back as a flat dict; the operator JWT is forwarded."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    creds = await load_basic_credentials(_Target(), _make_operator("op.jwt"))

    assert creds == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    # The JWT/OIDC login was performed with the operator's raw_jwt.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.jwt"
    # The read addressed the secret_ref path under the default mount.
    assert fake.secrets.kv.v2.read_calls[-1] == {
        "path": "targets/vc-lab-01",
        "mount_point": DEFAULT_KV_MOUNT,
    }
    # The per-request Vault token is revoked on context exit.
    assert fake.auth.token.revoke_calls == 1


async def test_default_fields_constant_is_username_password() -> None:
    """The shared default-fields constant is the documented basic pair."""
    assert DEFAULT_BASIC_CREDENTIAL_FIELDS == ("username", "password")


async def test_custom_fields_and_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller can request a different field set under a non-default mount."""
    fake = install_fake_client(
        monkeypatch,
        secret={"user": "u", "secret_key": "k", "extra": "ignored"},
    )

    creds = await load_basic_credentials(
        _Target(),
        _make_operator(),
        fields=("user", "secret_key"),
        mount="kv-custom",
    )

    assert creds == {"user": "u", "secret_key": "k"}
    assert fake.secrets.kv.v2.read_calls[-1]["mount_point"] == "kv-custom"


async def test_non_string_field_is_coerced_to_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numeric secret field round-trips as the str a Basic header expects."""
    install_fake_client(monkeypatch, secret={"username": "u", "password": 12345})

    creds = await load_basic_credentials(_Target(), _make_operator())

    assert creds == {"username": "u", "password": "12345"}


async def test_credential_fields_are_whitespace_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing/leading whitespace on a credential value is stripped on load.

    A trailing newline is the single most common secret-storage artifact
    (`echo` without -n, `jq -r`, an editor's final newline). The connector
    forwards the field verbatim into a Basic-auth header / token body, so a
    stray ``\\n`` surfaces as an upstream 401 that looks like a permissions
    problem. Stripping at the loader fixes it for every connector that goes
    through ``load_basic_credentials`` (#1474).
    """
    install_fake_client(
        monkeypatch,
        secret={"username": "  svc-account\n", "password": "s3cret\n"},
    )

    creds = await load_basic_credentials(_Target(), _make_operator())

    assert creds == {"username": "svc-account", "password": "s3cret"}


async def test_strip_preserves_internal_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only surrounding whitespace is trimmed; internal whitespace survives."""
    install_fake_client(
        monkeypatch,
        secret={"username": "u", "password": "  pass phrase with spaces \n"},
    )

    creds = await load_basic_credentials(_Target(), _make_operator())

    assert creds == {"username": "u", "password": "pass phrase with spaces"}


async def test_secret_ref_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace around the secret_ref path does not slip into the read."""
    fake = install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})

    await load_basic_credentials(_Target(secret_ref="  targets/vc-lab-01  "), _make_operator())

    assert fake.secrets.kv.v2.read_calls[-1]["path"] == "targets/vc-lab-01"


# ---------------------------------------------------------------------------
# Fail-closed: empty operator JWT (system-call carve-out)
# ---------------------------------------------------------------------------


async def test_empty_jwt_fails_closed_before_touching_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system-initiated call (raw_jwt='') errors without a Vault read."""
    fake = install_fake_client(monkeypatch)

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(), _make_operator(jwt=""))

    msg = str(exc.value)
    assert "operator-context credential read requires an authenticated operator" in msg
    assert "vc-lab-01" in msg
    # Vault was never reached — no login, no read.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []


# ---------------------------------------------------------------------------
# Read-phase errors (never a bare KeyError)
# ---------------------------------------------------------------------------


async def test_missing_secret_ref_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured target (secret_ref=None) errors before the read."""
    fake = install_fake_client(monkeypatch)

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(secret_ref=None), _make_operator())

    assert "no secret_ref configured" in str(exc.value)
    assert "vc-lab-01" in str(exc.value)
    assert fake.secrets.kv.v2.read_calls == []


async def test_missing_field_raises_naming_target_and_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret missing 'password' raises the helper error, not a KeyError."""
    install_fake_client(monkeypatch, secret={"username": _CANARY_USERNAME})

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(), _make_operator())

    msg = str(exc.value)
    assert "missing required field 'password'" in msg
    assert "vc-lab-01" in msg
    # secret_ref is named so an operator can find the misconfigured path.
    assert "targets/vc-lab-01" in msg


async def test_missing_field_is_not_a_bare_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing-field path raises VaultCredentialsReadError, not KeyError."""
    install_fake_client(monkeypatch, secret={"username": "u"})

    with pytest.raises(VaultCredentialsReadError):
        await load_basic_credentials(_Target(), _make_operator())
    # A bare KeyError would have escaped the pytest.raises above; reaching
    # here proves the contract. (KeyError is not a VaultCredentialsReadError.)


async def test_malformed_payload_raises_helper_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KV-v2 payload missing the nested data dict raises the helper error.

    The fake's ``read_secret_version`` is monkeypatched to return a
    payload missing the inner ``data`` level, simulating a malformed hvac
    response. The structural unwrap must surface a
    :class:`VaultCredentialsReadError`, not a bare ``KeyError``.
    """
    fake = install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})

    def _malformed(path: str, mount_point: str = "secret", **_kw: object) -> dict[str, object]:
        return {"data": {"metadata": {"version": 1}}}  # no nested "data"

    monkeypatch.setattr(fake.secrets.kv.v2, "read_secret_version", _malformed)

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(), _make_operator())

    assert "malformed payload" in str(exc.value)


# ---------------------------------------------------------------------------
# Fail-closed: API-path-shaped secret_ref (the #989 shape guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_ref",
    [
        "secret/data/foo",
        "kv/data/foo",
        "data/foo",
        "secret/data/vsphere/vcenter-a",
        "kv/data/k8s/rke2-meho",
        "  kv/data/foo  ",  # rejected after the strip, too
    ],
)
async def test_api_path_shaped_secret_ref_is_rejected(
    monkeypatch: pytest.MonkeyPatch, bad_ref: str
) -> None:
    """A KV-v2 API-path-shaped secret_ref fails closed before the read.

    hvac inserts the ``/data/`` segment itself, so a value embedding the
    mount or the ``/data/`` API prefix double-resolves to a 404. The guard
    rejects it with an actionable message naming the target and the fix,
    before Vault is touched.
    """
    fake = install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(secret_ref=bad_ref), _make_operator())

    msg = str(exc.value)
    assert "API-path-shaped secret_ref" in msg
    assert "logical KV-v2 path relative" in msg
    assert "vc-lab-01" in msg  # the target is named
    # Vault was never reached — the guard runs before login and read.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []


@pytest.mark.parametrize(
    "good_ref",
    [
        "targets/data-center-01/host",  # 'data' only as a substring, not a segment
        "vsphere/vcenter-a",
        "targets/vc-lab-01",
        "data-store/primary",  # first segment merely starts with 'data'
        "k8s/rke2-meho/kubeconfig",
        "secret-data/foo",  # 'secret-data' is one segment, not 'secret/data'
    ],
)
async def test_logical_secret_ref_with_data_substring_is_accepted(
    monkeypatch: pytest.MonkeyPatch, good_ref: str
) -> None:
    """A logical path containing 'data' as a non-signature segment is accepted.

    The guard is specific to the leading ``data/`` / ``<mount>/data/``
    signature; ``data`` appearing as a substring or deeper segment must not
    trip a false positive.
    """
    fake = install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})

    creds = await load_basic_credentials(_Target(secret_ref=good_ref), _make_operator())

    assert creds == {"username": "u", "password": "p"}
    # The read addressed the logical path verbatim (stripped), proving it
    # passed the shape guard.
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == good_ref.strip()


async def test_api_path_guard_message_carries_no_credential_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape-guard error names only the path/target — never a credential.

    The guard fires before any Vault read, so no secret is even loaded; the
    message echoes the offending secret_ref path (operator-actionable) but
    no credential value can appear in it.
    """
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(secret_ref="secret/data/foo"), _make_operator())

    msg = str(exc.value)
    assert _CANARY_PASSWORD not in msg
    assert _CANARY_USERNAME not in msg


# ---------------------------------------------------------------------------
# Login-phase errors propagate verbatim
# ---------------------------------------------------------------------------


async def test_login_failure_propagates_as_vault_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role-denied login surfaces as VaultRoleDeniedError, not the read error.

    ``vault_client_for_operator`` maps hvac's ``Forbidden`` to
    :class:`VaultRoleDeniedError` during login; the helper does not catch
    it, so callers can distinguish login-phase from read-phase failure.
    """
    install_fake_client(
        monkeypatch,
        login_exc=VaultRoleDeniedError("role denied"),
    )

    with pytest.raises(VaultRoleDeniedError):
        await load_basic_credentials(_Target(), _make_operator())


# ---------------------------------------------------------------------------
# No secret in logs (AC #3)
# ---------------------------------------------------------------------------


async def test_no_secret_value_in_log_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structlog events carry only target/host/field-names — no values."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    with capture_logs() as captured:
        creds = await load_basic_credentials(_Target(), _make_operator())

    # Sanity: we actually got the secret back, so the canary is live.
    assert creds["password"] == _CANARY_PASSWORD

    loaded_events = [e for e in captured if e.get("event") == "vault_basic_credentials_loaded"]
    assert len(loaded_events) == 1
    event = loaded_events[0]
    assert event["target"] == "vc-lab-01"
    assert event["host"] == "vc-lab-01.example.test"
    assert event["fields"] == ["username", "password"]

    # No credential value (username or password) appears in ANY captured
    # event's serialised form.
    serialised = repr(captured)
    assert _CANARY_PASSWORD not in serialised
    assert _CANARY_USERNAME not in serialised

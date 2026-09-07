# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.scheduler.credentials` (#823, #1478).

The credential-resolution shim sits between the scheduler loop and
:meth:`AgentInvoker.run_scheduled` (G11.2-T2 #1096) -- it sources the
``(client_id, client_secret)`` pair the ``client_credentials`` grant
needs. Resolution is **Vault-first** (G0.19-T2 #1478): the secret is read
from Vault under the scheduler's static service token, falling back to an
env-var pattern derived from the agent's ``identity_ref`` only when Vault
yields nothing.

Coverage matrix
---------------

* **Client-id encoder** -- ``agent:reporter`` -> reversible hex; distinct
  identity refs (incl. separator / case variants) -> distinct segments
  (S10, #298).
* **Vault-first happy path** -- when Vault returns a secret, it wins over
  the env var.
* **Env-var fallback** -- Vault not configured / secret absent -> the env
  var resolves.
* **Vault read error -> env fallback** -- a transient broker error falls
  back to the env var rather than failing the fire.
* **Both miss raises** -- neither Vault nor env -> the loop's "skip + log"
  path via :class:`AgentCredentialsUnresolvedError`.

The Vault read seam is mocked here (the resolver imports
:func:`read_agent_secret` lazily); the live-Vault round-trip is covered
by the integration suite.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import meho_backplane.scheduler.vault_credentials as vault_credentials
from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    agent_client_id_from_identity_ref,
    resolve_agent_credentials,
)
from meho_backplane.settings import get_settings

#: A fixed tenant threaded through resolution below (S10, #298 — the
#: derivation is now per-(tenant, principal)).
_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _env_var(
    identity_ref: str,
    *,
    tenant_id: uuid.UUID = _TENANT,
    pattern: str = "MEHO_AGENT_SECRET_{tenant_id}_{client_id}",
) -> str:
    """Compute the fallback env-var name for *identity_ref* in *tenant_id*.

    Recomputed here from the encoder so the tests set / assert the exact
    per-tenant name the resolver derives without hard-coding hex blobs.
    """
    return pattern.format(
        tenant_id=tenant_id.hex,
        client_id=agent_client_id_from_identity_ref(identity_ref),
    ).upper()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch):
    """Pin :class:`Settings` env vars; clear the lru cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_vault_read(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    """Stub :func:`read_agent_secret` to return *result* (or raise it).

    The resolver imports ``read_agent_secret`` lazily from the
    ``vault_credentials`` module, so patching the module attribute is what
    the import resolves to.
    """

    async def _fake(identity_ref: str, *, tenant_id: uuid.UUID) -> str | None:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(vault_credentials, "read_agent_secret", _fake)


@pytest.mark.parametrize(
    "identity_ref",
    [
        "agent:reporter",
        "agent:incident-triage",
        "agent:billing.summary",
        "agent:multi:colon:ref",
        "AGENT_X",
        "agent_x",
        ":foo:",
    ],
)
def test_client_id_encoder_is_reversible(identity_ref: str) -> None:
    """The client-id segment is a reversible hex encoding of *identity_ref*."""
    encoded = agent_client_id_from_identity_ref(identity_ref)
    assert bytes.fromhex(encoded).decode("utf-8") == identity_ref


def test_client_id_encoder_distinguishes_separator_and_case_variants() -> None:
    """S10 #298: variants that collapsed to ``AGENT_A_B`` under the old
    sanitiser now encode distinctly — even after the callers' ``upper()``."""
    variants = ["agent:a-b", "agent:a_b", "agent:a.b", "agent:A-B"]
    encoded = {agent_client_id_from_identity_ref(v).upper() for v in variants}
    assert len(encoded) == len(variants)


async def test_vault_first_secret_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Vault returns a secret, it is used even if the env var is set."""
    _patch_vault_read(monkeypatch, "vault-secret")
    monkeypatch.setenv(_env_var("agent:reporter"), "env-secret")

    client_id, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert client_id == "agent:reporter"
    assert secret == "vault-secret"


async def test_env_fallback_when_vault_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault secret absent (``None``) -> the env-var fallback resolves."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(_env_var("agent:reporter"), "env-secret")

    client_id, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert client_id == "agent:reporter"
    assert secret == "env-secret"


async def test_env_fallback_when_vault_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``VAULT_SCHEDULER_TOKEN`` -> NotConfigured -> env fallback.

    Exercises the real :func:`read_agent_secret`, which raises
    :class:`SchedulerVaultNotConfiguredError` before touching Vault when
    the token is unset; the resolver swallows it and falls back.
    """
    monkeypatch.delenv("VAULT_SCHEDULER_TOKEN", raising=False)
    monkeypatch.setenv(_env_var("agent:reporter"), "env-secret")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert secret == "env-secret"


async def test_vault_broker_error_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault read error falls back to the env var rather than failing.

    A transient Vault outage must not block an agent whose secret is also
    wired into the pod env (break-glass).
    """
    _patch_vault_read(
        monkeypatch,
        vault_credentials.SchedulerVaultBrokerError("vault unreachable"),
    )
    monkeypatch.setenv(_env_var("agent:reporter"), "env-secret")

    _, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert secret == "env-secret"


async def test_resolve_uses_sanitised_uppercased_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var name is sanitised then upper-cased (fallback path)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(
        _env_var("agent:incident-triage"),
        "triage-secret",
    )

    _, secret = await resolve_agent_credentials("agent:incident-triage", tenant_id=_TENANT)

    assert secret == "triage-secret"


async def test_resolve_missing_both_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither Vault nor env -> :class:`AgentCredentialsUnresolvedError`."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.delenv(_env_var("agent:reporter"), raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError, match=r"agent:reporter"):
        await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)


async def test_resolve_empty_env_with_no_vault_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty env-var value is treated as unset (Helm template defence)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(_env_var("agent:reporter"), "")

    with pytest.raises(AgentCredentialsUnresolvedError):
        await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)


async def test_resolve_whitespace_only_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env-var value is treated as unset."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(_env_var("agent:reporter"), "   \n\t  ")

    with pytest.raises(AgentCredentialsUnresolvedError):
        await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)


async def test_custom_env_pattern_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default ``SCHEDULER_AGENT_SECRET_ENV_PATTERN`` reroutes the lookup."""
    _patch_vault_read(monkeypatch, None)
    pattern = "OPS_AGENT_{tenant_id}_{client_id}_PASSPHRASE"
    monkeypatch.setenv("SCHEDULER_AGENT_SECRET_ENV_PATTERN", pattern)
    monkeypatch.setenv(_env_var("agent:reporter", pattern=pattern), "rotating-key")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert secret == "rotating-key"


async def test_resolve_uppercases_full_env_var_name_even_with_lowercase_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lower-cased pattern still resolves to the upper-cased env var."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(
        "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
        "meho_agent_secret_{tenant_id}_{client_id}",
    )
    monkeypatch.setenv(_env_var("agent:reporter"), "the-secret")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter", tenant_id=_TENANT)

    assert secret == "the-secret"


@pytest.mark.parametrize(
    "bad_pattern",
    [
        "MEHO_AGENT_SECRET_PROD",  # no placeholders at all
        "MEHO_AGENT_SECRET_{tenant_id}",  # missing {client_id} (S10 #298)
        "MEHO_AGENT_SECRET_{client_id}",  # missing {tenant_id} (S10 #298)
        "MEHO_AGENT_SECRET_{0}_{client_id}",  # positional, no tenant_id key
        "MEHO_AGENT_SECRET_{tenant_id}_{client_id",  # unbalanced opening brace
        "MEHO_AGENT_SECRET_{tenant_id}_client_id}",  # unbalanced closing brace
    ],
)
def test_settings_rejects_malformed_scheduler_secret_pattern(
    monkeypatch: pytest.MonkeyPatch,
    bad_pattern: str,
) -> None:
    """The settings validator fails fast at load on malformed patterns."""
    monkeypatch.setenv("SCHEDULER_AGENT_SECRET_ENV_PATTERN", bad_pattern)
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="SCHEDULER_AGENT_SECRET_ENV_PATTERN"):
        get_settings()


def test_settings_accepts_valid_scheduler_secret_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: a valid pattern passes the validator without error."""
    pattern = "CORP_AGENT_{tenant_id}_{client_id}_SECRET"
    monkeypatch.setenv("SCHEDULER_AGENT_SECRET_ENV_PATTERN", pattern)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.scheduler_agent_secret_env_pattern == pattern


async def test_error_message_names_expected_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error names the env var operators need to set (fallback path)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.delenv(_env_var("agent:billing.summary"), raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError) as excinfo:
        await resolve_agent_credentials("agent:billing.summary", tenant_id=_TENANT)

    msg = str(excinfo.value)
    assert _env_var("agent:billing.summary") in msg

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

* **Sanitiser** -- ``agent:reporter`` -> ``AGENT_REPORTER`` (default
  upper-case + non-alnum -> ``_``); edge cases.
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

from typing import Any

import pytest

import meho_backplane.scheduler.vault_credentials as vault_credentials
from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    agent_client_id_from_identity_ref,
    resolve_agent_credentials,
)
from meho_backplane.settings import get_settings


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

    async def _fake(identity_ref: str) -> str | None:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(vault_credentials, "read_agent_secret", _fake)


@pytest.mark.parametrize(
    ("identity_ref", "expected"),
    [
        ("agent:reporter", "agent_reporter"),
        ("agent:incident-triage", "agent_incident_triage"),
        ("agent:billing.summary", "agent_billing_summary"),
        ("agent:multi:colon:ref", "agent_multi_colon_ref"),
        ("AGENT_X", "AGENT_X"),
        ("agent_x", "agent_x"),
        (":foo:", "_foo_"),
    ],
)
def test_sanitiser_collapses_non_env_chars(identity_ref: str, expected: str) -> None:
    """Non-``[A-Za-z0-9_]`` chars in *identity_ref* collapse to ``_``."""
    assert agent_client_id_from_identity_ref(identity_ref) == expected


async def test_vault_first_secret_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Vault returns a secret, it is used even if the env var is set."""
    _patch_vault_read(monkeypatch, "vault-secret")
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "env-secret")

    client_id, secret = await resolve_agent_credentials("agent:reporter")

    assert client_id == "agent:reporter"
    assert secret == "vault-secret"


async def test_env_fallback_when_vault_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault secret absent (``None``) -> the env-var fallback resolves."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "env-secret")

    client_id, secret = await resolve_agent_credentials("agent:reporter")

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
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "env-secret")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter")

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
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "env-secret")

    _, secret = await resolve_agent_credentials("agent:reporter")

    assert secret == "env-secret"


async def test_resolve_uses_sanitised_uppercased_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var name is sanitised then upper-cased (fallback path)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(
        "MEHO_AGENT_SECRET_AGENT_INCIDENT_TRIAGE",
        "triage-secret",
    )

    _, secret = await resolve_agent_credentials("agent:incident-triage")

    assert secret == "triage-secret"


async def test_resolve_missing_both_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither Vault nor env -> :class:`AgentCredentialsUnresolvedError`."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.delenv("MEHO_AGENT_SECRET_AGENT_REPORTER", raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError, match=r"agent:reporter"):
        await resolve_agent_credentials("agent:reporter")


async def test_resolve_empty_env_with_no_vault_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty env-var value is treated as unset (Helm template defence)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "")

    with pytest.raises(AgentCredentialsUnresolvedError):
        await resolve_agent_credentials("agent:reporter")


async def test_resolve_whitespace_only_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env-var value is treated as unset."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "   \n\t  ")

    with pytest.raises(AgentCredentialsUnresolvedError):
        await resolve_agent_credentials("agent:reporter")


async def test_custom_env_pattern_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default ``SCHEDULER_AGENT_SECRET_ENV_PATTERN`` reroutes the lookup."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(
        "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
        "OPS_AGENT_{client_id}_PASSPHRASE",
    )
    monkeypatch.setenv("OPS_AGENT_AGENT_REPORTER_PASSPHRASE", "rotating-key")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter")

    assert secret == "rotating-key"


async def test_resolve_uppercases_full_env_var_name_even_with_lowercase_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lower-cased pattern still resolves to the upper-cased env var."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.setenv(
        "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
        "meho_agent_secret_{client_id}",
    )
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "the-secret")
    get_settings.cache_clear()

    _, secret = await resolve_agent_credentials("agent:reporter")

    assert secret == "the-secret"


@pytest.mark.parametrize(
    "bad_pattern",
    [
        "MEHO_AGENT_SECRET_PROD",  # no {client_id} placeholder
        "MEHO_AGENT_SECRET_{0}",  # positional, no client_id key
        "MEHO_AGENT_SECRET_{client_id",  # unbalanced opening brace
        "MEHO_AGENT_SECRET_client_id}",  # unbalanced closing brace
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
    monkeypatch.setenv(
        "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
        "CORP_AGENT_{client_id}_SECRET",
    )
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.scheduler_agent_secret_env_pattern == "CORP_AGENT_{client_id}_SECRET"


async def test_error_message_names_expected_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error names the env var operators need to set (fallback path)."""
    _patch_vault_read(monkeypatch, None)
    monkeypatch.delenv("MEHO_AGENT_SECRET_AGENT_BILLING_SUMMARY", raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError) as excinfo:
        await resolve_agent_credentials("agent:billing.summary")

    msg = str(excinfo.value)
    assert "MEHO_AGENT_SECRET_AGENT_BILLING_SUMMARY" in msg

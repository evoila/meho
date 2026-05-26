# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.scheduler.credentials` (G11.3-T2 #823).

The credential-resolution shim sits between the scheduler loop and
:meth:`AgentInvoker.run_scheduled` (G11.2-T2 #1096) -- it sources the
``(client_id, client_secret)`` pair the ``client_credentials`` grant
needs by looking the secret up via an env-var pattern derived from
the agent's ``identity_ref``.

Coverage matrix
---------------

* **Sanitiser** -- ``agent:reporter`` -> ``AGENT_REPORTER`` (default
  upper-case + non-alnum -> ``_``); edge cases:
  ``agent:incident-triage`` -> ``AGENT_INCIDENT_TRIAGE``; trailing /
  leading punctuation collapses; already-clean strings are pass-through.
* **Resolution happy path** -- when the env var derived from the
  pattern + identity_ref is set, return ``(identity_ref, secret)``
  exactly. Identity_ref is *not* sanitised in the returned tuple
  (Keycloak's client-id namespace tolerates the original form).
* **Missing env var raises** -- unset env -> the loop's "skip + log"
  path is exercised by :class:`AgentCredentialsUnresolvedError`.
* **Empty env var raises** -- ``MEHO_AGENT_SECRET_X=""`` is the same
  as not setting it (strip semantics; defends against Helm template
  leaks that render an empty value).
* **Custom env pattern** -- a non-default
  ``SCHEDULER_AGENT_SECRET_ENV_PATTERN`` is honoured (operators wiring
  agent secrets through a non-MEHO env prefix).

These tests live separately from :mod:`tests.test_scheduler` so the
credential boundary stays its own audited unit -- regressions in the
sanitiser or env-pattern expansion fail here without being masked by
the integration-shaped scheduler-loop tests.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("identity_ref", "expected"),
    [
        ("agent:reporter", "agent_reporter"),
        ("agent:incident-triage", "agent_incident_triage"),
        ("agent:billing.summary", "agent_billing_summary"),
        ("agent:multi:colon:ref", "agent_multi_colon_ref"),
        # Already env-clean -- no rewrite needed.
        ("AGENT_X", "AGENT_X"),
        ("agent_x", "agent_x"),
        # Edge: leading + trailing punctuation collapse.
        (":foo:", "_foo_"),
    ],
)
def test_sanitiser_collapses_non_env_chars(identity_ref: str, expected: str) -> None:
    """Non-``[A-Za-z0-9_]`` chars in *identity_ref* collapse to ``_``."""
    assert agent_client_id_from_identity_ref(identity_ref) == expected


def test_resolve_returns_identity_ref_verbatim_as_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned ``client_id`` is the *identity_ref verbatim* (no sanitisation).

    Keycloak's client-id namespace tolerates the ``:`` separators MEHO
    uses, and the ``client_credentials`` grant carries the original
    identifier. The sanitisation applies only to the env-var-name
    derivation.
    """
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "s3cr3t")

    client_id, client_secret = resolve_agent_credentials("agent:reporter")

    assert client_id == "agent:reporter"
    assert client_secret == "s3cr3t"


def test_resolve_uses_sanitised_uppercased_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var name is sanitised then upper-cased.

    Pattern default ``MEHO_AGENT_SECRET_{client_id}`` against
    ``agent:incident-triage`` -> ``MEHO_AGENT_SECRET_AGENT_INCIDENT_TRIAGE``.
    """
    monkeypatch.setenv(
        "MEHO_AGENT_SECRET_AGENT_INCIDENT_TRIAGE",
        "triage-secret",
    )

    _, secret = resolve_agent_credentials("agent:incident-triage")

    assert secret == "triage-secret"


def test_resolve_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset env var -> :class:`AgentCredentialsUnresolvedError`."""
    monkeypatch.delenv("MEHO_AGENT_SECRET_AGENT_REPORTER", raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError, match=r"agent:reporter"):
        resolve_agent_credentials("agent:reporter")


def test_resolve_empty_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env-var value is treated as unset (Helm template defence)."""
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "")

    with pytest.raises(AgentCredentialsUnresolvedError):
        resolve_agent_credentials("agent:reporter")


def test_resolve_whitespace_only_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env-var value is treated as unset.

    Defence against a Helm template that renders ``"\n"`` for an
    unbound secret -- a literal-newline secret would pass an
    ``if env_value`` check but fail Keycloak's grant request.
    """
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "   \n\t  ")

    with pytest.raises(AgentCredentialsUnresolvedError):
        resolve_agent_credentials("agent:reporter")


def test_custom_env_pattern_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default ``SCHEDULER_AGENT_SECRET_ENV_PATTERN`` reroutes the lookup.

    Operators wiring agent secrets through a non-``MEHO_AGENT_SECRET_``
    prefix swap the pattern; the resolver must read from the alternate
    name without code changes.
    """
    monkeypatch.setenv(
        "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
        "OPS_AGENT_{client_id}_PASSPHRASE",
    )
    monkeypatch.setenv("OPS_AGENT_AGENT_REPORTER_PASSPHRASE", "rotating-key")
    get_settings.cache_clear()

    _, secret = resolve_agent_credentials("agent:reporter")

    assert secret == "rotating-key"


def test_error_message_names_expected_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The :class:`AgentCredentialsUnresolvedError` message names the env-var
    operators need to set.

    A reader of the scheduler's WARN log line should be able to copy
    the env-var name from the error into their Helm values without
    deriving the sanitisation by hand.
    """
    monkeypatch.delenv("MEHO_AGENT_SECRET_AGENT_BILLING_SUMMARY", raising=False)

    with pytest.raises(AgentCredentialsUnresolvedError) as excinfo:
        resolve_agent_credentials("agent:billing.summary")

    msg = str(excinfo.value)
    assert "MEHO_AGENT_SECRET_AGENT_BILLING_SUMMARY" in msg

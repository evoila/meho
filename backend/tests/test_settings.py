# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.settings`.

The validator under test is the async-DSN guard on
:attr:`Settings.database_url` (T27 review iteration 1, M2): ADR 0004
mandates that every database I/O path off the request hot loop is
``await``-able, so a sync DSN like ``postgresql://`` or ``sqlite:///``
must fail at :class:`Settings` construction time rather than silently
landing as the engine URL and blocking the FastAPI event loop on the
first DB checkout.

The tests below cover both halves of the contract:

* Accepted async schemes (``postgresql+asyncpg://`` and
  ``sqlite+aiosqlite://``) construct successfully.
* Sync schemes raise :class:`ValueError` with the supported-schemes
  message (so the operator can fix ``DATABASE_URL`` from the error
  text alone).

A second batch (#964 M2) covers env-var hygiene at ingest for the
three Vault-rendered UI knobs: trailing-newline values must be
stripped by :func:`get_settings` so downstream Fernet /
``client_secret_basic`` consumers see the operator's intended value.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from meho_backplane.settings import Settings, get_settings


def _settings_kwargs(database_url: str) -> dict[str, object]:
    """Return the minimum kwargs needed to construct :class:`Settings`.

    Only ``database_url`` varies across tests; the rest are fixed at
    valid sentinels so the construction failure (when it happens) is
    unambiguously the validator under test.
    """
    return {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": database_url,
    }


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://meho:secret@db.test:5432/meho",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///tmp/test.db",
    ],
)
def test_database_url_accepts_async_drivers(url: str) -> None:
    """Both supported async schemes construct without raising."""
    settings = Settings(**_settings_kwargs(url))  # type: ignore[arg-type]
    assert settings.database_url == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://meho:secret@db.test:5432/meho",
        "postgresql+psycopg2://meho:secret@db.test:5432/meho",
        "postgres://meho:secret@db.test:5432/meho",
        "sqlite:///:memory:",
        "mysql://meho:secret@db.test/meho",
    ],
)
def test_database_url_rejects_sync_or_unsupported_drivers(url: str) -> None:
    """Sync DSNs and unsupported schemes raise with the supported-schemes message.

    The error message must name the supported schemes so the operator
    can fix ``DATABASE_URL`` from the error text alone — without
    grepping the codebase or hunting for ADR 0004. The assertion below
    pins both supported scheme prefixes literally.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_settings_kwargs(url))  # type: ignore[arg-type]
    message = str(exc_info.value)
    assert "postgresql+asyncpg://" in message
    assert "sqlite+aiosqlite://" in message


# ---------------------------------------------------------------------------
# #964 M2 — Vault-rendered env vars stripped at ingest
# ---------------------------------------------------------------------------


def test_get_settings_strips_trailing_newline_from_ui_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing-newline env-var values are stripped by :func:`get_settings`.

    Regression for #964 M2. Vault-rendering chains (Vault Agent
    templates, ``vault kv get -field`` default mode) commonly emit
    secret values with a trailing ``\\n``. The non-empty check at the
    consumer site passes, but downstream surfaces reject the padded
    value:

    * ``client_secret_basic`` token-endpoint auth -- Keycloak returns
      ``invalid_client`` on a credential with a trailing newline.
    * ``Fernet(key.encode("ascii"))`` -- raises ``binascii.Error`` on
      the padded base64 string.

    Stripping at ingest in ``get_settings`` is the single seam every
    consumer flows through, so the downstream errors above become
    unreachable from Vault-shaped misconfig.
    """
    fernet_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # Trailing newline on each of the three UI knobs the task scopes.
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web\n")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "s3cr3t\n")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", f"{fernet_key}\n")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ui_keycloak_client_id == "meho-web"
        assert settings.ui_keycloak_client_secret == "s3cr3t"
        assert settings.ui_session_encryption_key == fernet_key
        # Fernet construction round-trip proves the stripped key is the
        # 32-byte URL-safe base64 shape ``Fernet`` accepts -- the failure
        # mode this guard prevents in production.
        Fernet(settings.ui_session_encryption_key.encode("ascii"))
    finally:
        get_settings.cache_clear()


def test_get_settings_preserves_empty_string_default_for_unset_ui_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip is a no-op for unset env vars; the empty-string default flows through.

    Sanity check: the strip change must not regress the unset-default
    path. An empty default ``""`` stays ``""`` after ``.strip()``;
    the downstream ``_ensure_client_configured`` guard in
    :mod:`meho_backplane.ui.auth.flow` (and the BFF route 503 mapping)
    continues to fire on the genuine "operator never rendered the
    credentials" case.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("UI_KEYCLOAK_CLIENT_ID", raising=False)
    monkeypatch.delenv("UI_KEYCLOAK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("UI_SESSION_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ui_keycloak_client_id == ""
        assert settings.ui_keycloak_client_secret == ""
        assert settings.ui_session_encryption_key == ""
    finally:
        get_settings.cache_clear()

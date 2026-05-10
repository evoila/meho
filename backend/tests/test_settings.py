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
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_backplane.settings import Settings


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

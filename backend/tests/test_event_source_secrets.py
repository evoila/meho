# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault secret-custody tests for the ``event_source`` registry (#2880).

Asserts the derived path shape and that ``store_event_source_secret``
carries the value through the secret-broker vault-kv sink to a KV-v2
write -- with the raw value never touching a log line (the autouse
secret-leak sweep in :mod:`tests.conftest` is the safety net).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.event_source.secrets import (
    event_source_secret_ref,
    store_event_source_secret,
)

from ._event_source_helpers import (
    _settings_env,  # noqa: F401  (autouse fixture)
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _operator() -> Operator:
    return Operator(
        sub="admin-1",
        raw_jwt="unused-in-mock",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def test_secret_ref_derivation_shape() -> None:
    ref = event_source_secret_ref(_TENANT, "prod-am")
    assert ref == f"tenants/{_TENANT}/event-sources/prod-am"


@pytest.mark.asyncio
async def test_store_writes_value_to_vault_kv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value round-trips to a KV-v2 write at the derived path/field."""
    calls: list[dict[str, Any]] = []

    def _create_or_update_secret(**kwargs: Any) -> None:
        calls.append(kwargs)

    fake_client = SimpleNamespace(
        secrets=SimpleNamespace(
            kv=SimpleNamespace(v2=SimpleNamespace(create_or_update_secret=_create_or_update_secret))
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_client_for_operator(_operator: Operator) -> AsyncIterator[Any]:
        yield fake_client

    monkeypatch.setattr(
        "meho_backplane.auth.vault.vault_client_for_operator", _fake_client_for_operator
    )

    ref = event_source_secret_ref(_TENANT, "prod-am")
    await store_event_source_secret(_operator(), ref, SecretStr("hmac-signing-key"))

    assert len(calls) == 1
    assert calls[0]["path"] == f"tenants/{_TENANT}/event-sources/prod-am"
    assert calls[0]["secret"] == {"secret": "hmac-signing-key"}
    assert calls[0]["mount_point"] == "secret"

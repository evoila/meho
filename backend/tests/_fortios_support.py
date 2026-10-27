# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared test helpers for the FortiGate connector tests: targets, operator, fixtures."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.fortios import (
    FORTIOS_IMPL_ID,
    FORTIOS_PRODUCT,
    FORTIOS_VERSION,
    FortiOSConnector,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings
from tests._fortios_fixtures import HOST, TOKEN


class FortiTarget:
    """A FortiGate target with the fields the connector and the resolver read."""

    def __init__(
        self,
        *,
        scope: str = "device",
        vdom: str | None = None,
        version: str = "7.4.4",
        extras: dict[str, Any] | None = None,
    ) -> None:
        self.product = FORTIOS_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000f0f01")
        self.name = f"fgt-{scope}-{self.id.hex[:6]}"
        self.host = HOST
        self.port = 443
        self.secret_ref = "targets/fortigate/example"
        self.auth_model = "shared_service_account"
        self.tls_ca_pin: str | None = None
        self.tls_server_name: str | None = None
        if extras is None:
            extras = {"scope": scope}
            if vdom is not None:
                extras["vdom"] = vdom
        self.extras = extras


def make_operator() -> Operator:
    return Operator(
        sub="op-fortigate",
        name="FortiGate Operator",
        email=None,
        raw_jwt="op.fortigate.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000f0f02"),
        tenant_role=TenantRole.OPERATOR,
    )


async def token_loader(_target: object, _operator: Operator) -> str:
    return TOKEN


def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=FORTIOS_PRODUCT,
        version=FORTIOS_VERSION,
        impl_id=FORTIOS_IMPL_ID,
        cls=FortiOSConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


def stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def no_retry_wait(connector: FortiOSConnector, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry connection errors without waiting, so a TLS failure test stays fast."""
    from tenacity import wait_none

    monkeypatch.setattr(connector._request_json.retry, "wait", wait_none())  # type: ignore[attr-defined]

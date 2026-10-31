# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for the tests of the Automation integration ops.

The ops are written once (``connectors/vmware_shared``) and registered on the
VCF Automation 9 connector (``vcfa.tenant.*``) and on the Aria Automation 8
connector (``vcfa.vra8.*``). These helpers drive the ``vcfa.tenant`` ids
through the real dispatcher against a respx-mocked appliance. All names, ids
and secrets are made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx

from meho_backplane.connectors.vcf_automation import (
    VcfAutomationConnector,
    register_vcfa_integration_operations,
)
from meho_backplane.connectors.vmware_shared import vcfa_integration_calls
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from tests._vcfa_provisioning_support import (
    BASE_URL,
    seed_target,
    wire_connector,
)

#: The password the stubbed Vault read returns. It must never reach a result.
VAULT_VALUE = "Pw-sentinel-4501a"
#: The Vault ref the calls name.
SECRET_REF = "tenants/example/integration-login"

INTEGRATIONS = f"{BASE_URL}/iaas/api/integrations"
TRACKER = f"{BASE_URL}/iaas/api/request-tracker"
SOURCES = f"{BASE_URL}/content/api/sources"
SYNC_REQUESTS = f"{BASE_URL}/content/api/sourcecontrol/sync-requests"
SYNC_HISTORY = f"{BASE_URL}/content/api/sourcecontrol/sync-history"
RANGES = f"{BASE_URL}/iaas/api/external-network-ip-ranges"
PROFILES = f"{BASE_URL}/iaas/api/network-profiles"
PROVIDERS = f"{BASE_URL}/provisioning/ipam/api/providers"
IMPORT = f"{BASE_URL}/provisioning/ipam/api/providers/packages/import"

#: The table of the Task: op name -> (safety level, requires approval).
TIERS: dict[str, tuple[str, bool]] = {
    "integration.list": ("safe", False),
    "integration.set": ("dangerous", True),
    "integration.delete": ("destructive", True),
    "ipam_package.import": ("destructive", True),
    "git_repo.set": ("dangerous", True),
}


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(vcfa_integration_calls, "POLL_SECONDS", 0.0)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the Vault read of ``password_secret_ref``."""
    stub = AsyncMock(return_value={"password": VAULT_VALUE})
    monkeypatch.setattr(vcfa_integration_calls, "load_param_secret_data", stub)
    return stub


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await register_vcfa_integration_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def mount_logins_full(mock: respx.MockRouter) -> None:
    """Mount the provider and tenant logins on *mock* with full URLs."""
    mock.post(f"{BASE_URL}/cloudapi/1.0.0/sessions/provider").respond(
        200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "prov-jwt"}
    )
    mock.post(f"{BASE_URL}/iaas/api/login").respond(200, json={"token": "tenant-token"})


def tracker(tracker_id: str, status: str, **extra: Any) -> dict[str, Any]:
    """A vendor request tracker."""
    return {
        "id": tracker_id,
        "status": status,
        "progress": 100 if status != "INPROGRESS" else 40,
        "selfLink": f"/iaas/api/request-tracker/{tracker_id}",
        **extra,
    }


def body(route: respx.Route, index: int = -1) -> Any:
    """The JSON body of one call of *route*."""
    return json.loads(route.calls[index].request.content)


def no_secret(value: Any) -> bool:
    """True when *value*, as JSON, does not hold the stubbed secret."""
    return VAULT_VALUE not in json.dumps(value)


__all__ = [
    "BASE_URL",
    "IMPORT",
    "INTEGRATIONS",
    "PROFILES",
    "PROVIDERS",
    "RANGES",
    "SECRET_REF",
    "SOURCES",
    "SYNC_HISTORY",
    "SYNC_REQUESTS",
    "TIERS",
    "TRACKER",
    "VAULT_VALUE",
    "body",
    "mount_logins_full",
    "no_secret",
    "tracker",
]

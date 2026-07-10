# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Boot-time ExecutionProfile stamping (#2288).

Pins the parent-Initiative (#2271) acceptance criteria for wiring
``record_profile_stamp`` into production via
:func:`~meho_backplane.operations.ingest.boot_stamp.stamp_catalog_profiled_connectors`:

* A fresh boot registers a ``ProfiledRestConnector`` for every catalog row
  carrying a ``profile_resource``; a row whose ``(product, version, impl_id)``
  triple is already served by a hand-coded class no-ops without error.
* A dispatched op through a boot-stamped profiled connector works end to end
  (vRLI-shaped ``session_login`` test profile against a mock), proving the
  registered class is dispatch-capable, not merely present.
* The stamp path does no network I/O, and a malformed profile fails closed.

The review-gate invariant (#1971 — stamping never auto-enables an op) is
pinned in :mod:`tests.test_operations_profile_stamp_gate` against the boot
trigger.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector, shim_kind
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._branches import dispatch_ingested
from meho_backplane.operations.ingest.boot_stamp import (
    BOOT_STAMP_OPERATOR_SUB,
    stamp_catalog_profiled_connectors,
)
from meho_backplane.operations.ingest.catalog import (
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_catalog,
)
from meho_backplane.settings import get_settings

_LOADER_MODULE = "meho_backplane.operations.ingest.boot_stamp.load_profile_resource"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _profile_entry(
    *,
    product: str,
    version: str,
    impl_id: str,
    profile_resource: str,
    requires_connector_class: str = "ProfiledRestConnector_placeholder",
) -> ConnectorSpecEntry:
    """Build a profile-backed catalog entry pointing at *profile_resource*."""
    return ConnectorSpecEntry.model_validate(
        {
            "product": product,
            "version": version,
            "impl_id": impl_id,
            "requires_connector_class": requires_connector_class,
            "upstream": None,
            "spec_resource": profile_resource,
            "profile_resource": profile_resource,
        }
    )


class _HandRolledFixture(Connector):
    """A dispatchable hand-coded class occupying the fixture triple (T4 precedent)."""

    product = "fixture"
    version = "1.0"
    impl_id = "fixture-rest"
    supported_version_range = ">=1.0,<2.0"
    priority = 5

    async def fingerprint(self, target: Any, operator: Operator | None = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# AC1 — boot registers a profiled connector per profile row; occupied no-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_stamp_registers_profiled_for_unoccupied_row() -> None:
    """An unoccupied profile-backed row registers a dispatchable profiled class."""
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="_fixture",
                version="1.0",
                impl_id="fixture-rest",
                profile_resource="_fixture_minimal.yaml",
            ),
        )
    )

    count = await stamp_catalog_profiled_connectors(cat)

    assert count == 1
    # The catalog product ``_fixture`` parses to the dispatch-canonical
    # ``fixture`` (the underscore prefix does not round-trip), so the class
    # registers under the parsed triple.
    cls = all_connectors_v2()[("fixture", "1.0", "fixture-rest")]
    assert shim_kind(cls) == "profiled"
    assert cls.__name__ == "ProfiledRestConnector_fixture_1_0"
    assert cls.profile is not None
    assert cls.profile.auth.scheme == "basic"
    # Bounded range derived from the label — beats a bare shim, never a class.
    assert cls.supported_version_range == ">=1.0,<2.0"
    assert cls.priority == 0


@pytest.mark.asyncio
async def test_boot_stamp_noops_on_triple_occupied_by_handrolled_class() -> None:
    """A triple already served by a hand-coded class is left untouched (no error)."""
    register_connector_v2(
        product="fixture", version="1.0", impl_id="fixture-rest", cls=_HandRolledFixture
    )
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="_fixture",
                version="1.0",
                impl_id="fixture-rest",
                profile_resource="_fixture_minimal.yaml",
            ),
        )
    )

    count = await stamp_catalog_profiled_connectors(cat)

    assert count == 0
    # The hand-coded class still owns the triple — never overwritten.
    assert all_connectors_v2()[("fixture", "1.0", "fixture-rest")] is _HandRolledFixture


@pytest.mark.asyncio
async def test_boot_stamp_shipped_catalog_registers_every_profile_row() -> None:
    """Against the real shipped catalog every profile-backed row stamps cleanly.

    Runs the real ``load_profile_resource`` + ``ExecutionProfile`` parse +
    class synthesis for the shipped vmware (``session_login_basic``), sddc
    (``basic``) and fixture (``basic``) profiles. With a cleared registry no
    triple is occupied, so every profile row registers a profiled class.
    """
    expected = sum(1 for e in load_catalog().entries if e.profile_resource is not None)
    assert expected >= 1  # guard the assertion against a catalog with zero profile rows

    count = await stamp_catalog_profiled_connectors()

    assert count == expected
    registry = all_connectors_v2()
    assert ("fixture", "1.0", "fixture-rest") in registry
    assert ("vmware", "9.0", "vmware-rest") in registry
    for cls in registry.values():
        assert shim_kind(cls) == "profiled"


@pytest.mark.asyncio
async def test_boot_stamp_is_idempotent_within_a_process() -> None:
    """A second run over an already-stamped catalog registers nothing new."""
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="_fixture",
                version="1.0",
                impl_id="fixture-rest",
                profile_resource="_fixture_minimal.yaml",
            ),
        )
    )

    assert await stamp_catalog_profiled_connectors(cat) == 1
    assert await stamp_catalog_profiled_connectors(cat) == 0


# ---------------------------------------------------------------------------
# AC5 — no network I/O in the stamp path; malformed profile fails closed
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_boot_stamp_makes_no_network_calls() -> None:
    """The stamp path issues no HTTP request.

    ``respx.mock`` raises on any unmocked request; no routes are registered,
    so completing the stamp proves the path is network-free.
    """
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="_fixture",
                version="1.0",
                impl_id="fixture-rest",
                profile_resource="_fixture_minimal.yaml",
            ),
        )
    )

    assert await stamp_catalog_profiled_connectors(cat) == 1


@pytest.mark.asyncio
async def test_boot_stamp_fails_closed_on_malformed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed profile propagates a validation error (crashes the lifespan).

    Defense-in-depth: the boot validator (``validate_shipped_artifacts``) is
    the primary fail-closed guard and runs first, but the stamp path re-parses
    with the same model and must never swallow a bad profile into a silently
    unregistered connector.
    """
    monkeypatch.setattr(_LOADER_MODULE, lambda _resource: "product: x\nversion: '1.0'\n")
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="x",
                version="1.0",
                impl_id="x-rest",
                profile_resource="broken.yaml",
            ),
        )
    )

    with pytest.raises(ValidationError):
        await stamp_catalog_profiled_connectors(cat)


# ---------------------------------------------------------------------------
# AC3 — a dispatched op through a boot-stamped profiled connector works E2E
# ---------------------------------------------------------------------------

_VRLI_PROFILE_YAML = """
product: vrli
version: "8.12"
auth:
  scheme: session_login
  secret_fields:
    - username
    - password
fingerprint:
  path: /api/v2/version
  authenticated: false
  version_key: version
  version_splitter: none
probe: delegate
pagination:
  strategy: none
  items_key: events
"""


@dataclass
class _StubTarget:
    name: str = "vrli"
    host: str = "vrli.invalid"
    port: int | None = 443
    secret_ref: str | None = "p/secret"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc", "password": "pw"}


def _events_descriptor() -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vrli",
        version="8.12",
        impl_id="vrli-rest",
        op_id="vrli.events.list",
        source_kind="ingested",
        method="GET",
        path="/api/v1/events",
        parameter_schema={"type": "object", "properties": {}},
    )


@respx.mock
@pytest.mark.asyncio
async def test_boot_stamped_profiled_connector_dispatches_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot-stamped ``session_login`` connector authenticates and dispatches an op.

    Proves the registered class is not just present but dispatch-capable: the
    session-login round-trip mints a token and the op GET returns the payload.
    """
    monkeypatch.setattr(_LOADER_MODULE, lambda _resource: _VRLI_PROFILE_YAML)
    cat = ConnectorSpecCatalog(
        entries=(
            _profile_entry(
                product="vrli",
                version="8.12",
                impl_id="vrli-rest",
                profile_resource="vrli.yaml",
            ),
        )
    )

    assert await stamp_catalog_profiled_connectors(cat) == 1
    cls = all_connectors_v2()[("vrli", "8.12", "vrli-rest")]
    assert shim_kind(cls) == "profiled"

    # The production dispatcher instantiates the registered class with no args;
    # the test injects a credentials_loader to skip the Vault read. The vetted
    # profile rides on the class attribute set by the stamp.
    connector = cls(credentials_loader=_loader)

    login = respx.post("https://vrli.invalid/api/v2/sessions").mock(
        return_value=httpx.Response(200, json={"sessionId": "sess-xyz", "ttl": 1800})
    )
    op = respx.get("https://vrli.invalid/api/v1/events").mock(
        return_value=httpx.Response(200, json={"events": [{"id": 1}, {"id": 2}]})
    )

    result = await dispatch_ingested(
        connector=connector,
        descriptor=_events_descriptor(),
        operator=_operator(),
        target=_StubTarget(),
        params={},
    )

    assert result == {"events": [{"id": 1}, {"id": 2}]}
    assert login.called
    assert op.called
    # The op request carried the minted session token as a Bearer header.
    assert op.calls[0].request.headers["authorization"] == "Bearer sess-xyz"
    # The login body carried the loader's credentials, no stale auth header.
    assert json.loads(login.calls[0].request.read().decode())["username"] == "svc"
    await connector.aclose()


def test_boot_stamp_operator_sub_is_a_system_principal() -> None:
    """The stamp attributes its audit rows to a stable ``system:*`` sub."""
    assert BOOT_STAMP_OPERATOR_SUB.startswith("system:")

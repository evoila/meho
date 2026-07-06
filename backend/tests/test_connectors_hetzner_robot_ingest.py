# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingest → resolve → dispatch tests for the hetzner-rest connector (#2079).

The ``hetzner-rest-2026.04`` connector shell registers empty
(``operation_count=0``); #2079 ships a MEHO-authored minimal OpenAPI spec
(``operations/ingest/specs/hetzner_robot_minimal.yaml``) so ingesting it
yields dispatchable operations that resolve to the hand-rolled
:class:`~meho_backplane.connectors.hetzner_robot.HetznerRobotConnector`.

The module proves the four ingest-side acceptance criteria of #2079:

* the shipped spec dry-run-parses with the same ``parse_openapi`` the live
  ingest uses (boot-safety net; malformed spec fails CI, not the first
  ``--spec`` ingest);
* ingesting the spec against ``product=hetzner version=2026.04
  impl=hetzner-rest`` yields ``operation_count > 0`` and reuses the
  hand-coded connector class (no GenericRestConnector shim shadows it);
* the ingested ops resolve to :class:`HetznerRobotConnector` through
  :func:`~meho_backplane.connectors.resolver.resolve_connector` rather than
  501-ing with ``no_connector``;
* an enabled read op returns non-empty mocked data end-to-end via
  :func:`~meho_backplane.operations.meta_tools.call_operation` (a respx test
  proving a successful, non-empty response — not ``connector_unsupported``).

The Vault-backed target + auth wiring is covered in
``tests/test_connectors_hetzner_robot_auth.py``; the curated read-only
dispatch smoke over the hand-seeded canary lives in
``tests/acceptance/test_g37_robot_dispatch_smoke.py``. This module closes
the gap between them: it drives the *spec-ingest* path end to end.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib.resources import files
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.hetzner_robot import (
    ROBOT_CONNECTOR_ID,
    ROBOT_IMPL_ID,
    ROBOT_PRODUCT,
    ROBOT_VERSION,
    HetznerRobotConnector,
    HetznerRobotTargetLike,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    parse_openapi,
    register_ingested_operations,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

#: The shipped minimal spec, loaded as package data (survives into the wheel).
_SPEC_RESOURCE = "hetzner_robot_minimal.yaml"

#: Tenant the ingest/dispatch tests act under.
_TENANT = UUID("00000000-0000-0000-0000-0000000000a9")

#: RFC 6761 ``.test.invalid`` host so no real network egress fires.
_ROBOT_HOST = "robot-ws.test.invalid"

#: op_ids the spec must yield — cross-checked against ROBOT_CORE_OPS so the
#: ingested rows and the curated read core agree on the same strings.
_EXPECTED_READ_OP_IDS = frozenset(
    {
        "GET:/server",
        "GET:/server/{server-ip}",
        "GET:/vswitch",
        "GET:/vswitch/{id}",
        "GET:/firewall/{server-ip}",
        "GET:/rdns",
    }
)

#: The write/admin ops the spec covers for the full wrapper surface.
_EXPECTED_WRITE_OP_IDS = frozenset(
    {
        "POST:/vswitch/{id}",
        "DELETE:/vswitch/{id}",
        "POST:/firewall/{server-ip}",
        "POST:/order/server_addon/transaction",
    }
)


def _read_shipped_spec() -> str:
    """Return the shipped minimal spec text from package data."""
    resource = files("meho_backplane.operations.ingest.specs").joinpath(_SPEC_RESOURCE)
    return resource.read_text(encoding="utf-8")


def _parse_shipped_spec() -> list[EndpointDescriptorProto]:
    """Parse the shipped spec via the same ``parse_openapi`` the live ingest uses."""
    operations: list[EndpointDescriptorProto] = parse_openapi(
        f"https://specs.example.test/{_SPEC_RESOURCE}",
        spec_source=f"spec:{_SPEC_RESOURCE}",
        content=_read_shipped_spec(),
    )
    return operations


def _make_operator() -> Operator:
    """Return an operator with a non-empty raw_jwt (a real, non-system caller)."""
    return Operator(
        sub="hetzner-ingest-test",
        name="Hetzner Ingest Test",
        email=None,
        raw_jwt="<hetzner-ingest-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors ``test_operations_register_ingested.py``'s autouse fixture so the
    ingest path can open the DB engine (``get_settings`` reads these).
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` standing in for the embedding service."""
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Re-register HetznerRobotConnector around each test.

    Sibling modules (``test_connectors_registry_v2.py``) install an autouse
    fixture that clears the registry between tests; re-register the hetzner
    class (versioned + wildcard, mirroring the package's own registration)
    before each test so the ingest guard finds the hand-coded class and the
    resolver can bind it.
    """
    clear_registry()
    register_connector_v2(
        product=HetznerRobotConnector.product,
        version=HetznerRobotConnector.version,
        impl_id=HetznerRobotConnector.impl_id,
        cls=HetznerRobotConnector,
    )
    register_connector_v2(product="hetzner", version="", impl_id="", cls=HetznerRobotConnector)
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# AC: the shipped spec parses (boot-safety net).
# ---------------------------------------------------------------------------


def test_shipped_spec_parses_and_covers_the_wrapper_surface() -> None:
    """The shipped minimal spec dry-run-parses and yields the expected ops.

    Exercises the same ``parse_openapi`` the live ingest and the boot-time
    shipped-artifact validator use, so a malformed spec fails this test
    rather than 500-ing the first ``--spec`` ingest. Asserts the read op_ids
    match the curated ROBOT_CORE_OPS strings and every AC-required surface
    (servers, vSwitch get + membership, firewall get/set, rDNS, server_addon
    order) is present.
    """
    rows = _parse_shipped_spec()
    op_ids = {r.op_id for r in rows}

    assert len(rows) > 0
    assert op_ids >= _EXPECTED_READ_OP_IDS, (
        f"missing read op_ids: {sorted(_EXPECTED_READ_OP_IDS - op_ids)}"
    )
    assert op_ids >= _EXPECTED_WRITE_OP_IDS, (
        f"missing write op_ids: {sorted(_EXPECTED_WRITE_OP_IDS - op_ids)}"
    )
    # Every op declares an operationId → a stable, non-empty op_id.
    for row in rows:
        method, _, path = row.op_id.partition(":")
        assert method and path.startswith("/"), f"malformed op_id {row.op_id!r}"


# ---------------------------------------------------------------------------
# AC: ingesting the spec yields operation_count > 0 (and reuses the class).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_shipped_spec_yields_operations(
    stub_embedding_service: AsyncMock,
) -> None:
    """Ingesting the spec against the hetzner triple yields operation_count > 0.

    Drives the programmatic ingest entry point
    (:func:`register_ingested_operations`) the CLI's ``--spec`` path also
    calls, then asserts the endpoint_descriptor rows land under
    ``product=hetzner version=2026.04 impl_id=hetzner-rest`` — the connector
    shell is no longer at ``operation_count=0``.

    ``connector_registered`` MUST be ``False``: the ingest guard defers to
    the hand-coded :class:`HetznerRobotConnector` already registered for the
    triple rather than scaffolding a shadowing GenericRestConnector shim
    (the product-identity footgun #1798 guards against).
    """
    operations = _parse_shipped_spec()

    result = await register_ingested_operations(
        product=ROBOT_PRODUCT,
        version=ROBOT_VERSION,
        impl_id=ROBOT_IMPL_ID,
        spec_source=_SPEC_RESOURCE,
        operations=operations,
        embedding_service=stub_embedding_service,
    )

    assert result.inserted_count == len(operations) > 0
    assert result.connector_registered is False, (
        "ingest must reuse the hand-coded HetznerRobotConnector, not register a "
        "GenericRestConnector shim that shadows it"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.product == ROBOT_PRODUCT,
                        EndpointDescriptor.version == ROBOT_VERSION,
                        EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
                    )
                )
            )
            .scalars()
            .all()
        )

    # operation_count > 0 for hetzner-rest-2026.04.
    assert len(rows) == len(operations) > 0
    op_ids = {row.op_id for row in rows}
    assert op_ids >= _EXPECTED_READ_OP_IDS
    for row in rows:
        assert row.source_kind == "ingested"
        assert f"spec:{_SPEC_RESOURCE}" in row.tags


# ---------------------------------------------------------------------------
# AC: the ingested ops resolve to HetznerRobotConnector (not no_connector).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingested_ops_resolve_to_hetzner_connector(
    stub_embedding_service: AsyncMock,
) -> None:
    """A hetzner target resolves the ingested ops to the hand-coded connector.

    After ingesting the spec, a :class:`Target` with ``product="hetzner"``
    resolves to :class:`HetznerRobotConnector` (registered under
    ``product="hetzner"``) — the ingested descriptors reach a dispatchable
    connector rather than 501-ing with ``no_connector``. Guards the
    string-derived-product-identity footgun: the resolver binds the real
    connector, not a divergent-product shim.
    """
    await register_ingested_operations(
        product=ROBOT_PRODUCT,
        version=ROBOT_VERSION,
        impl_id=ROBOT_IMPL_ID,
        spec_source=_SPEC_RESOURCE,
        operations=_parse_shipped_spec(),
        embedding_service=stub_embedding_service,
    )

    class _FakeFingerprint:
        version = ROBOT_VERSION

    class _FakeTarget:
        product = "hetzner"
        fingerprint = _FakeFingerprint()
        preferred_impl_id = None
        version = ROBOT_VERSION

    resolved = resolve_connector(_FakeTarget())
    assert resolved is HetznerRobotConnector

    # The triple is dispatchable: the ingested rows key on the same triple
    # the registered class advertises.
    assert (ROBOT_PRODUCT, ROBOT_VERSION, ROBOT_IMPL_ID) in all_connectors_v2()
    assert all_connectors_v2()[(ROBOT_PRODUCT, ROBOT_VERSION, ROBOT_IMPL_ID)] is (
        HetznerRobotConnector
    )


# ---------------------------------------------------------------------------
# AC: an ingested read op returns non-empty data end-to-end via call_operation.
# ---------------------------------------------------------------------------


async def _stub_loader(_target: HetznerRobotTargetLike, _operator: Operator) -> dict[str, str]:
    """Injected loader — bypasses the live operator-context Vault read in test."""
    return {"username": "webservice-user", "password": "stub-password"}


@pytest.mark.asyncio
async def test_ingested_read_op_returns_non_empty_via_call_operation(
    stub_embedding_service: AsyncMock,
) -> None:
    """An ingested read op returns non-empty mocked data through ``call_operation``.

    Ingests the spec, enables the ``GET:/server`` descriptor, seeds a
    hetzner :class:`Target`, then dispatches the op via the agent-facing
    :func:`call_operation` meta-tool against a respx-mocked Robot
    Webservice. Asserts ``status='ok'`` with a non-empty result — proving
    the ingested op reaches :class:`HetznerRobotConnector`'s HTTP transport
    and executes, not ``connector_unsupported`` / ``no_connector``.

    Runs against the autouse-migrated SQLite engine (same as the argocd /
    nsx / keycloak E2E dispatch tests); the connector's httpx client is
    routed to a respx-mocked Webservice, so no real network egress fires.
    """
    operator = _make_operator()
    reset_dispatcher_caches()

    await register_ingested_operations(
        product=ROBOT_PRODUCT,
        version=ROBOT_VERSION,
        impl_id=ROBOT_IMPL_ID,
        spec_source=_SPEC_RESOURCE,
        operations=_parse_shipped_spec(),
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # The ingest default is is_enabled=False; enable the read op so
        # lookup_descriptor (is_enabled-filtered) finds it for dispatch.
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.product == ROBOT_PRODUCT,
                EndpointDescriptor.version == ROBOT_VERSION,
                EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
                EndpointDescriptor.op_id == "GET:/server",
            )
            .values(is_enabled=True)
        )
        target = Target(
            tenant_id=_TENANT,
            name="robot-ingest",
            aliases=[],
            product="hetzner",
            host=_ROBOT_HOST,
            port=443,
            fqdn=None,
            secret_ref="hetzner/robot-ingest",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"vendor": "hetzner", "product": "robot-webservice", "reachable": True},
            notes="seeded by test_connectors_hetzner_robot_ingest",
        )
        session.add(target)
        await session.commit()

    # Preseed the dispatcher's connector-instance cache with a stub-loader
    # instance so no live Vault read fires (mirrors the argocd/nsx E2E tests).
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    instance = HetznerRobotConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[HetznerRobotConnector] = instance

    servers = [
        {"server": {"server_ip": "1.2.3.1", "server_number": 100001, "dc": "FSN1-DC14"}},
        {"server": {"server_ip": "1.2.3.2", "server_number": 100002, "dc": "FSN1-DC15"}},
    ]

    try:
        async with respx.mock(
            base_url=f"https://{_ROBOT_HOST}",
            assert_all_called=False,
            assert_all_mocked=False,
        ) as mock:
            mock.get("/server").respond(200, json=servers)
            result = await call_operation(
                operator,
                {
                    "connector_id": ROBOT_CONNECTOR_ID,
                    "op_id": "GET:/server",
                    "target": {"name": "robot-ingest"},
                    "params": {},
                },
            )
    finally:
        await instance.aclose()
        reset_dispatcher_caches()

    assert result["status"] == "ok", (
        f"ingested GET:/server did not dispatch cleanly: "
        f"{result.get('error')!r}; full result={result!r}"
    )
    # Non-empty successful response — not connector_unsupported / no_connector.
    payload = result.get("result")
    assert payload, f"expected a non-empty server list; got result={payload!r}"
    assert len(payload) == len(servers)

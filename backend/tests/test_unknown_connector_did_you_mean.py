# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unknown connector ids get a "did you mean" answer (#4136).

Small models often guess a connector id that is close but wrong, for
example ``vmware-rest`` for ``vmware-rest-9.0`` or ``linux-ssh`` for
``linux-ssh-1.x``. Every unknown-connector error now names up to 5 close
ids the caller can see, so the model can fix its call at once:

* ``search_operations`` / ``list_operation_groups`` answer ``-32602`` with
  ``data.did_you_mean``.
* ``list_targets`` with a wrong id answers the same error instead of an
  empty list. A known product with no targets still gets an empty list.
* ``call_operation`` / ``preview_operation`` answer ``unknown_connector``
  with ``extras.did_you_mean``, not ``unknown_op``.

A connector of another tenant is never named. Rows are real rows in the
autouse-migrated SQLite database, so the tenant rule runs as real SQL.
A built-in connector class with no rows yet is named: a hand-coded
class, or the profiled class that startup stamps for a catalog row
shipped with a profile. The class that ingest registers is not named on
its own: a tenant-scoped ingest registers one too, so it counts only
through its tenant rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector, shim_kind
from meho_backplane.connectors.profile import ExecutionProfile
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    deregister_connector_v2,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.operations import _lookup, dispatch
from meho_backplane.operations._lookup import close_connector_ids, connector_product_known
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.ingest import catalog as catalog_module
from meho_backplane.operations.ingest import ensure_connector_class_registered
from meho_backplane.operations.ingest.boot_stamp import stamp_catalog_profiled_connectors
from meho_backplane.operations.ingest.catalog import (
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_profile_resource,
)
from meho_backplane.operations.ingest.connector_registration import synthesise_profiled_class
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000b0b0")
_OP = "GET:/api/vcenter/vm"

#: A made-up connector no shipped package registers, so the registry
#: tests do not collide with the real connectors the app imports.
_ACME = ("acme", "2.0", "acme-rest")


class _AcmeConnector(Connector):
    """Hand-coded class for ``acme-rest-2.0``; its ``shim_kind`` is ``"none"``."""

    product = "acme"
    version = "2.0"
    impl_id = "acme-rest"

    async def fingerprint(self, target, operator=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def probe(self, target):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def execute(self, target, op_id, params):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _only_acme_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the shipped connector classes from the lookup; show only the made-up one.

    The app imports every shipped connector into the process-global
    registry. Hand-coded classes are named as built-in connectors, so
    with the real registry a close spelling of a seeded id could also
    match a shipped id and the exact lists below would change whenever
    a connector is added. The made-up connector is still seen when a
    test registers it.
    """
    real = all_connectors_v2

    def only_acme() -> dict[tuple[str, str, str], type[Connector]]:
        return {key: cls for key, cls in real().items() if key == _ACME}

    monkeypatch.setattr(_lookup, "all_connectors_v2", only_acme)


@pytest.fixture
def _acme_registry_cleanup() -> Iterator[None]:
    """Remove the made-up connector from the process-global registry after the test."""
    try:
        yield
    finally:
        deregister_connector_v2(product=_ACME[0], version=_ACME[1], impl_id=_ACME[2])


def _register_acme_as_tenant_ingest_does() -> None:
    """Register the bare auto-shim that an ingest, also a tenant-scoped one, registers."""
    assert ensure_connector_class_registered(
        product=_ACME[0], version=_ACME[1], impl_id=_ACME[2], base_url=None
    )


#: The shipped fixture profile, reused for the made-up connector.
_PROFILE_RESOURCE = "_fixture_minimal.yaml"


def _acme_shipped_profile_catalog() -> ConnectorSpecCatalog:
    """A catalog with one profile-backed row for the made-up connector."""
    return ConnectorSpecCatalog(
        entries=(
            ConnectorSpecEntry.model_validate(
                {
                    "product": _ACME[0],
                    "version": _ACME[1],
                    "impl_id": _ACME[2],
                    "requires_connector_class": "ProfiledRestConnector_acme_2_0",
                    "upstream": None,
                    "spec_resource": _PROFILE_RESOURCE,
                    "profile_resource": _PROFILE_RESOURCE,
                }
            ),
        )
    )


async def _stamp_acme_as_startup_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the startup stamp over a catalog that ships a profile for the made-up connector."""
    catalog = _acme_shipped_profile_catalog()
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: catalog)
    assert await stamp_catalog_profiled_connectors(catalog) == 1
    assert shim_kind(all_connectors_v2()[_ACME]) == "profiled"


def _stamp_acme_as_tenant_profile_stamp_does() -> None:
    """Register the profiled class a tenant-scoped profile stamp registers.

    Same class shape as the startup stamp, but the shipped catalog has no
    profile row for the triple, so it is not built-in.
    """
    profile = ExecutionProfile.model_validate(
        yaml.safe_load(load_profile_resource(_PROFILE_RESOURCE))
    )
    cls = synthesise_profiled_class(
        product=_ACME[0], version=_ACME[1], impl_id=_ACME[2], profile=profile
    )
    register_connector_v2(product=_ACME[0], version=_ACME[1], impl_id=_ACME[2], cls=cls)


async def _seed_connector(
    product: str, version: str, impl_id: str, *, tenant_id: UUID | None = None
) -> None:
    """Insert one enabled descriptor row, which makes the connector visible."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=_OP,
                source_kind="ingested",
                method="GET",
                path="/api/vcenter/vm",
                handler_ref=None,
                summary="fixture",
                description="fixture",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


async def _seed_default_connectors() -> None:
    await _seed_connector("vmware", "9.0", "vmware-rest")
    await _seed_connector("vmware", "8.0", "vmware-rest")
    await _seed_connector("linux", "1.x", "linux-ssh")


async def _seed_target(name: str, product: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="op-tenant"))
        session.add(
            TargetORM(
                tenant_id=OPERATOR_TENANT_ID,
                name=name,
                aliases=[],
                product=product,
                host=f"{name}.example",
                port=443,
                secret_ref=f"targets/{name}",
                auth_model="shared_service_account",
            )
        )


def _tools_call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    body: dict[str, Any] = response.json()
    return body


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="did-you-mean",
        name="Did You Mean Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# close_connector_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_and_close_spelling_are_found() -> None:
    """A missing version and a wrong version both find the real id."""
    await _seed_default_connectors()

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="linux-ssh") == [
        "linux-ssh-1.x"
    ]
    assert await close_connector_ids(
        tenant_id=OPERATOR_TENANT_ID, connector_id="linux-ssh-1.0"
    ) == ["linux-ssh-1.x"]
    assert set(
        await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="VMware-Rest")
    ) == {"vmware-rest-9.0", "vmware-rest-8.0"}


@pytest.mark.asyncio
async def test_no_close_id_returns_empty_list() -> None:
    await _seed_default_connectors()

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="zzz-9") == []
    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="  ") == []


@pytest.mark.asyncio
async def test_at_most_five_ids_are_named() -> None:
    for minor in range(7):
        await _seed_connector("vmware", f"9.{minor}", "vmware-rest")

    found = await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="vmware-rest")

    assert len(found) == 5


@pytest.mark.asyncio
async def test_another_tenants_connector_is_never_named() -> None:
    """A connector private to another tenant is not named; the owner sees it."""
    await _seed_connector("vmware", "9.0", "vmware-rest", tenant_id=_OTHER_TENANT)

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="vmware-rest") == []
    assert await close_connector_ids(tenant_id=_OTHER_TENANT, connector_id="vmware-rest") == [
        "vmware-rest-9.0"
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_builtin_class_without_rows_is_named() -> None:
    """A hand-coded connector class is built-in, so it is named before its rows exist."""
    register_connector_v2(product=_ACME[0], version=_ACME[1], impl_id=_ACME[2], cls=_AcmeConnector)

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="acme-rest") == [
        "acme-rest-2.0"
    ]
    assert await connector_product_known(tenant_id=OPERATOR_TENANT_ID, product="acme")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_shipped_profile_stamped_at_startup_is_named_before_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profiled class stamped at startup from a shipped profile is built-in.

    Before its first ingest it has no operation rows, yet its id is a
    real id, so it is named and its product is known.
    """
    await _stamp_acme_as_startup_does(monkeypatch)

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="acme-rest") == [
        "acme-rest-2.0"
    ]
    assert await connector_product_known(tenant_id=OPERATOR_TENANT_ID, product="acme")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_tenant_profile_stamp_is_not_named_to_another_tenant() -> None:
    """A profiled class for a triple the catalog ships no profile for counts only through rows."""
    _stamp_acme_as_tenant_profile_stamp_does()
    await _seed_connector(*_ACME, tenant_id=_OTHER_TENANT)

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="acme-rest") == []
    assert not await connector_product_known(tenant_id=OPERATOR_TENANT_ID, product="acme")
    assert await close_connector_ids(tenant_id=_OTHER_TENANT, connector_id="acme-rest") == [
        "acme-rest-2.0"
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_tenant_ingest_registration_is_not_named_to_another_tenant() -> None:
    """The class a tenant-scoped ingest registers counts only through the tenant's rows."""
    _register_acme_as_tenant_ingest_does()
    await _seed_connector(*_ACME, tenant_id=_OTHER_TENANT)

    assert await close_connector_ids(tenant_id=OPERATOR_TENANT_ID, connector_id="acme-rest") == []
    assert not await connector_product_known(tenant_id=OPERATOR_TENANT_ID, product="acme")
    assert await close_connector_ids(tenant_id=_OTHER_TENANT, connector_id="acme-rest") == [
        "acme-rest-2.0"
    ]
    assert await connector_product_known(tenant_id=_OTHER_TENANT, product="acme")


# ---------------------------------------------------------------------------
# search_operations / list_operation_groups over MCP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_search_operations_unknown_connector_names_close_ids(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_default_connectors()

    body = _tools_call(
        client, "search_operations", {"connector_id": "vmware-rest", "query": "list vms"}
    )

    assert body["error"]["code"] == INVALID_PARAMS
    data = body["error"]["data"]
    assert data["reason"] == "unknown_connector"
    assert data["connector_id"] == "vmware-rest"
    assert "vmware-rest-9.0" in data["did_you_mean"]
    assert "Did you mean:" in body["error"]["message"]
    assert "vmware-rest-9.0" in body["error"]["message"]
    assert "bug report" not in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_operation_groups_without_close_id_points_at_connector_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_default_connectors()

    body = _tools_call(client, "list_operation_groups", {"connector_id": "zzz-9"})

    assert body["error"]["code"] == INVALID_PARAMS
    assert body["error"]["data"]["did_you_mean"] == []
    assert "meho_connector_list" in body["error"]["message"]


# ---------------------------------------------------------------------------
# list_targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_unknown_connector_is_an_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``vmware-rest`` has product ``vmware-rest``; no connector has it."""
    client, _op = client_with_operator
    await _seed_default_connectors()
    await _seed_target("vc-1", "vmware")

    body = _tools_call(client, "list_targets", {"connector_id": "vmware-rest"})

    assert body["error"]["code"] == INVALID_PARAMS
    data = body["error"]["data"]
    assert data["reason"] == "unknown_connector"
    assert data["connector_id"] == "vmware-rest"
    assert "vmware-rest-9.0" in data["did_you_mean"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_known_connector_still_returns_targets(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_default_connectors()
    await _seed_target("vc-1", "vmware")

    body = _tools_call(client, "list_targets", {"connector_id": "vmware-rest-9.0"})

    payload = json.loads(body["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["vc-1"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_known_product_without_targets_is_empty(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A real connector with no targets of its product is an empty list, not an error."""
    client, _op = client_with_operator
    await _seed_default_connectors()
    await _seed_target("vc-1", "vmware")

    body = _tools_call(client, "list_targets", {"connector_id": "linux-ssh-1.x"})

    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["targets"] == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_list_targets_shipped_profile_without_rows_or_targets_is_empty(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector stamped at startup, not yet ingested, with no targets is an empty list."""
    client, _op = client_with_operator
    await _stamp_acme_as_startup_does(monkeypatch)
    await _seed_target("vc-1", "vmware")

    body = _tools_call(client, "list_targets", {"connector_id": "acme-rest-2.0"})

    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["targets"] == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.usefixtures("_acme_registry_cleanup")
async def test_list_targets_another_tenants_ingested_connector_is_unknown(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A connector ingested by another tenant is unknown, not an empty list."""
    client, _op = client_with_operator
    _register_acme_as_tenant_ingest_does()
    await _seed_connector(*_ACME, tenant_id=_OTHER_TENANT)
    await _seed_target("vc-1", "vmware")

    body = _tools_call(client, "list_targets", {"connector_id": "acme-rest-2.0"})

    assert body["error"]["code"] == INVALID_PARAMS
    data = body["error"]["data"]
    assert data["reason"] == "unknown_connector"
    assert "acme-rest-2.0" not in data["did_you_mean"]


# ---------------------------------------------------------------------------
# call_operation / preview_operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_unknown_connector_names_close_ids() -> None:
    """``linux-ssh`` is an unknown connector, not an unknown operation."""
    await _seed_default_connectors()

    result = await dispatch(
        operator=_operator(OPERATOR_TENANT_ID),
        connector_id="linux-ssh",
        op_id="linux.uptime",
        target=None,
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "unknown_connector"
    assert result.extras["did_you_mean"] == ["linux-ssh-1.x"]
    assert result.error is not None
    assert result.error.startswith("unknown_connector:")
    assert "Did you mean: linux-ssh-1.x?" in result.error


@pytest.mark.asyncio
async def test_preview_operation_unknown_connector_names_close_ids() -> None:
    await _seed_default_connectors()

    envelope = await preview_dispatch(
        operator=_operator(OPERATOR_TENANT_ID),
        connector_id="vmware-rest-9",
        op_id=_OP,
        target=None,
        params={},
    )

    assert envelope["extras"]["error_code"] == "unknown_connector"
    assert envelope["extras"]["did_you_mean"][0] == "vmware-rest-9.0"


@pytest.mark.asyncio
async def test_call_operation_never_names_another_tenants_connector() -> None:
    await _seed_connector("linux", "1.x", "linux-ssh", tenant_id=_OTHER_TENANT)

    result = await dispatch(
        operator=_operator(OPERATOR_TENANT_ID),
        connector_id="linux-ssh",
        op_id="linux.uptime",
        target=None,
        params={},
    )

    assert result.extras["error_code"] == "unknown_connector"
    assert result.extras["did_you_mean"] == []
    assert result.error is not None
    assert "linux-ssh-1.x" not in result.error

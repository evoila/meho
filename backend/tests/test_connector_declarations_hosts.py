# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host, directory and database connectors declare what a target needs.

Covers the folders ``rke2``, ``bind9``, ``pfsense``, ``freeipa``, ``msad``,
``winsrv``, ``windows_dns``, ``wsfc``, ``hyperv``, ``mongodb``, ``mssql`` and
``postgres`` (the Linux SSH connector has its own test):

* every connector class in these folders declares its login ways, so a new
  class with no declaration fails here;
* the two SSH ways, the several-lines key field and the key-only pfSense way;
* the start check accepts all declarations against the real operation rows,
  and ``GET /api/v1/connectors/target-form`` answers for every product.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.connectors_target_form import router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import Connector
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.bind9.connector import Bind9Connector
from meho_backplane.connectors.pfsense.connector import PfSenseConnector
from meho_backplane.connectors.rke2.connector import Rke2SshConnector
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import typed_register
from meho_backplane.settings import get_settings

_FOLDERS = (
    "rke2",
    "bind9",
    "pfsense",
    "freeipa",
    "msad",
    "winsrv",
    "windows_dns",
    "wsfc",
    "hyperv",
    "mongodb",
    "mssql",
    "postgres",
)

#: folder -> (default port, test read, way keys). The ports and the test reads
#: are what the code dials and what the code registers today.
_EXPECTED: dict[str, tuple[int, str, tuple[str, ...]]] = {
    "rke2": (22, "rke2.node.service.status", ("key", "password")),
    "bind9": (22, "bind9.zone.list", ("key", "password")),
    "pfsense": (22, "pfsense.version", ("key",)),
    "freeipa": (443, "freeipa.about", ("password",)),
    "msad": (22, "msad.domain.info", ("key", "password")),
    "winsrv": (22, "winsrv.system.os-info", ("key", "password")),
    "windows_dns": (22, "windns.zone.list", ("key", "password")),
    "wsfc": (22, "wsfc.cluster.get", ("key", "password")),
    "hyperv": (22, "hyperv.host.info", ("key", "password")),
    "mongodb": (27017, "mongodb.databases", ("password",)),
    "mssql": (1433, "mssql.instance.version", ("password",)),
    "postgres": (5432, "postgres.databases", ("password",)),
}


def _classes_of(folder: str) -> list[type[Connector]]:
    module = importlib.import_module(f"meho_backplane.connectors.{folder}.connector")
    return [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, Connector) and cls.__module__ == module.__name__
    ]


_ALL = [(folder, cls) for folder in _FOLDERS for cls in _classes_of(folder)]


def test_every_folder_has_one_connector_class() -> None:
    assert sorted({folder for folder, _ in _ALL}) == sorted(_FOLDERS)
    assert len(_ALL) == len(_FOLDERS)


@pytest.mark.parametrize(("folder", "cls"), _ALL, ids=[c.__name__ for _, c in _ALL])
def test_every_class_declares_its_target_form(folder: str, cls: type[Connector]) -> None:
    port, test_read, way_keys = _EXPECTED[folder]
    assert cls.credential_ways is not None, f"{cls.__name__} has no credential_ways"
    assert cls.target_fields == ()
    assert cls.default_port == port
    assert cls.test_operation == test_read
    assert tuple(way.key for way in cls.credential_ways) == way_keys
    for way in cls.credential_ways:
        assert way.fields, f"{cls.__name__} way {way.key} has no fields"


@pytest.mark.parametrize("cls", [Rke2SshConnector, Bind9Connector], ids=["rke2", "bind9"])
def test_two_ssh_ways_with_a_multiline_key(cls: type[SshConnector]) -> None:
    assert cls.credential_ways is not None
    ways = {way.key: way for way in cls.credential_ways}
    assert set(ways) == {"key", "password"}
    key_fields = {f.name: f for f in ways["key"].fields}
    assert key_fields["ssh_private_key"].multiline is True
    assert key_fields["ssh_private_key"].required is True
    assert key_fields["known_hosts"].multiline is True
    assert key_fields["username"].required is False
    assert [f.name for f in ways["password"].fields] == ["username", "password", "known_hosts"]
    # The sudo password is read by the write operations of these two classes.
    assert key_fields["sudo_password"].required is False
    assert key_fields["sudo_password"].hidden is True


def test_pfsense_has_a_key_way_only() -> None:
    assert PfSenseConnector.credential_ways is not None
    (way,) = PfSenseConnector.credential_ways
    by_name = {f.name: f for f in way.fields}
    assert set(by_name) == {"username", "ssh_private_key", "known_hosts"}
    assert by_name["ssh_private_key"].multiline is True


def test_the_shared_ssh_class_declares_nothing() -> None:
    assert SshConnector.credential_ways is None
    assert SshConnector.test_operation is None


# ---------------------------------------------------------------------------
# The start check and the route, on the real operation rows
# ---------------------------------------------------------------------------


@pytest.fixture
def _real_start_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.mark.asyncio
async def test_the_start_check_and_the_route_accept_every_declaration(
    _real_start_env: None,
) -> None:
    async with typed_register.operation_catalog_write(embedding_service=_embedding_service()):
        pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_jwt_and_bind] = lambda: Operator(
        sub="op", raw_jwt="test-jwt", tenant_id=uuid.uuid4(), tenant_role=TenantRole.OPERATOR
    )
    client = TestClient(app)
    products = {
        "rke2": "rke2",
        "bind9": "bind9",
        "pfsense": "pfsense",
        "freeipa": "freeipa",
        "msad": "msad",
        "winsrv": "winsrv",
        "windows_dns": "windns",
        "wsfc": "wsfc",
        "hyperv": "hyperv",
        "mongodb": "mongodb",
        "mssql": "mssql",
        "postgres": "postgres",
    }
    for folder, product in products.items():
        port, test_read, way_keys = _EXPECTED[folder]
        resp = client.get("/api/v1/connectors/target-form", params={"product": product})
        assert resp.status_code == 200, (product, resp.text)
        answer: dict[str, Any] = resp.json()["classes"][0]
        assert answer["default_port"] == port
        assert answer["target_fields"] == []
        assert answer["test_operation"] == test_read
        assert tuple(w["key"] for w in answer["credential_ways"]) == way_keys

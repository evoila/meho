# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration smoke test for :class:`MongoDbConnector` against a real MongoDB.

Boots a stock ``mongo`` container (root auth enabled) and exercises the
connector's :meth:`fingerprint`, curated read ops, and the credentialled auth
path end-to-end over the real pymongo wire path:

* ``fingerprint`` returns ``version`` / wire-version / storage engine / auth
  mode against a live standalone (Task #2237 AC).
* ``mongodb.databases`` / ``mongodb.collections`` see a seeded database +
  collection.
* ``mongodb.indexes`` surfaces a TTL index's ``expireAfterSeconds`` (Task #2237
  AC).
* ``mongodb.replica_status`` reports ``is_replica_set=False`` on a standalone
  (the ``replSetGetStatus`` code-76 carve-out) -- member roles on a real
  replica set are covered by the recorded-fixture unit test, per the AC's "or
  recorded fixture" allowance.

The credential loader is exercised through the in-process Vault fake (there is
no real Vault in this lane), which returns the container's own root credentials
so the connector's operator-context read path runs for real.

Skip conditions mirror ``tests/integration/conftest.py``: no Docker socket ->
skip (agent sandbox); CI provisions Docker so the test runs there. A Docker Hub
pull rate-limit is converted to a skip so the suite's pass/fail signal stays
meaningful.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.mongodb import MongoDbConnector
from meho_backplane.settings import get_settings

from .._vault_fakes import install_fake_client


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


@dataclass
class _MongoLiveTarget:
    """Target double carrying the container's connection coordinates.

    ``secret_ref`` is set so the connector resolves credentials through the
    operator-context loader (fed by the Vault fake); the integration-double
    ``id`` / ``tenant_id`` / ``product`` / ``version`` fields stay present so
    nothing downstream trips on a missing attribute (the integration-double
    trap).
    """

    host: str
    port: int
    secret_ref: str = "targets/mongo-live"
    name: str = "mongo-live"
    id: str = "00000000-0000-0000-0000-0000000000f0"
    tenant_id: str = "00000000-0000-0000-0000-000000000000"
    product: str = "mongodb"
    version: str | None = "7"
    extras: dict[str, object] = field(default_factory=dict)


def _operator() -> Operator:
    return Operator(
        sub="op-mongo-live",
        name="Mongo Live Operator",
        email=None,
        raw_jwt="op.mongo.live.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000f4"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Container:
    host: str
    port: int
    user: str
    password: str


@pytest.fixture(scope="module")
def mongo_container() -> Iterator[_Container]:
    """Boot a stock Mongo (root auth), seed a collection + TTL index, yield coords."""
    from docker.errors import APIError as _DockerAPIError
    from testcontainers.mongodb import MongoDbContainer

    image = os.environ.get("MEHO_TEST_MONGODB_IMAGE", "mongo:7")
    mongo = MongoDbContainer(image)
    try:
        mongo.start()
    except _DockerAPIError as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "too many requests" in msg or "429" in msg:
            pytest.skip(f"Docker Hub pull rate-limited for {image!r}; set MEHO_TEST_MONGODB_IMAGE")
        raise
    try:
        coords = _Container(
            host=mongo.get_container_host_ip(),
            port=int(mongo.get_exposed_port(27017)),
            user=mongo.username,
            password=mongo.password,
        )
        _seed(coords)
        yield coords
    finally:
        mongo.stop()


def _seed(c: _Container) -> None:
    """Create a database + collection with a TTL index so listIndexes has one."""
    from pymongo import MongoClient

    client: MongoClient[dict[str, object]] = MongoClient(
        host=c.host, port=c.port, username=c.user, password=c.password, authSource="admin"
    )
    try:
        events = client.get_database("app").get_collection("events")
        events.insert_many([{"n": i} for i in range(10)])
        events.create_index("createdAt", expireAfterSeconds=3600, name="ttl_idx")
    finally:
        client.close()


@pytest.fixture
def _vault(monkeypatch: pytest.MonkeyPatch, mongo_container: _Container) -> None:
    """Route the credential loader at the container's own root credentials."""
    get_settings.cache_clear()
    install_fake_client(
        monkeypatch,
        secret={"username": mongo_container.user, "password": mongo_container.password},
    )


@pytest.mark.asyncio
async def test_fingerprint_returns_identity_fields(
    mongo_container: _Container, _vault: None
) -> None:
    """AC: fingerprint returns version / wire version / storage engine / auth mode."""
    target = _MongoLiveTarget(host=mongo_container.host, port=mongo_container.port)
    fp = await MongoDbConnector().fingerprint(target, _operator())

    assert fp.reachable is True, fp.extras
    assert fp.vendor == "mongodb"
    assert fp.version is not None and fp.version.startswith("7")
    assert fp.edition in {"community", "enterprise"}
    assert isinstance(fp.extras["max_wire_version"], int)
    assert fp.extras["auth_mode"] == "scram"
    assert fp.extras["storage_engine"]  # e.g. "wiredTiger"


@pytest.mark.asyncio
async def test_databases_and_collections(mongo_container: _Container, _vault: None) -> None:
    """AC: a curated op dispatches; the seeded database + collection are visible."""
    target = _MongoLiveTarget(host=mongo_container.host, port=mongo_container.port)
    connector = MongoDbConnector()

    dbs = await connector.list_databases(_operator(), target, {})
    assert any(d["name"] == "app" for d in dbs["databases"])

    colls = await connector.list_collections(_operator(), target, {"database": "app"})
    assert any(c["name"] == "events" for c in colls["collections"])


@pytest.mark.asyncio
async def test_indexes_surface_ttl_expire_after_seconds(
    mongo_container: _Container, _vault: None
) -> None:
    """AC: getIndexes surfaces TTL expireAfterSeconds against a live Mongo."""
    target = _MongoLiveTarget(host=mongo_container.host, port=mongo_container.port)
    result = await MongoDbConnector().list_indexes(
        _operator(), target, {"database": "app", "collection": "events"}
    )
    ttl = next((i for i in result["indexes"] if i["name"] == "ttl_idx"), None)
    assert ttl is not None, result["indexes"]
    assert ttl["expireAfterSeconds"] == 3600


@pytest.mark.asyncio
async def test_replica_status_standalone(mongo_container: _Container, _vault: None) -> None:
    """AC: replica_status reports is_replica_set=False on a standalone (code-76 carve-out)."""
    target = _MongoLiveTarget(host=mongo_container.host, port=mongo_container.port)
    result = await MongoDbConnector().replica_status(_operator(), target, {})
    assert result["is_replica_set"] is False
    assert result["repl_set_status"] is None

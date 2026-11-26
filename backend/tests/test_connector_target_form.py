# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a connector declares for its target form, and the route that returns it.

Covers:

* a class that declares nothing passes the check and answers with nulls;
* the shape check of a declaration (blank name, duplicate name, duplicate way
  key, and the other broken cases);
* a declaration built only from ``meho_connector_kit`` passes;
* ``GET /api/v1/connectors/target-form``: the answers for Harbor and Linux,
  404 for an unknown product, 403 for the read-only role, two classes of one
  product, 404 for a connector another tenant imported, and no secret store
  call.

The check of ``test_operation`` at start is in
``test_connector_declaration_start.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.connectors_target_form import router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.connectors import Connector
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.linux.connector import LinuxSshConnector
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    CredentialField,
    CredentialWay,
    TargetField,
    check_connector_declaration,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_KEY = make_rsa_keypair("kid-target-form")
_PATH = "/api/v1/connectors/target-form"


class _Base(Connector):
    product = "formfake"
    version = "1.x"
    impl_id = "formfake-rest"

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


def _make(**attrs: Any) -> type[Connector]:
    return type("FakeConnector", (_Base,), attrs)


def _way(*names: str, key: str = "basic") -> CredentialWay:
    return CredentialWay(
        key=key, label="Basic", fields=tuple(CredentialField(name=n, label=n) for n in names)
    )


# ---------------------------------------------------------------------------
# The base class and the shape check
# ---------------------------------------------------------------------------


def test_a_class_that_declares_nothing_is_not_declared() -> None:
    cls = _make()
    assert cls.default_port is None
    assert cls.target_fields is None
    assert cls.credential_ways is None
    assert cls.test_operation is None
    check_connector_declaration(cls)


def test_a_good_declaration_passes() -> None:
    cls = _make(
        default_port=443,
        target_fields=(
            TargetField(key="realm", label="Realm"),
            TargetField(key="mode", label="Mode", kind="choice", choices=("a", "b")),
        ),
        credential_ways=(_way("username", "password"), _way("token", key="token")),
    )
    check_connector_declaration(cls)


def test_no_credential_needed_is_an_empty_tuple_and_passes() -> None:
    cls = _make(credential_ways=())
    assert cls.credential_ways == ()
    check_connector_declaration(cls)


@pytest.mark.parametrize(
    ("attrs", "needle"),
    [
        pytest.param(
            {"credential_ways": (_way("username", " "),)}, "blank name", id="blank-field-name"
        ),
        pytest.param(
            {"credential_ways": (_way("username", "x" * 201),)},
            "longer than 200",
            id="field-name-too-long",
        ),
        pytest.param(
            {"credential_ways": (_way("username", "username"),)},
            "'username' twice",
            id="duplicate-field-name",
        ),
        pytest.param(
            {"credential_ways": (_way("a", key="k"), _way("b", key="k"))},
            "credential way key 'k' is twice",
            id="duplicate-way-key",
        ),
        pytest.param({"credential_ways": (_way("a", key=""),)}, "blank key", id="blank-way-key"),
        pytest.param(
            {"target_fields": (TargetField(key="", label="x"),)}, "blank key", id="blank-field-key"
        ),
        pytest.param(
            {"target_fields": (TargetField(key="a", label="x"), TargetField(key="a", label="y"))},
            "'a' is twice",
            id="duplicate-field-key",
        ),
        pytest.param(
            {"target_fields": (TargetField(key="a", label="x", kind="choice"),)},
            "lists no choices",
            id="choice-without-choices",
        ),
        pytest.param({"default_port": 70000}, "between 1 and 65535", id="port-out-of-range"),
        pytest.param({"test_operation": " "}, "test_operation is blank", id="blank-test-op"),
    ],
)
def test_a_broken_declaration_names_the_connector(attrs: dict[str, Any], needle: str) -> None:
    with pytest.raises(ConnectorDeclarationError, match="FakeConnector") as excinfo:
        check_connector_declaration(_make(**attrs))
    assert needle in str(excinfo.value)


# ---------------------------------------------------------------------------
# The check of test_operation against the operation rows
# ---------------------------------------------------------------------------


def test_a_declaration_from_the_kit_alone_passes() -> None:
    """A connector package builds its declaration with kit names only."""
    import meho_connector_kit as kit

    class KitConnector(kit.Connector):
        product = "kitfake"
        version = "1.x"
        impl_id = "kitfake-rest"
        default_port = 8443
        target_fields = (
            kit.TargetField(key="region", label="Region", kind="choice", choices=("eu", "us")),
        )
        credential_ways = (
            kit.CredentialWay(
                key="basic",
                label="User name and password",
                fields=(
                    kit.CredentialField(name="username", label="User name", hidden=False),
                    kit.CredentialField(name="password", label="Password"),
                ),
            ),
        )

        async def fingerprint(self, target: Any, operator: Any = None) -> Any:
            raise NotImplementedError

        async def probe(self, target: Any) -> Any:
            raise NotImplementedError

    check_connector_declaration(KitConnector)
    assert kit.TargetField is TargetField
    assert kit.CredentialWay is CredentialWay
    assert kit.CredentialField is CredentialField


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    yield TestClient(app)


def _get(
    client: TestClient,
    product: str,
    *,
    role: str = "operator",
    tenant_id: uuid.UUID | None = None,
) -> Any:
    tenant = str(tenant_id or uuid.uuid4())
    token = mint_token(_KEY, sub=f"op-{role}", tenant_id=tenant, tenant_role=role)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.get(
            _PATH, params={"product": product}, headers={"Authorization": f"Bearer {token}"}
        )


async def _seed_ready_test_read(cls: type[Connector]) -> None:
    """The stored row of the class's test read: ``safe`` and turned on."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=cls.test_operation,
                source_kind="typed",
                parameter_schema={"type": "object"},
                safety_level="safe",
                is_enabled=True,
            )
        )


@pytest.mark.asyncio
async def test_harbor_answer(client: TestClient) -> None:
    await _seed_ready_test_read(HarborConnector)
    resp = _get(client, "harbor")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product"] == "harbor"
    (cls,) = body["classes"]
    assert cls["impl_id"] == "harbor-rest"
    assert cls["test_operation"] == "harbor.project.list"
    (way,) = cls["credential_ways"]
    assert [(f["name"], f["required"], f["hidden"]) for f in way["fields"]] == [
        ("username", True, False),
        ("password", True, True),
    ]


@pytest.mark.asyncio
async def test_linux_answer_has_two_ways_and_a_multiline_key(client: TestClient) -> None:
    await _seed_ready_test_read(LinuxSshConnector)
    resp = _get(client, "linux")
    assert resp.status_code == 200, resp.text
    (cls,) = resp.json()["classes"]
    ways = {w["key"]: w for w in cls["credential_ways"]}
    assert set(ways) == {"key", "password"}
    key_field = next(f for f in ways["key"]["fields"] if f["name"] == "ssh_private_key")
    assert key_field["multiline"] is True
    assert [f["name"] for f in ways["password"]["fields"]] == [
        "username",
        "password",
        "known_hosts",
    ]
    for way in ways.values():
        by_name = {f["name"]: f for f in way["fields"]}
        assert by_name["username"]["required"] is False
        assert by_name["known_hosts"]["multiline"] is True
        assert by_name["known_hosts"]["hidden"] is False
    assert cls["test_operation"] == "linux.identity.show"


def test_kubernetes_answer(client: TestClient) -> None:
    resp = _get(client, "k8s")
    assert resp.status_code == 200, resp.text
    (cls,) = resp.json()["classes"]
    (way,) = cls["credential_ways"]
    assert [(f["name"], f["multiline"]) for f in way["fields"]] == [("kubeconfig", True)]


def test_a_class_that_declares_nothing_answers_with_nulls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        connector_registry._REGISTRY_V2, ("formfake", "1.x", "formfake-rest"), _make()
    )
    (cls,) = _get(client, "formfake").json()["classes"]
    assert cls["target_fields"] is None
    assert cls["credential_ways"] is None
    assert cls["test_operation"] is None
    assert cls["default_port"] is None


def test_unknown_product_is_404_with_a_plain_message(client: TestClient) -> None:
    resp = _get(client, "no-such-product")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No connector serves the product 'no-such-product'."


def test_read_only_role_is_403(client: TestClient) -> None:
    assert _get(client, "harbor", role="read_only").status_code == 403


def test_two_classes_of_one_product_are_both_listed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = type("FirstConnector", (_Base,), {"impl_id": "formfake-a", "version": "1.x"})
    second = type(
        "SecondConnector",
        (_Base,),
        {"impl_id": "formfake-b", "version": "2.x", "supported_version_range": ">=2,<3"},
    )
    monkeypatch.setitem(connector_registry._REGISTRY_V2, ("formfake", "1.x", "formfake-a"), first)
    monkeypatch.setitem(connector_registry._REGISTRY_V2, ("formfake", "2.x", "formfake-b"), second)
    classes = _get(client, "FormFake").json()["classes"]
    assert [(c["impl_id"], c["version"]) for c in classes] == [
        ("formfake-a", "1.x"),
        ("formfake-b", "2.x"),
    ]
    assert classes[1]["supported_version_range"] == ">=2,<3"


@pytest.mark.asyncio
async def test_a_connector_another_tenant_imported_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import registers a class for every tenant; only its own tenant sees it."""
    owner, other = uuid.uuid4(), uuid.uuid4()
    shim = type(
        "AutoShim_privfake",
        (_Base,),
        {"product": "privfake", "impl_id": "privfake-rest", "_shim_kind": "bare"},
    )
    monkeypatch.setitem(connector_registry._REGISTRY_V2, ("privfake", "1.x", "privfake-rest"), shim)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                tenant_id=owner,
                product="privfake",
                version="1.x",
                impl_id="privfake-rest",
                op_id="privfake.item.list",
                source_kind="ingested",
                parameter_schema={"type": "object"},
                safety_level="safe",
                is_enabled=True,
            )
        )
    mine = _get(client, "privfake", tenant_id=owner)
    assert mine.status_code == 200, mine.text
    assert [c["impl_id"] for c in mine.json()["classes"]] == ["privfake-rest"]
    theirs = _get(client, "privfake", tenant_id=other)
    assert theirs.status_code == 404
    assert theirs.json()["detail"] == "No connector serves the product 'privfake'."
    assert _get(client, "harbor", tenant_id=other).status_code == 200


def test_the_route_never_calls_the_secret_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meho_backplane.connectors._shared.vault_creds as vault_creds

    boom = AsyncMock(side_effect=AssertionError("the secret store was called"))
    monkeypatch.setattr(vault_creds, "load_vault_secret_data", boom)
    monkeypatch.setattr(vault_creds, "load_basic_credentials", boom)
    for product in ("harbor", "linux", "k8s"):
        assert _get(client, product).status_code == 200
    boom.assert_not_called()

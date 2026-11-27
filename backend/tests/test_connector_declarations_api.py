# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The API connectors declare what a target needs (target fields, login fields, test read).

Covers:

* every connector class in the API connector folders declares its ways to log in
  (``credential_ways`` is not ``None``), and the folders are not empty;
* the declared names are the names the connector code reads;
* a profiled generic connector gets its way to log in from the profile;
* the Keycloak connector names ``managed_realm`` as required;
* the real start accepts every declaration (the test read of each connector is
  a ``safe`` read that needs no input);
* ``GET /api/v1/connectors/target-form`` answers with the declarations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from importlib import resources
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.connectors_target_form import router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.profile import ExecutionProfile
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import typed_register
from meho_backplane.operations.connector_declaration_check import registered_connector_classes
from meho_backplane.operations.ingest.connector_registration import synthesise_profiled_class
from meho_backplane.settings import get_settings

#: The connector folders of this Task (API connectors).
_FOLDERS = (
    "argocd",
    "azure",
    "entra",
    "gcloud",
    "github",
    "hetzner_robot",
    "keycloak",
    "loki",
    "prometheus",
    "tempo",
    "phpipam",
    "proxmox",
    "rabbitmq",
    "vault",
)

#: Class name -> (default port, target field keys, {way key: field names}, test read).
_EXPECTED: dict[str, tuple[int | None, tuple[str, ...], dict[str, tuple[str, ...]], str | None]] = {
    "ArgoCdConnector": (443, (), {"token": ("token",)}, "argocd.app.list"),
    "AzureConnector": (
        443,
        ("subscription_id", "tenant_id", "cloud"),
        {"client_secret": ("tenant_id", "client_id", "client_secret")},
        "azure.about",
    ),
    "EntraConnector": (
        443,
        ("tenant_id", "cloud"),
        {
            "client_secret": ("tenant_id", "client_id", "client_secret"),
            "certificate": ("tenant_id", "client_id", "private_key", "certificate"),
        },
        "entra.about",
    ),
    "GcloudConnector": (None, ("gcp_project", "gcp_impersonate_sa"), {}, "gcloud.about"),
    "GitHubRestConnector": (
        None,
        (),
        {"app": ("app_id", "private_key", "installation_id"), "token": ("token",)},
        None,
    ),
    "HetznerRobotConnector": (443, (), {"basic": ("username", "password")}, None),
    "KeycloakConnector": (
        None,
        ("managed_realm", "admin_realm"),
        {
            "client": ("client_id", "client_secret"),
            "password": ("username", "password"),
        },
        "keycloak.realm.get",
    ),
    "LokiConnector": (
        3100,
        ("scheme",),
        {"token": ("token",), "basic": ("username", "password")},
        "loki.labels",
    ),
    "TempoConnector": (
        3200,
        ("scheme",),
        {"token": ("token",), "basic": ("username", "password")},
        "tempo.search_tags",
    ),
    "PrometheusConnector": (
        None,
        ("scheme", "path_prefix", "flavour"),
        {"token": ("token",), "basic": ("username", "password")},
        "prometheus.labels",
    ),
    "PhpipamConnector": (
        None,
        ("app_id", "base_path"),
        {"app_code": ("token",), "user": ("username", "password")},
        "phpipam.subnet.list",
    ),
    "ProxmoxConnector": (
        8006,
        (),
        {
            "token": ("token_id", "token_secret"),
            "ticket": ("username", "password", "realm"),
        },
        "proxmox.about",
    ),
    "RabbitMqConnector": (None, (), {"basic": ("username", "password")}, "rabbitmq.overview"),
    "VaultConnector": (None, ("vault_role", "vault_mount"), {}, "vault.sys.mounts.list"),
    "OpenBaoConnector": (None, ("vault_role", "vault_mount"), {}, None),
}


def _classes_of_the_folders() -> list[type[Connector]]:
    connector_registry._eager_import_connectors()
    prefixes = tuple(f"meho_backplane.connectors.{folder}." for folder in _FOLDERS)
    return [
        cls for cls in registered_connector_classes() if f"{cls.__module__}.".startswith(prefixes)
    ]


def test_every_connector_class_of_the_folders_declares_its_ways_to_log_in() -> None:
    classes = _classes_of_the_folders()
    missing = [cls.__name__ for cls in classes if cls.credential_ways is None]
    assert missing == [], f"These connector classes declare no credential_ways: {missing}"
    folders_seen = {cls.__module__.split(".")[2] for cls in classes}
    assert folders_seen == set(_FOLDERS)


def test_the_declarations_match_the_table() -> None:
    classes = {cls.__name__: cls for cls in _classes_of_the_folders()}
    assert set(classes) == set(_EXPECTED)
    for name, (port, field_keys, ways, test_operation) in _EXPECTED.items():
        cls = classes[name]
        assert cls.default_port == port, name
        assert tuple(f.key for f in cls.target_fields or ()) == field_keys, name
        assert {w.key: tuple(f.name for f in w.fields) for w in cls.credential_ways or ()} == ways
        assert cls.test_operation == test_operation, name


def test_the_declared_names_are_the_names_the_code_reads() -> None:
    from meho_backplane.connectors.argocd.session import ARGOCD_TOKEN_FIELD
    from meho_backplane.connectors.azure import session as azure_session
    from meho_backplane.connectors.entra import session as entra_session
    from meho_backplane.connectors.github.session import _GH_APP_FIELDS, _GH_PAT_FIELD
    from meho_backplane.connectors.keycloak import session as keycloak_session
    from meho_backplane.connectors.prometheus.connector import _VALID_FLAVOURS
    from meho_backplane.connectors.proxmox import session as proxmox_session
    from meho_backplane.connectors.rabbitmq.session import RABBITMQ_CREDENTIAL_FIELDS

    classes = {cls.__name__: cls for cls in _classes_of_the_folders()}

    def way_fields(name: str, key: str) -> tuple[str, ...]:
        (way,) = [w for w in classes[name].credential_ways or () if w.key == key]
        return tuple(f.name for f in way.fields)

    def target_field(name: str, key: str) -> Any:
        (field,) = [f for f in classes[name].target_fields or () if f.key == key]
        return field

    assert way_fields("ArgoCdConnector", "token") == (ARGOCD_TOKEN_FIELD,)
    assert way_fields("AzureConnector", "client_secret") == azure_session._SECRET_KEYS
    assert target_field("AzureConnector", "cloud").choices == tuple(azure_session.AZURE_CLOUDS)
    assert target_field("AzureConnector", "cloud").default == azure_session.DEFAULT_CLOUD
    assert target_field("EntraConnector", "cloud").choices == tuple(entra_session.ENTRA_CLOUDS)
    assert way_fields("EntraConnector", "client_secret") == (
        *entra_session._SECRET_KEYS,
        *entra_session._SECRET_WAY,
    )
    assert way_fields("EntraConnector", "certificate") == (
        *entra_session._SECRET_KEYS,
        *entra_session._CERTIFICATE_WAY,
    )
    assert way_fields("GitHubRestConnector", "app") == _GH_APP_FIELDS
    assert way_fields("GitHubRestConnector", "token") == (_GH_PAT_FIELD,)
    assert way_fields("KeycloakConnector", "client") == keycloak_session._CLIENT_FIELDS
    assert way_fields("KeycloakConnector", "password") == keycloak_session._PASSWORD_FIELDS
    assert target_field("KeycloakConnector", "admin_realm").default == (
        keycloak_session.DEFAULT_ADMIN_REALM
    )
    assert way_fields("ProxmoxConnector", "token") == (
        proxmox_session.TOKEN_ID_FIELD,
        proxmox_session.TOKEN_SECRET_FIELD,
    )
    assert way_fields("ProxmoxConnector", "ticket") == (
        proxmox_session.USERNAME_FIELD,
        proxmox_session.PASSWORD_FIELD,
        proxmox_session.REALM_FIELD,
    )
    assert way_fields("RabbitMqConnector", "basic") == RABBITMQ_CREDENTIAL_FIELDS
    assert set(target_field("PrometheusConnector", "flavour").choices) == _VALID_FLAVOURS


def test_keycloak_names_the_managed_realm_as_required() -> None:
    classes = {cls.__name__: cls for cls in _classes_of_the_folders()}
    fields = {f.key: f for f in classes["KeycloakConnector"].target_fields or ()}
    assert fields["managed_realm"].required is True
    assert fields["admin_realm"].required is False


def test_an_optional_login_is_marked_in_the_declaration_not_forced() -> None:
    # A Proxmox ticket login may leave the realm empty (the code uses pam).
    classes = {cls.__name__: cls for cls in _classes_of_the_folders()}
    (ticket,) = [w for w in classes["ProxmoxConnector"].credential_ways or () if w.key == "ticket"]
    assert {f.name: f.required for f in ticket.fields}["realm"] is False


def _shipped_profile() -> ExecutionProfile:
    text = (
        resources.files("meho_backplane.connectors.profiles")
        .joinpath("meho_automation_minimal.yaml")
        .read_text(encoding="utf-8")
    )
    return ExecutionProfile.model_validate(yaml.safe_load(text))


def test_a_profiled_connector_gets_its_way_to_log_in_from_the_profile() -> None:
    profile = _shipped_profile()
    cls = synthesise_profiled_class(
        product="mehoauto", version="0.1.0", impl_id="mehoauto-rest", profile=profile
    )
    (way,) = cls.credential_ways or ()
    assert way.key == profile.auth.scheme
    assert tuple(f.name for f in way.fields) == profile.auth.secret_fields
    # A profile has no read the start check accepts, so none is declared.
    assert cls.test_operation is None


def test_a_profiled_connector_with_other_fields_follows_its_profile() -> None:
    profile = ExecutionProfile.model_validate(
        {
            "product": "demo",
            "version": "1.0.0",
            "auth": {"scheme": "basic", "secret_fields": ["username", "password"]},
            "fingerprint": {"path": "/version", "authenticated": False, "version_key": "v"},
            "probe": "delegate",
            "pagination": {"strategy": "none", "items_key": "items"},
        }
    )
    cls = synthesise_profiled_class(
        product="demo", version="1.0.0", impl_id="demo-rest", profile=profile
    )
    (way,) = cls.credential_ways or ()
    assert tuple(f.name for f in way.fields) == ("username", "password")
    assert [f.hidden for f in way.fields] == [False, True]


def test_a_bare_generic_connector_stays_not_declared() -> None:
    from meho_backplane.operations.ingest.connector_registration import GenericRestConnector

    assert GenericRestConnector.credential_ways is None
    assert GenericRestConnector.test_operation is None


# ---------------------------------------------------------------------------
# The real start and the route
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


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_jwt_and_bind] = lambda: Operator(
        sub="op", raw_jwt="test-jwt", tenant_id=uuid.uuid4(), tenant_role=TenantRole.OPERATOR
    )
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_real_start_env")
async def test_the_real_start_accepts_every_declaration_and_the_route_returns_them() -> None:
    """Every named test read is a safe read with no input, and the route shows the fields."""
    async with typed_register.operation_catalog_write(embedding_service=_embedding_service()):
        pass
    client = _client()
    answer = client.get("/api/v1/connectors/target-form", params={"product": "keycloak"})
    assert answer.status_code == 200, answer.text
    (entry,) = answer.json()["classes"]
    assert entry["test_operation"] == "keycloak.realm.get"
    required = {f["key"]: f["required"] for f in entry["target_fields"]}
    assert required == {"managed_realm": True, "admin_realm": False}
    assert [w["key"] for w in entry["credential_ways"]] == ["client", "password"]

    loki = client.get("/api/v1/connectors/target-form", params={"product": "loki"}).json()
    (loki_entry,) = loki["classes"]
    assert loki_entry["default_port"] == 3100
    assert loki_entry["test_operation"] == "loki.labels"

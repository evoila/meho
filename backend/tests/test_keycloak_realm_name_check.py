# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Keycloak realm name is one plain name, and it is always encoded in a path.

* The shared check refuses a realm that is not a plain name; nothing is
  trimmed.
* Create and update refuse such a target at REST, through
  ``targets.register`` and ``targets.update``, and on the import path.
  Keycloak and Vault see no call.
* A stored target with such a value: every Keycloak operation is refused
  before any secret read or request.
* Every path template keeps its segments, whatever the values hold.
* A realm param, and the realm of a secret broker ref, must be plain too.
* A managed realm that is the admin realm, or MEHO's own sign-in realm,
  is allowed with a warning: at create, at update and on the approval card.
"""

from __future__ import annotations

import copy
import inspect
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector, _paths
from meho_backplane.connectors.keycloak.realm_params import check_realm_params
from meho_backplane.connectors.keycloak.secret_endpoint import (
    KeycloakSecretRefError,
    _parse_keycloak_client_ref,
    _parse_keycloak_ref,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakManagedRealmMissingError,
    KeycloakTargetLike,
)
from meho_backplane.connectors.targets.ops import targets_register
from meho_backplane.connectors.targets.update import targets_update
from meho_backplane.targets.keycloak_realm_warnings import keycloak_realm_warnings
from meho_backplane.targets.required_extras import check_keycloak_managed_realm, realm_name_problem

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

#: Realm values that are not one plain name.
BAD_REALMS = ["x/../other", "name?x=", "name#", " name ", "..", "a%2Fb", "a\\b", "a\tb", "x" * 256]

_KC_HOST = "keycloak-a.test.invalid"
_TENANT = UUID(DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# The shared check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", BAD_REALMS)
def test_a_realm_that_is_not_a_plain_name_is_refused(value: str) -> None:
    assert realm_name_problem(value) is not None
    with pytest.raises(ValueError, match=r"extras\.managed_realm"):
        check_keycloak_managed_realm("keycloak", {"managed_realm": value}, "kc")
    with pytest.raises(ValueError, match=r"extras\.admin_realm"):
        check_keycloak_managed_realm(
            "keycloak", {"managed_realm": "example", "admin_realm": value}, "kc"
        )


@pytest.mark.parametrize("value", ["example", "Example Realm", "a.b", "...a", "x" * 255, "réalm"])
def test_a_plain_realm_name_passes(value: str) -> None:
    assert realm_name_problem(value) is None
    check_keycloak_managed_realm("keycloak", {"managed_realm": value, "admin_realm": value}, "kc")


def test_an_empty_admin_realm_means_the_default() -> None:
    check_keycloak_managed_realm("keycloak", {"managed_realm": "example", "admin_realm": ""}, "kc")
    check_keycloak_managed_realm(
        "keycloak", {"managed_realm": "example", "admin_realm": None}, "kc"
    )


# ---------------------------------------------------------------------------
# Create and update through every door
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _auth(key: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_admin_token(key)}"}


def _body(name: str, realm: str) -> dict[str, Any]:
    return {
        "name": name,
        "product": "keycloak",
        "host": _KC_HOST,
        "extras": {"managed_realm": realm},
    }


@pytest.mark.parametrize("realm", BAD_REALMS[:5])
def test_rest_create_and_update_refuse_a_realm_that_is_not_plain(
    client: TestClient, realm: str
) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        keycloak = mock.route(host=_KC_HOST).respond(200)
        created = client.post("/api/v1/targets", json=_body("kc-bad", realm), headers=_auth(key))
        good = client.post("/api/v1/targets", json=_body("kc-good", "example"), headers=_auth(key))
        updated = client.patch(
            "/api/v1/targets/kc-good",
            json={"extras": {"managed_realm": realm}},
            headers=_auth(key),
        )
        names = [
            t["name"] for t in client.get("/api/v1/targets", headers=_auth(key)).json()["items"]
        ]
    assert created.status_code == 422, created.text
    assert "extras.managed_realm" in created.json()["detail"]
    assert good.status_code == 201
    assert updated.status_code == 422, updated.text
    assert names == ["kc-good"]
    assert not keycloak.called


def test_import_entry_with_a_realm_that_is_not_plain_is_refused(client: TestClient) -> None:
    """The import replays one POST per entry; a bad realm in the entry's extras is refused."""
    key = make_rsa_keypair("kid-A")
    entry = {
        "name": "kc-import",
        "product": "keycloak",
        "host": _KC_HOST,
        "version": "26.3",
        "extras": {"managed_realm": "../master", "admin_realm": "master", "site": "lab"},
    }
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        keycloak = mock.route(host=_KC_HOST).respond(200)
        response = client.post("/api/v1/targets", json=entry, headers=_auth(key))
    assert response.status_code == 422
    assert not keycloak.called


def _admin() -> Operator:
    return Operator(
        sub="p-1",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", ["x/../other", " name ", ".."])
async def test_targets_register_and_update_ops_refuse_a_realm_that_is_not_plain(
    realm: str,
) -> None:
    existing = await _insert_target(
        name="kc-op", product="keycloak", host=_KC_HOST, extras={"managed_realm": "example"}
    )
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route().respond(200)
        with pytest.raises(ValueError, match=r"extras\.managed_realm"):
            await targets_register(_admin(), None, _body("kc-op-new", realm))
        with pytest.raises(ValueError, match=r"extras\.managed_realm"):
            await targets_update(
                _admin(), None, {"target_id": str(existing.id), "extras": {"managed_realm": realm}}
            )
    assert not everything.called


# ---------------------------------------------------------------------------
# A stored target with a bad realm: every operation is refused before any call
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str = "keycloak-a"
    host: str = _KC_HOST
    port: int | None = 443
    secret_ref: str | None = "tenant/keycloak/admin"
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="operator.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _all_ops() -> list[Any]:
    from meho_backplane.connectors.keycloak.ops_authflow import APPROVAL_FLOW_OPS
    from meho_backplane.connectors.keycloak.ops_authflow_passkey_schemas import (
        PASSKEY_CODE_SKIP_OPS,
    )
    from meho_backplane.connectors.keycloak.ops_authflow_touch_schemas import TOUCH_OPS
    from meho_backplane.connectors.keycloak.ops_flow_reads import FLOW_READ_OPS
    from meho_backplane.connectors.keycloak.ops_identity_provider import IDENTITY_PROVIDER_OPS
    from meho_backplane.connectors.keycloak.ops_lock_status import LOCK_STATUS_OPS
    from meho_backplane.connectors.keycloak.ops_login_check import LOGIN_CHECK_OPS
    from meho_backplane.connectors.keycloak.ops_read import READ_OPS
    from meho_backplane.connectors.keycloak.ops_user_required_actions import (
        USER_REQUIRED_ACTION_OPS,
    )
    from meho_backplane.connectors.keycloak.ops_user_state import USER_STATE_OPS
    from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS

    return [
        *READ_OPS,
        *WRITE_OPS,
        *LOGIN_CHECK_OPS,
        *APPROVAL_FLOW_OPS,
        *PASSKEY_CODE_SKIP_OPS,
        *TOUCH_OPS,
        *IDENTITY_PROVIDER_OPS,
        *USER_REQUIRED_ACTION_OPS,
        *USER_STATE_OPS,
        *LOCK_STATUS_OPS,
        *FLOW_READ_OPS,
    ]


#: A value for each JSON schema type a required param can have.
_VALUE_FOR_TYPE: dict[str, Any] = {"object": {}, "array": [], "boolean": False, "integer": 1}


def _sample(schema: dict[str, Any]) -> Any:
    """A value of the schema's type, for the required params of an op."""
    if "enum" in schema:
        return schema["enum"][0]
    types = schema.get("type")
    names = types if isinstance(types, list) else [types]
    for name in names:
        if name in _VALUE_FOR_TYPE:
            return copy.deepcopy(_VALUE_FOR_TYPE[name])
    return "example-value"


def _params(op: Any) -> dict[str, Any]:
    schema = op.parameter_schema
    props = schema.get("properties", {})
    return {name: _sample(props.get(name, {})) for name in schema.get("required", [])}


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", BAD_REALMS[:5])
async def test_every_operation_on_a_stored_bad_realm_is_refused_before_any_call(
    realm: str,
) -> None:
    loads: list[str] = []

    async def _load(_t: KeycloakTargetLike, _o: Operator) -> KeycloakAdminCredentials:
        loads.append("loaded")
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="s3cret")

    connector = KeycloakConnector(credentials_loader=_load)
    target = _StubTarget(extras={"managed_realm": realm})
    ops = _all_ops()
    assert len(ops) > 40
    async with respx.mock(assert_all_called=False) as mock:
        for op in ops:
            handler = getattr(connector, op.handler_attr)
            with pytest.raises(KeycloakManagedRealmMissingError, match="plain realm name"):
                await handler(_operator(), target, _params(op))
        assert mock.calls.call_count == 0
    assert loads == []


# ---------------------------------------------------------------------------
# Every path template keeps its segments
# ---------------------------------------------------------------------------


def _templates() -> list[str]:
    found = [
        value
        for name, value in inspect.getmembers(_paths)
        if name.endswith("_PATH") and isinstance(value, str) and "{" in value
    ]
    from meho_backplane.connectors.keycloak.ops_authflow_touch import (
        _AUTHENTICATOR_PROVIDERS_PATH,
    )

    return [*found, _AUTHENTICATOR_PROVIDERS_PATH]


@pytest.mark.parametrize("value", ["x/../other", "a/b", "..", "name?x=", "name#y", "a%2Fb"])
def test_a_value_never_changes_the_segments_of_any_path(value: str) -> None:
    templates = _templates()
    assert len(templates) > 30
    for template in templates:
        names = re.findall(r"\{([^{}]+)\}", template)
        path = _paths.fill_path(template, dict.fromkeys(names, value))
        assert path.count("/") == template.count("/"), (template, path)
        assert "?" not in path and "#" not in path, (template, path)
        assert "/../" not in path and not path.endswith("/.."), (template, path)


# ---------------------------------------------------------------------------
# Realm params and secret broker refs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("keycloak.realm.update", {"realm": " name ", "representation": {}}),
        ("keycloak.realm.update", {"representation": {"realm": "../master"}}),
        ("keycloak.realm.create", {"representation": {"realm": "a/b"}}),
        ("keycloak.realm.delete", {"realm": ".."}),
        ("keycloak.user.check_login", {"username": "u", "realm": "name#", "password_ref": "x"}),
    ],
)
def test_a_realm_param_that_is_not_plain_is_refused_before_the_call(
    op_id: str, params: dict[str, Any]
) -> None:
    problem = check_realm_params(op_id)(params)
    assert problem is not None
    assert problem["validator"] == "params_check"


def test_a_plain_realm_param_passes() -> None:
    assert check_realm_params("keycloak.realm.update")({"realm": "example"}) is None
    assert check_realm_params("keycloak.user.check_login")({"username": "u"}) is None


@pytest.mark.parametrize("realm", ["..", " name ", "."])
def test_a_secret_ref_realm_that_is_not_plain_is_refused(realm: str) -> None:
    with pytest.raises(KeycloakSecretRefError, match="plain realm name"):
        _parse_keycloak_ref(f"kc/{realm}/operator-a#password")
    with pytest.raises(KeycloakSecretRefError, match="plain realm name"):
        _parse_keycloak_client_ref(f"kc/{realm}/clients/app#secret")


def test_a_secret_ref_with_a_plain_realm_passes() -> None:
    assert _parse_keycloak_ref("kc/example/operator-a#password") == ("kc", "example", "operator-a")


# ---------------------------------------------------------------------------
# Warnings: the admin realm, and MEHO's own sign-in realm
# ---------------------------------------------------------------------------


def test_warnings_name_the_admin_realm_and_the_meho_sign_in_realm() -> None:
    # The test settings sign MEHO in with the realm "meho".
    assert keycloak_realm_warnings("keycloak", {"managed_realm": "example"}) == []
    assert keycloak_realm_warnings("ssh", {"managed_realm": "master"}) == []
    [admin] = keycloak_realm_warnings("keycloak", {"managed_realm": "Master"})
    assert "admin realm of this Keycloak" in admin
    [custom] = keycloak_realm_warnings("keycloak", {"managed_realm": "ops", "admin_realm": "ops"})
    assert "admin realm" in custom
    [own] = keycloak_realm_warnings("keycloak", {"managed_realm": "meho"})
    assert "realm MEHO itself signs in with" in own


def test_create_and_update_answer_with_the_warning(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        plain = client.post(
            "/api/v1/targets", json=_body("kc-plain", "example"), headers=_auth(key)
        )
        admin = client.post("/api/v1/targets", json=_body("kc-admin", "master"), headers=_auth(key))
        own = client.patch(
            "/api/v1/targets/kc-plain",
            json={"extras": {"managed_realm": "meho"}},
            headers=_auth(key),
        )
    assert plain.status_code == 201
    assert plain.json()["warnings"] == []
    assert admin.status_code == 201
    assert "admin realm of this Keycloak" in admin.json()["warnings"][0]
    assert own.status_code == 200
    assert "realm MEHO itself signs in with" in own.json()["warnings"][0]


@pytest.mark.asyncio
async def test_targets_ops_answer_with_the_warning_only_when_there_is_one() -> None:
    plain = await targets_register(_admin(), None, _body("kc-op-plain", "example"))
    risky = await targets_register(_admin(), None, _body("kc-op-risky", "master"))
    updated = await targets_update(
        _admin(), None, {"target_id": plain["target_id"], "extras": {"managed_realm": "meho"}}
    )
    assert "warnings" not in plain
    assert "admin realm" in risky["warnings"][0]
    assert "realm MEHO itself signs in with" in updated["warnings"][0]


@pytest.mark.asyncio
async def test_the_approval_card_of_a_write_shows_the_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.operations import dispatcher

    async def _no_instance(_descriptor: Any, _target: Any) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(dispatcher, "_resolve_connector_instance", _no_instance)
    descriptor = SimpleNamespace(
        op_id="keycloak.user.create", safety_level="caution", requires_approval=True
    )
    effects = []
    for realm in ("master", "example"):
        target = _StubTarget(extras={"managed_realm": realm})
        effects.append(
            await dispatcher._build_proposed_effect(
                op_id="keycloak.user.create",
                connector_id="keycloak-admin-26.x",
                descriptor=descriptor,  # type: ignore[arg-type]
                operator=_operator(),
                target=SimpleNamespace(product="keycloak", **target.__dict__),
                params={"representation": {"username": "u"}},
            )
        )
    assert effects[0] is not None
    assert "admin realm of this Keycloak" in effects[0]["target_warnings"][0]
    assert effects[1] is not None
    assert "target_warnings" not in effects[1]


# ---------------------------------------------------------------------------
# The list of refused targets: one tenant, or every tenant for a platform admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_check_route_lists_refused_targets_and_a_platform_admin_sees_all() -> None:
    from meho_backplane.api.v1.targets_keycloak_realm import router as check_router

    from ._oidc_jwt_helpers import mint_token

    other_tenant = UUID("00000000-0000-0000-0000-00000000b0b0")
    await _insert_target(name="kc-fine", product="keycloak", extras={"managed_realm": "example"})
    await _insert_target(name="kc-none", product="keycloak", extras={"admin_realm": "master"})
    await _insert_target(name="kc-path", product="keycloak", extras={"managed_realm": "a/../b"})
    await _insert_target(name="box", product="ssh", extras={})
    await _insert_target(name="kc-other", product="keycloak", tenant_id=other_tenant, extras={})

    app = _build_app()
    app.include_router(check_router)
    key = make_rsa_keypair("kid-A")
    url = "/api/v1/targets/checks/keycloak-realm"
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as test_client:
            own = test_client.get(url, headers=_auth(key))
            platform = test_client.get(
                url,
                headers={
                    "Authorization": "Bearer "
                    + mint_token(key, tenant_role=TenantRole.OPERATOR.value, platform_admin=True)
                },
            )
    assert own.status_code == 200, own.text
    assert own.json()["all_tenants"] is False
    assert [i["name"] for i in own.json()["items"]] == ["kc-none", "kc-path"]
    assert "names no managed realm" in own.json()["items"][0]["problem"]
    assert "not a plain realm name" in own.json()["items"][1]["problem"]
    assert platform.status_code == 200, platform.text
    assert platform.json()["all_tenants"] is True
    names = {(i["tenant_id"], i["name"]) for i in platform.json()["items"]}
    assert names == {
        (DEFAULT_TENANT_ID, "kc-none"),
        (DEFAULT_TENANT_ID, "kc-path"),
        (str(other_tenant), "kc-other"),
    }

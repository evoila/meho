# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings file import checks Keycloak realms like a target write does.

* The preview refuses a Keycloak target with no managed realm, or with a realm
  that is not a plain name, with the same sentence as the apply.
* A managed realm that is the admin realm, or MEHO's own sign-in realm, is
  allowed. The preview and the apply both carry the warning that REST gives.

The apply runs as a person in the web console, the only caller for whom a
file that adds or changes targets applies.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.main import app

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._rest_write_helpers import settings_import_at_once
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_KC_HOST = "keycloak-a.test.invalid"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _file(extras: str) -> str:
    return f"""
apiVersion: meho.evoila.com/v1
kind: Settings
spec:
  tenants:
    - id: {_TENANT}
      targets:
        - name: kc-01
          product: keycloak
          host: {_KC_HOST}
          version: "26.3"
          extras: {extras}
"""


def _preview_and_apply(client: TestClient, content: str) -> tuple[Any, Any]:
    key = make_rsa_keypair("kid-kc-settings")
    token = mint_token(
        key, sub="admin-person", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"}
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        keycloak = mock.route(host=_KC_HOST).respond(200)
        url = "/api/v1/settings/import"
        preview = client.post(url, params={"preview": "true"}, content=content, headers=headers)
        with settings_import_at_once(app):
            applied = client.post(url, content=content, headers=headers)
    assert not keycloak.called
    return preview, applied


async def _target_names() -> set[str]:
    async with get_sessionmaker()() as session:
        return set((await session.execute(select(Target.name))).scalars())


@pytest.mark.parametrize(
    ("extras", "words"),
    [
        ("{site: lab}", "names no managed realm"),
        ("{managed_realm: '../master'}", "is not a plain realm name"),
    ],
)
async def test_the_preview_refuses_a_bad_realm_like_the_apply(
    client: TestClient, extras: str, words: str
) -> None:
    preview, applied = _preview_and_apply(client, _file(extras))
    assert preview.status_code == 200, preview.text
    assert preview.json()["ok"] is False
    assert preview.json()["diff"] is None
    [mistake] = preview.json()["mistakes"]
    assert mistake.startswith("`spec.tenants[0].targets[0]`")
    assert words in mistake
    assert applied.status_code == 422, applied.text
    assert applied.json()["mistakes"] == [mistake]
    assert await _target_names() == set()


async def test_a_new_target_on_the_admin_realm_carries_the_warning(client: TestClient) -> None:
    preview, applied = _preview_and_apply(client, _file("{managed_realm: master}"))
    assert preview.status_code == 200, preview.text
    [warning] = preview.json()["warnings"]
    assert warning.startswith("`spec.tenants[0].targets[0]` (kc-01): ")
    assert "admin realm of this Keycloak" in warning
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["warnings"] == [warning]
    assert await _target_names() == {"kc-01"}


async def test_an_update_to_the_meho_sign_in_realm_carries_the_warning(
    client: TestClient,
) -> None:
    # The test settings sign MEHO in with the realm "meho".
    await _insert_target(
        tenant_id=_TENANT,
        name="kc-01",
        product="keycloak",
        host=_KC_HOST,
        version="26.3",
        secret_ref=None,
        extras={"managed_realm": "example"},
    )
    preview, applied = _preview_and_apply(client, _file("{managed_realm: meho}"))
    assert preview.status_code == 200, preview.text
    [warning] = preview.json()["warnings"]
    assert "realm MEHO itself signs in with" in warning
    assert applied.status_code == 200, applied.text
    assert applied.json()["warnings"] == [warning]


async def test_a_plain_realm_gives_no_warning(client: TestClient) -> None:
    preview, applied = _preview_and_apply(client, _file("{managed_realm: example}"))
    assert preview.json()["warnings"] == []
    assert applied.status_code == 200, applied.text
    assert applied.json()["warnings"] == []

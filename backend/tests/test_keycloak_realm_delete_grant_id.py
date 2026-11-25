# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A realm delete under a delete grant compares the realm id before the DELETE.

In sandbox mode a service login may run ``keycloak.realm.delete`` under a
delete grant, with no approval card. MEHO builds a fresh preview of the
call to check the preview hash. The handler compares the realm id of its
own read, right before the DELETE, with the realm id of that preview:

* another id: the call is refused with ``realm_id_changed`` and no DELETE
  is sent;
* the same id: the DELETE is sent;
* the fresh preview stays with its own call: after the call, no preview is
  left for the next call.

Drives the real dispatch stack against a respx-mocked Keycloak. Fixture
values are made up.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.operations._preview import grant_preview_var
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests.test_connectors_keycloak_realm_delete import (
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _OP,
    _REALM,
    _REALM_ID,
    _REALM_PATH,
    _TENANT_ID,
    _args,
    _in_the_handler,
    _mount_counts,
    _mount_token,
    _realm_rep,
    _required_settings_env,  # noqa: F401 -- autouse fixture: the settings these tests need
    _reset_module_state,  # noqa: F401 -- autouse fixture: fresh dispatcher caches
    kc,  # noqa: F401 -- the Keycloak connector fixture
)

_SERVICE = "svc-kc-realm-delete-grant"
_OTHER_ID = "4f6c2a1e-0000-0000-0000-00000000d004"


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    turn_sandbox_mode_on(monkeypatch)
    yield
    turn_sandbox_mode_off(monkeypatch)


def _service() -> Operator:
    return Operator(
        sub=_SERVICE,
        raw_jwt="<kc-realm-delete-grant-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


@pytest.mark.parametrize("same_id", [True, False], ids=["same-id", "other-id"])
async def test_a_delete_under_a_grant_compares_the_realm_id_with_the_fresh_preview(
    kc: KeycloakConnector,  # noqa: F811
    same_id: bool,
) -> None:
    """The checks before the run read one id; the handler's own read may read another."""
    grant_id = await seed_delete_grant(
        tenant_id=_TENANT_ID,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR_ID,
        target_product="keycloak",
    )
    handler_reads: list[str] = []

    def _realm(_request: httpx.Request) -> httpx.Response:
        if del_route.called:
            return httpx.Response(404)
        realm_id = _REALM_ID
        if _in_the_handler():
            realm_id = _REALM_ID if same_id else _OTHER_ID
            handler_reads.append(realm_id)
        return httpx.Response(200, json=_realm_rep(realm_id=realm_id))

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(side_effect=_realm)
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(_service(), _args(_REALM))
        assert preview["status"] == "ok", preview
        call = await call_operation(
            _service(), {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )

    # The delete grant cleared the call: it ran without an approval request.
    assert len(await grant_use_rows(grant_id)) == 1
    assert grant_preview_var.get() is None
    if same_id:
        assert call["status"] == "ok", call
        assert call["result"] == {"realm": _REALM, "deleted": True, "already_absent": False}
        assert del_route.call_count == 1
        return
    assert handler_reads == [_OTHER_ID], call
    assert call["status"] == "error", call
    assert "(realm_id_changed)" in str(call)
    assert "the preview MEHO built for this call" in str(call)
    assert not del_route.called

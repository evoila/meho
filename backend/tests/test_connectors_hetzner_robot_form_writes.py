# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Robot writes go out as form fields (#3973).

Robot reads write bodies only as ``application/x-www-form-urlencoded``. The
dispatcher hands every ingested write body to the connector's ``_post_json``
as ``json=``; :class:`HetznerRobotConnector` overrides that seam and sends
form fields instead. These tests prove, with a respx-mocked Robot:

* the encoder: text and numbers as they are, booleans as ``true`` /
  ``false``, lists as repeated ``name[]`` fields, and nested values refused;
* a dispatch of the vSwitch membership add (POST) and remove (DELETE) sends
  the right verb, the form content type and repeated ``server[]`` fields,
  and maps Robot's empty answer to ``{}``;
* a ``500`` or a timeout causes exactly one HTTP call (no retry);
* a 401 is one call and comes back as ``connector_auth_failed``;
* a nested object (the firewall set's rule list) is refused before any HTTP
  call.

The dispatches use ``_approved=True`` — the approval resume path — because
every vSwitch write waits for approval; the approval rules themselves are in
``test_connectors_hetzner_robot_vswitch_governance.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from meho_backplane.connectors.hetzner_robot.connector import robot_form_fields
from meho_backplane.db.models import Target
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._robot_vswitch_fixtures import (
    ADD_OP,
    BASE_URL,
    FIREWALL_SET_OP,
    REMOVE_OP,
    VSWITCH_ID,
    enable_ops,
    ingest_shipped_spec,
    install_connector,
    make_embedding_service,
    make_operator,
    membership_params,
    register_robot_connector,
    seed_target,
)

_CONNECTOR_ID = "hetzner-rest-2026.04"
_MEMBERSHIP_PATH = f"/vswitch/{VSWITCH_ID}/server"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    register_robot_connector()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


async def _setup(*op_ids: str) -> Target:
    await ingest_shipped_spec(make_embedding_service())
    await enable_ops(*op_ids)
    target = await seed_target()
    install_connector()
    return target


async def _dispatch_approved(target: Target, op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )


# ---------------------------------------------------------------------------
# The encoder
# ---------------------------------------------------------------------------


def test_form_fields_keep_text_and_numbers_and_spell_booleans() -> None:
    assert robot_form_fields({"name": "net-a", "vlan": 4001, "ratio": 1.5, "on": True}) == {
        "name": "net-a",
        "vlan": "4001",
        "ratio": "1.5",
        "on": "true",
    }
    assert robot_form_fields({"on": False}) == {"on": "false"}


def test_form_fields_send_a_list_as_repeated_name_brackets() -> None:
    assert robot_form_fields({"server": [321, "1.2.3.4", True]}) == {
        "server[]": ["321", "1.2.3.4", "true"]
    }
    assert robot_form_fields({"server": (7,)}) == {"server[]": ["7"]}


@pytest.mark.parametrize(
    "body",
    [
        {"rules": {"input": [{"action": "accept"}]}},
        {"server": [{"server_number": 321}]},
        {"server": [[321]]},
        {"name": None},
    ],
    ids=["nested-object", "object-in-list", "list-in-list", "null"],
)
def test_form_fields_refuse_values_robot_cannot_read(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="form field"):
        robot_form_fields(body)


def test_form_fields_refuse_a_body_that_is_not_an_object() -> None:
    with pytest.raises(ValueError, match="object of fields"):
        robot_form_fields([321])


# ---------------------------------------------------------------------------
# Dispatch: the right verb, form fields, empty answer -> {}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "verb"), [(ADD_OP, "POST"), (REMOVE_OP, "DELETE")])
async def test_membership_dispatch_sends_form_fields_with_the_right_verb(
    op_id: str, verb: str
) -> None:
    target = await _setup(op_id)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route(method=verb, path=_MEMBERSHIP_PATH).respond(200, content=b"")
        result = await _dispatch_approved(target, op_id, membership_params(321, "1.2.3.4"))

    assert result.status == "ok", result.error
    assert result.result == {}
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.method == verb
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["authorization"].startswith("Basic ")
    assert parse_qsl(request.content.decode()) == [("server[]", "321"), ("server[]", "1.2.3.4")]


async def test_membership_dispatch_maps_a_204_to_an_empty_object() -> None:
    target = await _setup(ADD_OP)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_MEMBERSHIP_PATH).respond(204)
        result = await _dispatch_approved(target, ADD_OP, membership_params(321))

    assert result.status == "ok", result.error
    assert result.result == {}
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# No retry: a 500 or a timeout is exactly one HTTP call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "verb"), [(ADD_OP, "POST"), (REMOVE_OP, "DELETE")])
async def test_a_500_is_one_call_and_an_error(op_id: str, verb: str) -> None:
    target = await _setup(op_id)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route(method=verb, path=_MEMBERSHIP_PATH).respond(500)
        result = await _dispatch_approved(target, op_id, membership_params(321))

    assert result.status == "error"
    assert route.call_count == 1


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.ConnectTimeout])
async def test_a_timeout_is_one_call_and_an_error(timeout: type[httpx.TimeoutException]) -> None:
    target = await _setup(ADD_OP)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_MEMBERSHIP_PATH).mock(side_effect=timeout("slow"))
        result = await _dispatch_approved(target, ADD_OP, membership_params(321))

    assert result.status == "error"
    assert route.call_count == 1


async def test_a_401_is_one_call_and_reports_auth_failed() -> None:
    target = await _setup(ADD_OP)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_MEMBERSHIP_PATH).respond(401)
        result = await _dispatch_approved(target, ADD_OP, membership_params(321))

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# A nested object is refused before any HTTP call
# ---------------------------------------------------------------------------


async def test_nested_firewall_rules_are_refused_before_any_http_call() -> None:
    """The firewall set stays off in production; switched on here, its nested
    rule list has no form shape, so nothing reaches Robot."""
    target = await _setup(FIREWALL_SET_OP)
    params = {
        "server-ip": "1.2.3.4",
        "body": {"status": "active", "rules": {"input": [{"action": "accept"}]}},
    }
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/firewall/1.2.3.4").respond(200, content=b"")
        result = await _dispatch_approved(target, FIREWALL_SET_OP, params)

    assert result.status == "error"
    assert "form field" in result.extras["exception_message"]
    assert route.call_count == 0


async def test_connector_refuses_a_nested_body_before_any_http_call() -> None:
    target = await _setup()
    connector = install_connector()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_MEMBERSHIP_PATH).respond(200, content=b"")
        with pytest.raises(ValueError, match="form field"):
            await connector._post_json(
                target,
                _MEMBERSHIP_PATH,
                operator=make_operator(),
                json={"server": [{"server_number": 321}]},
            )
    assert route.call_count == 0
    await connector.aclose()

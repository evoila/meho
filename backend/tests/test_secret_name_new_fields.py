# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SNMP keys, passphrases and other key-word names are hidden in every place.

One name rule (``broadcast.events.is_secret_param_name``) serves the normal
answer and the audit row, the approval preview (also the Keycloak one), the
broadcast params, the trace, the error text and the query names of a link.
Plain identifiers stay visible. A name that ends in ``ref`` is hidden when
the rest of it is a secret name (``password_ref``), as on main.

The tests are one table: a new secret name is one new row in
:data:`_SECRET_NAMES`, a new plain name one new row in :data:`_PLAIN_NAMES`.
Each row is checked in four spellings, with an SNMP prefix too.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.broadcast.events import (
    is_secret_param_name,
    is_secret_param_name_lowercased,
    scrub_broadcast_params,
    scrub_secret_named_values,
)
from meho_backplane.connectors.keycloak.ops_read import keycloak_realm_get
from meho_backplane.connectors.keycloak.ops_write_account_preview import _user_update_preview
from meho_backplane.connectors.keycloak.redaction import (
    REDACTED,
    redact_preview_fields,
    redact_secret_fields,
    redact_trace_fields,
)
from meho_backplane.connectors.pfsense.redaction_names import is_secret_name as is_pfsense_secret
from meho_backplane.operations._preview import _PARAM_REDACTED, _scrub_secret_param_keys
from meho_backplane.operations._secret_values import hide_secret_values, reveal_hidden_values
from meho_backplane.redaction.secret_fields import (
    scrub_secret_fields,
    scrub_secret_fields_in_text,
)
from meho_backplane.redaction.url_secret import is_secret_query_name
from meho_backplane.session_store.secret_check import check_part

_MARK = "[REDACTED:param_name]"
_VALUE = "made-up-value-123"

#: Secret names, in snake case. Each is hidden in every spelling.
_SECRET_NAMES = [
    "auth_key",
    "priv_key",
    "community_string",
    "tacacs_key",
    "radius_key",
    "hmac_key",
    "signing_key",
    "shared_key",
    "pre_shared_key",
    "license_key",
    "authentication_key",
    "privacy_key",
    "pass_phrase",
    "store_key",
    "application_key",
    "snmp_community",
]

#: Names that end in ``ref`` and are hidden, in snake case. Main hides the
#: first ones already, and so must every later change; the last ones end in
#: a name this table adds. Each is hidden in four spellings
#: (``password_ref``, ``passwordRef``, ``password-ref``, ``PASSWORD_REF``).
_SECRET_REF_NAMES = [
    "password_ref",
    "password_secret_ref",
    "passphrase_ref",
    "private_key_ref",
    "api_key_ref",
    "license_key_ref",
    "kubeconfig_ref",
    "auth_key_ref",
    "snmp_auth_key_ref",
    "shared_secret_ref",
]

#: Plain names, in snake case. Each stays visible in every spelling.
_PLAIN_NAMES = [
    "auth_key_id",
    "key_id",
    "community_id",
    "public_key",
    "secret_ref",
    "secret_key_ref",
    "connection_string_ref",
    "primary_key",
    "partition_key",
    "sort_key",
    "authentication_key_id",
]


def _spellings(snake: str) -> list[str]:
    """``auth_key``, ``authKey``, ``auth-key``, ``AUTHKEY``, ``snmp_auth_key``, ``snmpAuthKey``."""
    words = snake.split("_")
    camel = words[0] + "".join(word.title() for word in words[1:])
    return [
        snake,
        camel,
        snake.replace("_", "-"),
        snake.replace("_", "").upper(),
        f"snmp_{snake}",
        "snmp" + "".join(word.title() for word in words),
    ]


def _four_spellings(snake: str) -> list[str]:
    """``password_ref``, ``passwordRef``, ``password-ref``, ``PASSWORD_REF``."""
    words = snake.split("_")
    camel = words[0] + "".join(word.title() for word in words[1:])
    return [snake, camel, snake.replace("_", "-"), snake.upper()]


_ALL_SECRET = [spelling for name in _SECRET_NAMES for spelling in _spellings(name)]
_ALL_SECRET += [spelling for name in _SECRET_REF_NAMES for spelling in _four_spellings(name)]
_ALL_PLAIN = [spelling for name in _PLAIN_NAMES for spelling in _spellings(name)[:4]]
# Older names, hidden before this table: the capital spelling is one word.
_ALL_SECRET += ["sharedSecret", "shared_secret", "shared-secret", "PassPhrase", "PASSPHRASE"]
_ALL_PLAIN += ["snmpCommunityId", "snmp_public_key", "snmpKeyId"]


@pytest.mark.parametrize("name", _ALL_SECRET)
def test_secret_name_is_hidden_in_every_place(name: str) -> None:
    node = {
        "top": {name: _VALUE},
        "rows": [{"id": 1, name: _VALUE}, {"deep": [{name: {"inner": _VALUE}}]}],
        name: _VALUE,
    }

    def hidden(mark: str) -> dict[str, Any]:
        return {
            "top": {name: mark},
            "rows": [{"id": 1, name: mark}, {"deep": [{name: mark}]}],
            name: mark,
        }

    assert is_secret_param_name(name) is True
    assert scrub_secret_named_values(node) == (hidden(_MARK), True)  # result
    assert hide_secret_values("any.op", node) == hidden(_MARK)  # audit row
    assert scrub_broadcast_params(node) == (hidden(_MARK), True)  # broadcast
    assert _scrub_secret_param_keys(node) == hidden(_PARAM_REDACTED)  # preview
    assert redact_preview_fields(node) == hidden(REDACTED)  # Keycloak preview
    assert redact_trace_fields(node) == hidden(REDACTED)  # Keycloak trace
    assert redact_secret_fields(node) == node  # Keycloak result: hidden later, can be revealed
    assert scrub_secret_fields(node, hide_numbers=True) == hidden(_MARK)  # trace
    assert _VALUE not in scrub_secret_fields_in_text(f'{{"{name}": "{_VALUE}"}}')  # error text
    assert is_secret_query_name(name) is True  # a link's query
    assert is_pfsense_secret(name) is True  # pfSense config read
    assert check_part(json.dumps(node).encode()).unclear is True  # uploaded session file
    assert check_part(json.dumps({"line": f'"{name}": "{_VALUE}"'}).encode()).unclear is True


@pytest.mark.parametrize("name", _ALL_SECRET)
def test_secret_name_is_hidden_once_its_case_is_lost(name: str) -> None:
    assert is_secret_param_name_lowercased(name.lower()) is True


@pytest.mark.parametrize("name", _ALL_PLAIN)
def test_plain_name_stays_in_every_place(name: str) -> None:
    node = {"top": {name: "abc"}, "list": [{name: "abc"}]}
    text = f'{{"{name}": "abc"}}'
    assert is_secret_param_name(name) is False
    assert scrub_secret_named_values(node) == (node, False)
    assert hide_secret_values("any.op", node) == node
    assert scrub_broadcast_params(node) == (node, False)
    assert _scrub_secret_param_keys(node) == node
    assert redact_secret_fields(node) == node
    assert redact_preview_fields(node) == node
    assert scrub_secret_fields(node, hide_numbers=True) == node
    assert scrub_secret_fields_in_text(text) == text


#: A ``community`` in an SNMP place, with the path to it.
_SNMP_PLACES: list[tuple[Any, tuple[Any, ...]]] = [
    ({"snmp_version": "v2c", "community": _VALUE}, ("community",)),
    ({"SnmpPort": 161, "Community": _VALUE}, ("Community",)),
    ({"snmp": {"community": _VALUE}}, ("snmp", "community")),
    ({"SNMP": {"v2c": {"COMMUNITY": _VALUE}}}, ("SNMP", "v2c", "COMMUNITY")),
    ({"snmpCommunities": [{"community": _VALUE}]}, ("snmpCommunities", 0, "community")),
    ({"snmp-targets": [[{"community": _VALUE}]]}, ("snmp-targets", 0, 0, "community")),
    ({"fields": [{"value": _VALUE, "name": "community"}]}, ("fields", 0, "value")),
    ({"settings": [{"name": "COMMUNITY", "value": _VALUE}]}, ("settings", 0, "value")),
]


def _at(node: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        node = node[step]
    return node


@pytest.mark.parametrize(("node", "path"), _SNMP_PLACES)
def test_community_in_an_snmp_place_is_hidden_in_every_place(
    node: Any, path: tuple[Any, ...]
) -> None:
    assert _at(scrub_secret_named_values(node)[0], path) == _MARK
    assert _at(hide_secret_values("any.op", node), path) == _MARK
    assert _at(scrub_broadcast_params(node)[0], path) == _MARK
    assert _at(_scrub_secret_param_keys(node), path) == _PARAM_REDACTED
    assert _at(scrub_secret_fields(node), path) == _MARK
    assert check_part(json.dumps(node).encode()).unclear is True  # uploaded session file


def test_name_value_pair_community_is_hidden_in_text() -> None:
    text = f'[{{"name": "community", "value": "{_VALUE}"}}]'
    assert _VALUE not in scrub_secret_fields_in_text(text)


@pytest.mark.parametrize(
    "node",
    [
        {"community": "gardeners", "members": 4},
        {"groups": {"community": "gardeners"}},
        {"teams": [{"community": "gardeners"}]},
        {"snmp": {"community_id": "c-1"}},
        [{"name": "community_id", "value": "c-1"}],
    ],
)
def test_community_outside_an_snmp_place_stays(node: Any) -> None:
    assert check_part(json.dumps(node).encode()).unclear is False
    assert scrub_secret_named_values(node) == (node, False)
    assert scrub_broadcast_params({"params": node}) == ({"params": node}, False)
    assert _scrub_secret_param_keys(node) == node
    assert scrub_secret_fields(node) == node


def test_keycloak_preview_scrub_keeps_flags_under_a_secret_name() -> None:
    node = {"resetPasswordAllowed": True, "passwordPolicy": "length(8)"}
    assert redact_preview_fields(node) == {"resetPasswordAllowed": True, "passwordPolicy": REDACTED}


def test_keycloak_preview_scrub_shows_only_the_name_it_is_told_to_show() -> None:
    node = {
        "passwordPolicy": "length(8) and digits(1)",
        "smtpServer": {"password": _VALUE, "passwordPolicy": "length(8)"},
        "attributes": {"PasswordPolicy": "x", "snmpAuthKey": _VALUE},
        "passwordPolicyList": [_VALUE],
    }
    shown = redact_preview_fields(node, show=frozenset({"passwordPolicy"}))
    assert shown["passwordPolicy"] == "length(8) and digits(1)"
    assert shown["smtpServer"] == {"password": REDACTED, "passwordPolicy": "length(8)"}
    # Another spelling, another name and another value type are not shown.
    assert shown["attributes"] == {"PasswordPolicy": REDACTED, "snmpAuthKey": REDACTED}
    assert shown["passwordPolicyList"] == REDACTED
    assert redact_preview_fields(
        {"passwordPolicy": {"a": _VALUE}}, show=frozenset({"passwordPolicy"})
    ) == {"passwordPolicy": REDACTED}
    assert redact_trace_fields(node)["passwordPolicy"] == REDACTED


class _RealmReader:
    """Stands in for the connector: the Admin REST read returns *realm*."""

    def __init__(self, realm: dict[str, Any]) -> None:
        self._realm = realm

    async def _get_admin_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._realm


async def test_keycloak_read_hides_shared_names_by_default_and_reveal_returns_them() -> None:
    realm = {
        "realm": "demo",
        "resetPasswordAllowed": True,
        "passwordPolicy": "length(8)",
        "attributes": {"snmpAuthKey": _VALUE},
        "smtpServer": {"host": "smtp.example.invalid", "password": _VALUE},
    }
    target = SimpleNamespace(name="kc", extras={"managed_realm": "demo"})
    raw = await keycloak_realm_get(_RealmReader(realm), None, target, {})  # type: ignore[arg-type]

    # The Keycloak list hides the SMTP password at the source, for good.
    assert raw["realm"]["smtpServer"]["password"] == REDACTED
    # The shared names stay in the result, so the dispatcher can keep them.
    assert raw["realm"]["passwordPolicy"] == "length(8)"
    assert raw["realm"]["attributes"]["snmpAuthKey"] == _VALUE

    shown = hide_secret_values("keycloak.realm.get", raw)
    assert shown["realm"]["passwordPolicy"] == _MARK
    assert shown["realm"]["attributes"]["snmpAuthKey"] == _MARK
    assert shown["realm"]["resetPasswordAllowed"] is True

    revealed = reveal_hidden_values(raw, raw, shown)
    assert revealed["realm"]["passwordPolicy"] == "length(8)"
    assert revealed["realm"]["attributes"]["snmpAuthKey"] == _VALUE
    assert revealed["realm"]["smtpServer"]["password"] == REDACTED


async def test_keycloak_user_update_preview_hides_shared_names() -> None:
    params = {"realm": "demo", "changes": {"email": "j@example.com", "snmpAuthKey": _VALUE}}
    ctx = SimpleNamespace(params=params, connector_instance=None, target=None)
    card = await _user_update_preview(ctx)  # type: ignore[arg-type]
    assert card is not None
    assert card["changes"] == {"email": "j@example.com", "snmpAuthKey": REDACTED}


@pytest.mark.parametrize(
    "node",
    [
        {"community_strings": [_VALUE]},
        {"snmpAuthKey": {"inner": _VALUE}},
        {"password": [{"note": f"see {_VALUE}"}]},
        {"rows": [{"shared_secret": [[_VALUE]]}]},
    ],
)
def test_uploaded_value_inside_a_list_or_object_goes_to_quarantine(node: Any) -> None:
    """No plain string sits under a secret name here: only the inner value can trip the check."""
    assert check_part(json.dumps(node).encode()).unclear is True


@pytest.mark.parametrize(
    "node",
    [
        {"password": {"enabled": True, "length": 12}},
        {"community_strings": []},
        {"snmpAuthKey": ["[REDACTED:api_key]"]},
    ],
)
def test_uploaded_list_or_object_without_a_value_stays_clear(node: Any) -> None:
    assert check_part(json.dumps(node).encode()).unclear is False

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SNMP keys, passphrases and other key-word names are hidden in every place.

One name rule (``broadcast.events.is_secret_param_name``) serves the normal
answer and the audit row, the approval preview, the broadcast params and the
text/trace scrub. Plain identifiers that only end in "key" stay visible.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.broadcast.events import (
    is_secret_param_name,
    scrub_broadcast_params,
    scrub_secret_named_values,
)
from meho_backplane.operations._preview import _PARAM_REDACTED, _scrub_secret_param_keys
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.secret_fields import scrub_secret_fields

_MARK = "[REDACTED:param_name]"
_VALUE = "made-up-value-123"

_SECRET_NAMES = [
    ("authenticationKey", "authentication_key", "authentication-key"),
    ("privacyKey", "privacy_key", "privacy-key"),
    ("passphrase", "pass_phrase", "pass-phrase"),
    ("PassPhrase", "PASSPHRASE", "Pass-Phrase"),
    ("storeKey", "store_key", "store-key"),
    ("applicationKey", "application_key", "application-key"),
    ("sharedSecret", "shared_secret", "shared-secret"),
    ("snmpCommunity", "snmp_community", "snmp-community"),
]
_ALL_SECRET_NAMES = [name for row in _SECRET_NAMES for name in row]

_PLAIN_NAMES = [
    "primaryKey",
    "primary_key",
    "primary-key",
    "partitionKey",
    "partition_key",
    "sortKey",
    "sort-key",
    "keyId",
    "key_id",
    "publicKey",
    "public_key",
    "authenticationKeyId",
]


@pytest.mark.parametrize("name", _ALL_SECRET_NAMES)
def test_each_new_name_is_secret(name: str) -> None:
    assert is_secret_param_name(name) is True


@pytest.mark.parametrize("name", _PLAIN_NAMES)
def test_plain_identifiers_are_not_secret(name: str) -> None:
    assert is_secret_param_name(name) is False


def _nest(name: str) -> dict[str, Any]:
    return {
        "top": {name: _VALUE},
        "rows": [{"id": 1, name: _VALUE}, {"deep": [{name: {"inner": _VALUE}}]}],
        name: _VALUE,
    }


def _hidden(name: str, mark: str = _MARK) -> dict[str, Any]:
    return {
        "top": {name: mark},
        "rows": [{"id": 1, name: mark}, {"deep": [{name: mark}]}],
        name: mark,
    }


@pytest.mark.parametrize("name", _ALL_SECRET_NAMES)
def test_result_and_audit_hide_each_name_at_every_depth(name: str) -> None:
    # The normal answer and the audit copy both use this walk.
    shown, found = scrub_secret_named_values(_nest(name))
    assert found is True
    assert shown == _hidden(name)
    assert hide_secret_values("any.op", _nest(name)) == _hidden(name)


@pytest.mark.parametrize("name", _ALL_SECRET_NAMES)
def test_approval_preview_hides_each_name(name: str) -> None:
    assert _scrub_secret_param_keys(_nest(name)) == _hidden(name, _PARAM_REDACTED)


@pytest.mark.parametrize("name", _ALL_SECRET_NAMES)
def test_broadcast_hides_each_name(name: str) -> None:
    scrubbed, found = scrub_broadcast_params(_nest(name))
    assert found is True
    assert scrubbed == _hidden(name)


@pytest.mark.parametrize("name", _ALL_SECRET_NAMES)
def test_trace_and_text_scrub_hide_each_name(name: str) -> None:
    assert scrub_secret_fields(_nest(name)) == _hidden(name)
    text = scrub_secret_fields(f'{{"{name}": "{_VALUE}"}}')
    assert _VALUE not in text


@pytest.mark.parametrize("name", _PLAIN_NAMES)
def test_plain_identifiers_stay_in_every_place(name: str) -> None:
    node = {"top": {name: "abc"}, "list": [{name: "abc"}]}
    assert scrub_secret_named_values(node) == (node, False)
    assert _scrub_secret_param_keys(node) == node
    assert scrub_broadcast_params(node) == (node, False)
    assert scrub_secret_fields(node) == node


_SNMP_NODE = {"snmp_version": "v2c", "community": _VALUE, "port": 161}


def test_community_next_to_an_snmp_field_is_hidden_in_every_place() -> None:
    want = {"snmp_version": "v2c", "community": _MARK, "port": 161}
    assert scrub_secret_named_values(_SNMP_NODE)[0] == want
    assert _scrub_secret_param_keys(_SNMP_NODE) == {**want, "community": _PARAM_REDACTED}
    assert scrub_broadcast_params(_SNMP_NODE)[0] == want
    assert scrub_secret_fields(_SNMP_NODE) == want
    assert (
        scrub_secret_named_values({"SnmpPort": 161, "community": _VALUE})[0]["community"] == _MARK
    )


def test_community_without_an_snmp_field_stays() -> None:
    node = {"community": "gardeners", "members": 4}
    assert scrub_secret_named_values(node) == (node, False)
    assert scrub_secret_fields(node) == node

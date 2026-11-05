# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The flight recorder stores Azure answers with every key and password field masked (#5212).

Two layers are pinned here, with no event loop and no HTTP:

* :func:`~meho_backplane.connectors.azure.trace_mask.mask_answer`, the walk
  that replaces the value of each name in ``SECRET_FIELDS``;
* the connector hook ``AzureConnector.trace_response_body``, which every
  Azure family reaches through ``send``, the Blob service caller included.

The stored trace of a real traced call is pinned in
``test_connectors_azure_network.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.azure import trace_mask
from meho_backplane.connectors.azure.connector import TRACE_BODY_WITHHELD, AzureConnector
from meho_backplane.connectors.azure.storage_reads import _StorageCaller
from meho_backplane.connectors.azure.trace_mask import LEFT_OUT, SECRET_FIELDS, mask_answer

#: The key and password fields Azure puts into its answers (the ticket's list).
AZURE_SECRET_NAMES = (
    "serviceKey",
    "authorizationKey",
    "activationKey",
    "sharedKey",
    "adminPassword",
    "serverAppSecret",
    "secret",
    "keys",
    "connectionString",
)


def _circuit_answer() -> dict[str, Any]:
    return {
        "value": [
            {
                "name": "er-primary",
                "properties": {
                    "serviceKey": "made-up-service-key",
                    "authorizations": [
                        {"name": "auth-1", "properties": {"authorizationKey": "made-up-auth-key"}}
                    ],
                    "provisioningState": "Succeeded",
                },
            }
        ]
    }


def test_secret_fields_do_not_repeat_the_shared_rule() -> None:
    assert [name for name in SECRET_FIELDS if is_secret_param_name(name)] == []


def test_every_azure_secret_name_is_masked_by_the_mask_or_the_shared_rule() -> None:
    missed = [
        name
        for name in AZURE_SECRET_NAMES
        if name not in SECRET_FIELDS and not is_secret_param_name(name)
    ]
    assert missed == []


def test_mask_reaches_fields_inside_nested_lists() -> None:
    masked = mask_answer(_circuit_answer())

    row = masked["value"][0]["properties"]
    assert row["serviceKey"] == LEFT_OUT
    assert row["authorizations"][0]["properties"]["authorizationKey"] == LEFT_OUT
    assert row["authorizations"][0]["name"] == "auth-1"
    assert row["provisioningState"] == "Succeeded"


def test_mask_replaces_a_whole_object_or_list_value() -> None:
    assert mask_answer({"keys": [{"keyName": "key1", "value": "K"}]}) == {"keys": LEFT_OUT}
    assert mask_answer({"parameters": {"adminUser": {"value": "x"}}}) == {"parameters": LEFT_OUT}


def test_mask_matches_names_in_any_case() -> None:
    assert mask_answer({"ServiceKey": "S", "PRIMARYKEY": "P"}) == {
        "ServiceKey": LEFT_OUT,
        "PRIMARYKEY": LEFT_OUT,
    }


def test_mask_handles_a_top_level_list_and_scalars() -> None:
    assert mask_answer([{"activationKey": "A"}, "x", 3, None]) == [
        {"activationKey": LEFT_OUT},
        "x",
        3,
        None,
    ]


def test_hook_masks_a_json_answer() -> None:
    traced = AzureConnector().trace_response_body(httpx.Response(200, json=_circuit_answer()))

    text = json.dumps(traced)
    assert "made-up-service-key" not in text
    assert "made-up-auth-key" not in text
    assert LEFT_OUT in text
    assert "er-primary" in text


def test_hook_keeps_an_azure_error_answer() -> None:
    error = {"error": {"code": "AuthorizationFailed", "message": "Example message."}}

    assert AzureConnector().trace_response_body(httpx.Response(403, json=error)) == error


def test_hook_returns_none_for_empty_or_non_json_answers() -> None:
    connector = AzureConnector()

    assert connector.trace_response_body(httpx.Response(204)) is None
    bad_gateway = httpx.Response(502, text="<html>Bad gateway</html>")
    assert connector.trace_response_body(bad_gateway) is None


def test_hook_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(body: Any) -> Any:
        raise RuntimeError("walk failed")

    monkeypatch.setattr(trace_mask, "mask_answer", _broken)

    traced = AzureConnector().trace_response_body(httpx.Response(200, json={"serviceKey": "S"}))

    assert traced == TRACE_BODY_WITHHELD


def test_storage_caller_trace_uses_the_azure_mask() -> None:
    caller = _StorageCaller(AzureConnector(), {})

    traced = caller._trace_response_body(object(), httpx.Response(200, json=_circuit_answer()))

    assert "made-up-service-key" not in json.dumps(traced)

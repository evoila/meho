# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secrets in JSON without spaces, in maps a bare brace opens, and in nested inputs.

Three more forms of cloud template YAML hold a secret: JSON written without
spaces (``{"password":"x"}``), a map that a bare ``{`` opens and that puts
``encrypted: true, default: x`` on one row, and a value for an input nested
in an object input (``inputs.login.pin``) the template marks ``encrypted``.
Reads hide the first two, the approval card hides them, and writes refuse
them. ``blueprint.validate`` refuses the third before it calls the vendor.
All values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vmware_shared.vcfa_template_inputs import (
    encrypted_inputs_schema,
    refuse_secret_input_values,
)
from meho_backplane.connectors.vmware_shared.vcfa_yaml_secrets import scrub_template_yaml
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from tests._vcfa_provisioning_support import (
    BASE_URL,
    mount_logins,
    run,
    seed_target,
    wire_connector,
)

_BP = "/blueprint/api/blueprints"
_BP_1 = f"{_BP}/bp-1"
_VALIDATE = "/blueprint/api/blueprint-validation"
_MARKER = "[REDACTED:param_name]"
_COMPACT = "planted-compact-12"
_BARE = "planted-bare-13"
_LITERAL = "planted-literal-14"

#: A template written as JSON without spaces.
_COMPACT_JSON = f'{{"formatVersion":1,"resources":{{"vm":{{"password":"{_COMPACT}"}}}}}}'
#: An encrypted input whose bare ``{`` puts the mark and the default on one row.
_BARE_INPUT = (
    f"inputs:\n  loginValue: {{\n    type: string, encrypted: true, default: {_BARE}\n  }}\n"
)
_BOTH = f"formatVersion: 1\n{_BARE_INPUT}resources:\n  vm: {_COMPACT_JSON}\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_COMPACT_JSON, f"'{_MARKER}'"),
        (f'x: {{"password":"{_COMPACT}"}}\nend: 1', f"x: '{_MARKER}'\nend: 1"),
        (
            f'{{\n"inputs":{{\n"lv":{{\n"encrypted":true,\n"default":"{_COMPACT}"\n}}\n}}\n}}',
            f'{{\n"inputs":{{\n"lv": \'{_MARKER}\'\n}}\n}}',
        ),
        (
            f'{{\n  "formatVersion": 1,\n  "password":"{_COMPACT}",\n  "end": 1\n}}',
            f'{{\n  "formatVersion": 1,\n  "password": \'{_MARKER}\'\n  "end": 1\n}}',
        ),
        (_BARE_INPUT, f"inputs:\n  loginValue: '{_MARKER}'\n"),
        (
            f"inputs:\n  lv: {{\n    type: string, encrypted: true,\n    default: {_BARE}\n  }}\n",
            f"inputs:\n  lv: '{_MARKER}'\n",
        ),
        (
            f'{{\n  "inputs": {{\n    "lv": {{\n      "encrypted": true, "default": "{_BARE}"\n'
            "    }\n  }\n}",
            f'{{\n  "inputs": {{\n    "lv": \'{_MARKER}\'\n  }}\n}}',
        ),
        (f"- {{\n  encrypted: true, default: {_BARE}\n}}\nend: 1", f"- '{_MARKER}'\nend: 1"),
        (f"password: {{\n  a: {_BARE}\n}}\nend: 1", f"password: '{_MARKER}'\nend: 1"),
        (f"x: [\n  {{apiToken: {_BARE}}}\n]\nend: 1", f"x: [\n  '{_MARKER}'\n]\nend: 1"),
    ],
    ids=[
        "one-line-json-without-spaces",
        "value-json-without-spaces",
        "multi-line-json-without-spaces",
        "multi-line-json-row-without-spaces",
        "bare-brace-mark-and-default-on-one-row",
        "bare-brace-default-on-its-own-row",
        "multi-line-json-row-with-both",
        "list-item-bare-brace",
        "secret-key-bare-brace",
        "bare-list",
    ],
)
def test_scrub_hides_compact_json_and_bare_brace_maps(source: str, expected: str) -> None:
    text, count = scrub_template_yaml(source)
    assert (text, count) == (expected, 1)
    for planted in (_COMPACT, _BARE):
        assert planted not in text


@pytest.mark.parametrize(
    "source",
    [
        '{"formatVersion":1,"inputs":{"lv":{"type":"string","encrypted":true}}}',
        '{\n  "formatVersion": 1,\n  "inputs": {\n    "lv": {\n      "encrypted": true\n'
        "    }\n  }\n}",
        "inputs:\n  lv: {\n    type: string, encrypted: true, default: '${secret.lv}'\n  }\n",
        "x: {\n  url: http://a:1/b, a: [\n    1, 2\n  ]\n}\n",
    ],
    ids=["json-without-spaces", "multi-line-json", "reference", "nested-bare-brackets"],
)
def test_scrub_keeps_compact_json_and_bare_brace_maps_without_a_secret(source: str) -> None:
    assert scrub_template_yaml(source) == (source, 0)


_NESTED_TEMPLATE = (
    "formatVersion: 1\n"
    "inputs:\n"
    "  login:\n"
    "    type: object\n"
    "    properties:\n"
    "      user: {type: string}\n"
    "      pin: {type: string, encrypted: true}\n"
    "  logins:\n"
    "    type: array\n"
    "    items:\n"
    "      type: object\n"
    "      properties:\n"
    "        pin: {type: string, encrypted: 'true'}\n"
    "  size: {type: string}\n"
    "resources: {}\n"
)
_NESTED_SCHEMA = {
    "properties": {
        "login": {"properties": {"pin": {"encrypted": True}}},
        "logins": {"items": {"properties": {"pin": {"encrypted": True}}}},
    }
}


def test_encrypted_inputs_schema_reads_nested_definitions() -> None:
    assert encrypted_inputs_schema(_NESTED_TEMPLATE) == _NESTED_SCHEMA


def test_encrypted_inputs_schema_stops_at_an_alias_that_points_back_up() -> None:
    content = (
        "inputs:\n  login: &login\n    properties:\n      again: *login\n"
        "      pin: {encrypted: true}\n"
    )
    assert encrypted_inputs_schema(content) == {
        "properties": {"login": {"properties": {"pin": {"encrypted": True}}}}
    }


@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        ({"login": {"user": "<user>", "pin": _LITERAL}}, "login.pin"),
        ({"logins": [{"pin": "${secret.a}"}, {"pin": _LITERAL}]}, r"logins\[1\].pin"),
        ({"login": {"user": "<user>", "pin": "${secret.pin}"}, "logins": [{"pin": None}]}, None),
    ],
    ids=["object", "list-item", "references"],
)
def test_refuse_secret_input_values_in_nested_inputs(
    inputs: dict[str, object], named: str | None
) -> None:
    if named is None:
        refuse_secret_input_values(inputs, _NESTED_SCHEMA)
        return
    with pytest.raises(ValueError, match=named) as info:
        refuse_secret_input_values(inputs, _NESTED_SCHEMA)
    assert _LITERAL not in str(info.value)


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


async def test_blueprint_get_hides_compact_json_and_bare_brace_maps(
    vcfa: VcfAutomationConnector,
) -> None:
    stored = {"id": "bp-1", "name": "<template-name>", "content": _BOTH}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=stored)
        mock.get(f"{_BP_1}/inputs-schema").respond(200, json={"type": "object"})
        result = await run("vcfa.tenant.blueprint.get", {"id": "bp-1"})
    assert result["result"]["secrets_replaced"] == 2, result
    for planted in (_COMPACT, _BARE):
        assert planted not in str(result), planted
    content = result["result"]["blueprint"]["content"]
    assert f"  loginValue: '{_MARKER}'" in content
    assert f"  vm: '{_MARKER}'" in content


@pytest.mark.parametrize("content", [_COMPACT_JSON, _BARE_INPUT], ids=["compact", "bare"])
async def test_create_and_update_refuse_compact_json_and_bare_brace_maps(
    vcfa: VcfAutomationConnector, content: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        vendor = mock.route(path__startswith=_BP).respond(200, json={"content": []})
        created = await run(
            "vcfa.tenant.blueprint.create",
            {"project_id": "p-1", "name": "<template-name>", "content": content},
        )
        updated = await run("vcfa.tenant.blueprint.update", {"id": "bp-1", "content": content})
    for result in (created, updated):
        assert result["status"] == "error", result
        assert "1 value(s) that MEHO treats as a secret" in str(result)
        for planted in (_COMPACT, _BARE):
            assert planted not in str(result)
    assert not vendor.called


@pytest.mark.parametrize("content", [_COMPACT_JSON, _BARE_INPUT], ids=["compact", "bare"])
async def test_approval_card_hides_compact_json_and_bare_brace_maps(
    vcfa: VcfAutomationConnector, content: str
) -> None:
    params = {"project_id": "p-1", "name": "<template-name>", "content": content}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        write = mock.post(_BP).respond(201)
        result = await run("vcfa.tenant.blueprint.create", params, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not write.called
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(ApprovalRequest))).scalar_one()
    for planted in (_COMPACT, _BARE):
        assert planted not in str(row.proposed_effect)
    preview = row.proposed_effect["preview"]
    assert preview["secret_values_hidden"] == 1
    assert "refuses this call" in preview["refused"]
    assert preview["content"] == scrub_template_yaml(content)[0]


_VENDOR_SCHEMA = {
    "type": "object",
    "properties": {
        "login": {
            "type": "object",
            "properties": {
                "user": {"type": "string"},
                "pin": {"type": "string", "encrypted": True},
            },
        },
        "size": {"type": "string"},
    },
}


@pytest.mark.parametrize(
    ("params", "schema_path"),
    [
        ({"content": _NESTED_TEMPLATE}, None),
        ({"blueprint_id": "bp-1"}, f"{_BP_1}/inputs-schema"),
        ({"blueprint_id": "bp-1", "blueprint_version": "2"}, f"{_BP_1}/versions/2/inputs-schema"),
    ],
    ids=["content", "stored-draft", "stored-version"],
)
async def test_validate_refuses_a_value_for_a_nested_encrypted_input(
    vcfa: VcfAutomationConnector, params: dict[str, str], schema_path: str | None
) -> None:
    inputs = {"size": "small", "login": {"user": "<user>", "pin": _LITERAL}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        if schema_path is not None:
            schema = mock.get(schema_path).respond(200, json=_VENDOR_SCHEMA)
        post = mock.post(_VALIDATE).respond(200, json={"valid": True})
        result = await run(
            "vcfa.tenant.blueprint.validate", {"project_id": "p-1", "inputs": inputs, **params}
        )
    assert result["status"] == "error", result
    assert "login.pin" in str(result)
    assert _LITERAL not in str(result)
    assert not post.called
    if schema_path is not None:
        assert schema.called


async def test_validate_sends_a_reference_for_a_nested_encrypted_input(
    vcfa: VcfAutomationConnector,
) -> None:
    inputs = {"size": "small", "login": {"user": "<user>", "pin": "${secret.pin}"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_VALIDATE).respond(200, json={"valid": True})
        result = await run(
            "vcfa.tenant.blueprint.validate",
            {"project_id": "p-1", "content": _NESTED_TEMPLATE, "inputs": inputs},
        )
    assert result["result"]["valid"] is True, result
    assert post.called
    assert "${secret.pin}" in str(post.calls.last.request.content)

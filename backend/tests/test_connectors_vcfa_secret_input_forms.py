# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret values that run over lines, and secret inputs the template marks encrypted.

Two more forms of cloud template YAML hold a secret: a flow map that runs
over several lines (``lv: {encrypted: true,`` then ``default: x}``) and a
cloud-init ``chpasswd`` map nested in a flow map. Reads hide them, the
approval card hides them, and writes refuse them. ``blueprint.validate``
also refuses a value for an input the template marks ``encrypted: true``,
in the YAML it gets or in the inputs schema of a stored template. All
values are made up.
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
_SPLIT = "planted-split-8"
_NESTED = "planted-nested-9"
_ESCAPED = "planted-escaped-10"
_LITERAL = "planted-literal-11"

#: An encrypted input whose flow map runs over two lines.
_SPLIT_INPUT = f"inputs:\n  loginValue: {{type: string, encrypted: true,\n    default: {_SPLIT}}}\n"
#: A cloud-init ``chpasswd`` map nested in a flow map.
_NESTED_CHPASSWD = (
    "resources:\n  vm:\n    properties:\n      cloudConfig: |\n        #cloud-config\n"
    f"        {{chpasswd: {{list: 'root:{_NESTED}', expire: false}}}}\n"
)
_BOTH = "formatVersion: 1\n" + _SPLIT_INPUT + _NESTED_CHPASSWD


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_SPLIT_INPUT, f"inputs:\n  loginValue: '{_MARKER}'\n"),
        (
            f"inputs:\n  lv: {{type: string, encrypted: true,\ndefault: {_SPLIT}}}\nend: 1",
            f"inputs:\n  lv: '{_MARKER}'\nend: 1",
        ),
        (
            f"inputs: {{lv: {{type: string,\n  encrypted: true, default: {_SPLIT}}}}}",
            f"inputs: '{_MARKER}'",
        ),
        (
            _NESTED_CHPASSWD,
            "resources:\n  vm:\n    properties:\n      cloudConfig: |\n        #cloud-config\n"
            f"        '{_MARKER}'\n",
        ),
        (
            f"chpasswd: {{expire: false,\n  list: [root:{_NESTED}]}}\nend: 1",
            f"chpasswd: '{_MARKER}'\nend: 1",
        ),
        (f"x: {{password:\n  {_SPLIT}}}\nend: 1", f"x: '{_MARKER}'\nend: 1"),
        (
            f"x:\n  - {{name: a,\n     apiToken: {_SPLIT}}}\n  - {{name: b}}",
            f"x:\n  - '{_MARKER}'\n  - {{name: b}}",
        ),
        (
            f'cloudConfig: "#cloud-config\\nchpasswd:\\n  list: |\\n    root:{_ESCAPED}"\nend: 1',
            f"cloudConfig: '{_MARKER}'\nend: 1",
        ),
        (f'files:\n  - "#cloud-config\\npassword: {_ESCAPED}"', f"files:\n  - '{_MARKER}'"),
    ],
    ids=[
        "input-over-two-lines",
        "input-continued-at-column-0",
        "inputs-map-over-lines",
        "nested-chpasswd",
        "chpasswd-over-lines",
        "key-then-value-line",
        "list-item-over-lines",
        "escaped-newlines",
        "escaped-list-item",
    ],
)
def test_scrub_hides_a_secret_that_runs_over_lines_or_nests(source: str, expected: str) -> None:
    text, count = scrub_template_yaml(source)
    assert (text, count) == (expected, 1)
    for planted in (_SPLIT, _NESTED, _ESCAPED):
        assert planted not in text


@pytest.mark.parametrize(
    "source",
    [
        "inputs: {a: {type: string, encrypted: true}, b: {type: string, default: small}}\n",
        "x: {chpasswd: {expire: false}, list: [a, b]}\n",
        "inputs:\n  lv: {type: string, encrypted: true,\n    default: '${secret.lv}'}\n",
        'password: "${secret.x}" # a note\n',
    ],
    ids=[
        "encrypted-and-default-in-different-maps",
        "list-outside-chpasswd",
        "reference",
        "comment",
    ],
)
def test_scrub_keeps_flow_maps_without_a_secret(source: str) -> None:
    assert scrub_template_yaml(source) == (source, 0)


@pytest.mark.parametrize(
    ("content", "encrypted"),
    [
        ("inputs:\n  pw:\n    type: string\n    encrypted: true\n  size: {type: string}\n", {"pw"}),
        ("inputs: {lv: {encrypted: 'true'}, other: {encrypted: false}}\n", {"lv"}),
        ("inputs:\n  lv: {type: string, encrypted: true,\n    default: x}\n", {"lv"}),
        ("resources: {}\n", set()),
        ("inputs: [a, b]\n", set()),
        ("a: [\n", set()),
    ],
    ids=["block", "flow-quoted-true", "over-lines", "no-inputs", "inputs-not-a-map", "bad-yaml"],
)
def test_encrypted_inputs_schema(content: str, encrypted: set[str]) -> None:
    schema = encrypted_inputs_schema(content)
    assert set(schema.get("properties", {})) == encrypted
    assert all(schema["properties"][name] == {"encrypted": True} for name in encrypted)


def test_encrypted_inputs_schema_does_not_expand_aliases() -> None:
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{name}: &{name} [{', '.join(['*' + prev] * 10)}]\n"
        for name, prev in zip("bcdefghij", "abcdefghi", strict=True)
    )
    assert encrypted_inputs_schema(bomb + "inputs: {pw: {encrypted: true}}\n") == {
        "properties": {"pw": {"encrypted": True}}
    }


_SCHEMA = {
    "properties": {
        "pw": {"type": "string", "encrypted": True},
        "size": {"type": "string"},
        "login": {
            "type": "object",
            "properties": {
                "user": {"type": "string"},
                "pin": {"type": "string", "encrypted": True},
            },
        },
    }
}


@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        ({"pw": _LITERAL}, "pw"),
        ({"pw": "${secret.pw}", "size": "small"}, None),
        ({"login": {"user": "<user>", "pin": _LITERAL}}, "login.pin"),
        ({"login": {"user": "<user>", "pin": "${input.pin}"}}, None),
        ({"apiToken": _LITERAL}, "apiToken"),
        ({"pw": None, "size": _LITERAL}, None),
    ],
    ids=["encrypted", "reference", "nested-encrypted", "nested-reference", "secret-name", "none"],
)
def test_refuse_secret_input_values(inputs: dict[str, object], named: str | None) -> None:
    if named is None:
        refuse_secret_input_values(inputs, _SCHEMA)
        return
    with pytest.raises(ValueError, match=named) as info:
        refuse_secret_input_values(inputs, _SCHEMA)
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


async def test_blueprint_get_hides_the_two_forms(vcfa: VcfAutomationConnector) -> None:
    stored = {"id": "bp-1", "name": "<template-name>", "content": _BOTH}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=stored)
        mock.get(f"{_BP_1}/inputs-schema").respond(200, json={"type": "object"})
        result = await run("vcfa.tenant.blueprint.get", {"id": "bp-1"})
    assert result["result"]["secrets_replaced"] == 2, result
    for planted in (_SPLIT, _NESTED):
        assert planted not in str(result), planted
    assert f"  loginValue: '{_MARKER}'" in result["result"]["blueprint"]["content"]


@pytest.mark.parametrize("content", [_SPLIT_INPUT, _NESTED_CHPASSWD], ids=["split", "nested"])
async def test_create_and_update_refuse_the_two_forms(
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
        for planted in (_SPLIT, _NESTED):
            assert planted not in str(result)
    assert not vendor.called


@pytest.mark.parametrize("content", [_SPLIT_INPUT, _NESTED_CHPASSWD], ids=["split", "nested"])
async def test_approval_card_hides_the_two_forms(
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
    for planted in (_SPLIT, _NESTED):
        assert planted not in str(row.proposed_effect)
    preview = row.proposed_effect["preview"]
    assert preview["secret_values_hidden"] == 1
    assert "refuses this call" in preview["refused"]
    assert preview["content"] == scrub_template_yaml(content)[0]


_ENCRYPTED_TEMPLATE = (
    "formatVersion: 1\ninputs:\n  loginValue: {type: string, encrypted: true}\nresources: {}\n"
)
_INPUTS_SCHEMA = {
    "type": "object",
    "properties": {"loginValue": {"type": "string", "encrypted": True}},
}


@pytest.mark.parametrize(
    ("params", "schema_path"),
    [
        ({"content": _ENCRYPTED_TEMPLATE}, None),
        ({"blueprint_id": "bp-1"}, f"{_BP_1}/inputs-schema"),
        ({"blueprint_id": "bp-1", "blueprint_version": "2"}, f"{_BP_1}/versions/2/inputs-schema"),
    ],
    ids=["content", "stored-draft", "stored-version"],
)
async def test_validate_refuses_a_value_for_an_encrypted_input(
    vcfa: VcfAutomationConnector, params: dict[str, str], schema_path: str | None
) -> None:
    inputs = {"size": "small", "loginValue": _LITERAL}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        if schema_path is not None:
            schema = mock.get(schema_path).respond(200, json=_INPUTS_SCHEMA)
        post = mock.post(_VALIDATE).respond(200, json={"valid": True})
        result = await run(
            "vcfa.tenant.blueprint.validate", {"project_id": "p-1", "inputs": inputs, **params}
        )
    assert result["status"] == "error", result
    assert "loginValue" in str(result)
    assert _LITERAL not in str(result)
    assert not post.called
    if schema_path is not None:
        assert schema.called


async def test_validate_sends_a_reference_for_an_encrypted_input(
    vcfa: VcfAutomationConnector,
) -> None:
    inputs = {"size": "small", "loginValue": "${secret.loginValue}"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        schema = mock.get(f"{_BP_1}/inputs-schema").respond(200, json=_INPUTS_SCHEMA)
        post = mock.post(_VALIDATE).respond(200, json={"valid": True})
        result = await run(
            "vcfa.tenant.blueprint.validate",
            {"project_id": "p-1", "blueprint_id": "bp-1", "inputs": inputs},
        )
    assert result["result"]["valid"] is True, result
    assert schema.called
    assert post.called
    assert "loginValue" in str(post.calls.last.request.content)


async def test_validate_without_inputs_reads_no_schema(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        schema = mock.get(f"{_BP_1}/inputs-schema").respond(200, json=_INPUTS_SCHEMA)
        post = mock.post(_VALIDATE).respond(200, json={"valid": True})
        result = await run(
            "vcfa.tenant.blueprint.validate", {"project_id": "p-1", "blueprint_id": "bp-1"}
        )
    assert result["result"]["valid"] is True, result
    assert not schema.called
    assert post.called

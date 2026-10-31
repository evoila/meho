# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret values in cloud template YAML (VCF Automation 9 / Aria Automation 8).

``*.blueprint.get`` hides each secret value in the YAML behind a marker
(:mod:`~meho_backplane.connectors.vmware_shared.vcfa_yaml_secrets`), and
``*.blueprint.create`` / ``*.blueprint.update`` refuse YAML that still holds
such a marker, before any call to the vendor. All values are made up; the
private key is built at run time from random bytes.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
import respx

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vmware_shared.vcfa_yaml_secrets import (
    scrub_inputs_schema,
    scrub_template_yaml,
)
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
_PASSWORD = "planted-Pa55-value-1"
_PASSWD = "planted-cloud-config-2"
_DEFAULT = "planted-default-3"
_CLOUD_INIT = "planted-cloud-init-4"
_ENCRYPTED = "planted-encrypted-5"
_MARKER = "[REDACTED:param_name]"


def _key_body() -> list[str]:
    raw = base64.b64encode(os.urandom(600)).decode()
    return [raw[i : i + 64] for i in range(0, len(raw), 64)]


def _pem(kind: str, body: list[str], indent: str) -> str:
    begin = "-----BEGIN " + kind + " PRIVATE KEY-----"
    end = "-----END " + kind + " PRIVATE KEY-----"
    return "\n".join(indent + line for line in [begin, *body, end])


def _template(body: list[str]) -> str:
    head = [
        "formatVersion: 1",
        "# a comment that stays",
        "inputs:",
        "  adminPassword:",
        "    type: string",
        "    encrypted: true",
        f"    default: {_DEFAULT}",
        "resources:",
        "  vm:",
        "    type: Cloud.vSphere.Machine",
        "    properties:",
        "      remoteAccess:",
        "        authentication: usernamePassword",
        "        username: <user>",
        f"        password: {_PASSWORD}",
        "        privateKey: |",
    ]
    tail = [
        "        sshKey: '${secret.sshKey}'",
        "      cloudConfig: |",
        "        #cloud-config",
        "        users:",
        "          - name: <user>",
        f"            passwd: {_PASSWD}",
        "            lock_passwd: false",
        "            hashed_passwd: ${input.adminPassword}",
        "",
    ]
    return "\n".join([*head, _pem("RSA", body, " " * 10), *tail])


def test_scrub_hides_passwords_and_private_keys() -> None:
    body = _key_body()
    text, count = scrub_template_yaml(_template(body))
    assert _PASSWORD not in text
    assert _PASSWD not in text
    assert _DEFAULT not in text
    assert all(line not in text for line in body)
    assert "PRIVATE KEY" not in text
    assert count == 4
    assert "    default: '[REDACTED:param_name]'" in text.split("\n")
    assert "password: '[REDACTED:param_name]'" in text
    assert "privateKey: '[REDACTED:param_name]'" in text
    assert "passwd: '[REDACTED:param_name]'" in text


def test_scrub_keeps_every_other_line() -> None:
    text, _ = scrub_template_yaml(_template(_key_body()))
    for kept in (
        "# a comment that stays",
        "  adminPassword:",
        "    type: string",
        "    encrypted: true",
        "        authentication: usernamePassword",
        "        username: <user>",
        "        sshKey: '${secret.sshKey}'",
        "            lock_passwd: false",
        "            hashed_passwd: ${input.adminPassword}",
        "      cloudConfig: |",
    ):
        assert kept in text.split("\n"), kept


def test_scrub_hides_a_private_key_under_any_name() -> None:
    body = _key_body()
    source = "files:\n  - content: |\n" + _pem("OPENSSH", body, "      ") + "\n    path: /x\n"
    text, count = scrub_template_yaml(source)
    assert count == 1
    assert text == "files:\n  - content: |\n      [REDACTED:private_key]\n    path: /x\n"


def test_scrub_hides_a_nested_secret_value() -> None:
    source = "credentials:\n  user: a\n  pass: b\nother: 1\ntoken:\n- one\n- two\nend: 2"
    text, count = scrub_template_yaml(source)
    assert count == 2
    assert text == (
        "credentials: '[REDACTED:param_name]'\nother: 1\ntoken: '[REDACTED:param_name]'\nend: 2"
    )


@pytest.mark.parametrize(
    "chpasswd",
    [
        ["chpasswd:", "  list: |", f"    root:{_CLOUD_INIT}", "    <user>:x", "  expire: false"],
        [f"chpasswd: {{list: 'root:{_CLOUD_INIT}', expire: false}}"],
        ["chpasswd:", "  expire: false", "  list:", f"    - root:{_CLOUD_INIT}"],
        ["chpasswd:", "  users:", "    - name: root", f"      password: {_CLOUD_INIT}"],
        ["Chpasswd:", "  list: |", f"    root:{_CLOUD_INIT}"],
    ],
    ids=["list-block", "flow", "list-items", "users", "upper-case"],
)
def test_scrub_hides_cloud_init_chpasswd(chpasswd: list[str]) -> None:
    lines = [
        "resources:",
        "  vm:",
        "    properties:",
        "      cloudConfig: |",
        "        #cloud-config",
        "        ssh_authorized_keys:",
        "          - ssh-ed25519 AAAAexample <user>",
        *("        " + line for line in chpasswd),
        "        ssh_pwauth: true",
        "",
    ]
    text, count = scrub_template_yaml("\n".join(lines))
    assert _CLOUD_INIT not in text
    assert count == 1
    out = text.split("\n")
    assert f"        {chpasswd[0].split(':')[0]}: '{_MARKER}'" in out
    assert "          - ssh-ed25519 AAAAexample <user>" in out
    assert "        ssh_pwauth: true" in out


def test_scrub_hides_the_values_of_an_encrypted_input() -> None:
    source = "\n".join(
        [
            "inputs:",
            "  vmPass:",
            "    type: string",
            "    encrypted: true",
            f"    default: {_ENCRYPTED}",
            "    enum:",
            f"      - {_ENCRYPTED}",
            "  size:",
            "    type: string",
            "    default: small",
            "  adminPassword:",
            "    type: string",
            f"    default: {_DEFAULT}",
            "resources: {}",
        ]
    )
    text, count = scrub_template_yaml(source)
    assert _ENCRYPTED not in text
    assert _DEFAULT not in text
    assert count == 3
    out = text.split("\n")
    for kept in ("  vmPass:", "    encrypted: true", "    default: small", "  adminPassword:"):
        assert kept in out, kept
    assert out.count(f"    default: '{_MARKER}'") == 2
    assert f"    enum: '{_MARKER}'" in out


def test_scrub_hides_a_secret_map_outside_the_inputs() -> None:
    source = f"resources:\n  vm:\n    password:\n      type: x\n      value: {_PASSWORD}\n"
    text, count = scrub_template_yaml(source)
    assert count == 1
    assert text == f"resources:\n  vm:\n    password: '{_MARKER}'\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f"remoteAccess: {{username: <user>, password: {_PASSWORD}}}",
            f"remoteAccess: '{_MARKER}'",
        ),
        (f"x:\n  - {{name: a, apiToken: {_PASSWORD}}}", f"x:\n  - '{_MARKER}'"),
        (f'{{"formatVersion": 1, "password": "{_PASSWORD}"}}', f"'{_MARKER}'"),
        (
            f"inputs:\n  password:\n    type: string\n    value: {_PASSWORD}",
            f"inputs:\n  password:\n    type: string\n    value: '{_MARKER}'",
        ),
    ],
    ids=["flow-map", "flow-list-item", "one-line-json", "input-value"],
)
def test_scrub_hides_flow_and_fixed_values(source: str, expected: str) -> None:
    assert scrub_template_yaml(source) == (expected, 1)


def test_scrub_keeps_a_flow_map_with_references() -> None:
    source = "remoteAccess: {username: <user>, password: '${secret.vmPassword}'}"
    assert scrub_template_yaml(source) == (source, 0)


def test_scrub_keeps_crlf_line_ends() -> None:
    source = f"a: 1\r\npassword:\r\n  {_PASSWORD}\r\nb: 2\r\n"
    text, count = scrub_template_yaml(source)
    assert count == 1
    assert text == f"a: 1\r\npassword: '{_MARKER}'\r\nb: 2\r\n"


def _inputs_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "adminPassword": {"type": "string", "encrypted": True, "default": _DEFAULT},
            "vmPass": {"type": "string", "encrypted": True, "default": _ENCRYPTED},
            "apiToken": {"type": "string", "const": _PASSWORD, "examples": [_PASSWORD]},
            "size": {"type": "string", "default": "small", "enum": ["small", "large"]},
            "login": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "default": "<user>"},
                    "secret": {"type": "string", "enum": [_CLOUD_INIT]},
                },
            },
            "keys": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"privateKey": {"type": "string", "example": _PASSWD}},
                },
            },
        },
        "required": ["adminPassword"],
    }


def test_scrub_inputs_schema_hides_secret_input_values() -> None:
    schema = _inputs_schema()
    copy, count = scrub_inputs_schema(schema)
    text = str(copy)
    for planted in (_DEFAULT, _ENCRYPTED, _PASSWORD, _CLOUD_INIT, _PASSWD):
        assert planted not in text, planted
    assert count == 6
    props = copy["properties"]
    assert props["adminPassword"] == {"type": "string", "encrypted": True, "default": _MARKER}
    assert props["size"] == {"type": "string", "default": "small", "enum": ["small", "large"]}
    assert props["login"]["properties"]["user"]["default"] == "<user>"
    assert copy["required"] == ["adminPassword"]
    assert _DEFAULT in str(schema), "the input is not changed"


def test_scrub_leaves_yaml_without_secrets_as_it_is() -> None:
    source = "formatVersion: 1\ninputs: {}\nresources: {}\n"
    assert scrub_template_yaml(source) == (source, 0)


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


async def test_blueprint_get_hides_secret_values(vcfa: VcfAutomationConnector) -> None:
    body = _key_body()
    stored = {"id": "bp-1", "name": "<template-name>", "content": _template(body)}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=stored)
        mock.get(f"{_BP_1}/inputs-schema").respond(200, json=_inputs_schema())
        result = await run("vcfa.tenant.blueprint.get", {"id": "bp-1"})
    out = result["result"]
    content = out["blueprint"]["content"]
    assert out["secrets_replaced"] == 4 + 6
    for planted in (_DEFAULT, _ENCRYPTED, _PASSWORD, _CLOUD_INIT, _PASSWD):
        assert planted not in str(result), planted
    assert out["inputs_schema"]["properties"]["vmPass"]["default"] == _MARKER
    assert _PASSWORD not in content
    assert all(line not in content for line in body)
    assert "PRIVATE KEY" not in content


_HIDDEN = "formatVersion: 1\npassword: '[REDACTED:param_name]'\n"


async def test_update_refuses_a_hidden_value(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(_BP_1).respond(200, json={"id": "bp-1", "content": "x: 1"})
        put = mock.put(_BP_1).respond(200, json={"id": "bp-1"})
        result = await run("vcfa.tenant.blueprint.update", {"id": "bp-1", "content": _HIDDEN})
    assert result["status"] == "error", result
    assert "hidden value" in str(result)
    assert not read.called
    assert not put.called


async def test_create_refuses_a_hidden_value(vcfa: VcfAutomationConnector) -> None:
    params = {"project_id": "p-1", "name": "<template-name>", "content": _HIDDEN}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup = mock.get(_BP).respond(200, json={"content": []})
        post = mock.post(_BP).respond(201, json={"id": "bp-1"})
        result = await run("vcfa.tenant.blueprint.create", params)
    assert result["status"] == "error", result
    assert "hidden value" in str(result)
    assert not lookup.called
    assert not post.called

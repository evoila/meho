# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The default view hides secret-named values in YAML comments, broken YAML and XML.

* A comment line in a YAML text (``# dbPassword: ...``).
* ``key: value`` lines of a text that does not read as YAML.
* XML: the text of a secret-named element, a secret-named attribute, and
  the ``value`` of a ``key`` / ``name`` pair element.

Each form is hidden in the caller view and in the audit copy. A text with
no secret-named field comes back unchanged. A declared plain field stays,
and a person's ``reveal_secret: true`` gets the raw text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import yaml
from kubernetes_asyncio.client.models import V1ConfigMap, V1ObjectMeta
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.kubernetes.ops_config import configmap_info
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    _secret_values,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.secret_text import hide_secret_text
from meho_backplane.settings import get_settings

# Made-up values, built from parts so secret scanners do not flag the source.
_FAKE_COMMENT = "FAKE" + "-made-up-comment-1104"
_FAKE_BROKEN = "FAKE" + "-made-up-broken-2215"
_FAKE_BLOCK = "FAKE" + "-made-up-block-3326"
_FAKE_ELEMENT = "FAKE" + "-made-up-element-4437"
_FAKE_ATTR = "FAKE" + "-made-up-attr-5548"
_FAKE_PAIR = "FAKE" + "-made-up-pair-6659"
_MARKER = "[REDACTED:param_name]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"

_COMMENT_YAML = f"db:\n  host: db\n  # dbPassword: {_FAKE_COMMENT}\n  port: 5432\n"
_BROKEN_YAML = (
    f"db:\n  dbPassword: {_FAKE_BROKEN}\n  hosts: [db-1\n"
    f"  dbSecret: |\n    {_FAKE_BLOCK}\n  port: 5432\n"
)
_ELEMENT_XML = f"<config>\n  <dbPassword>{_FAKE_ELEMENT}</dbPassword>\n  <host>db</host>\n</config>"
_ATTR_XML = f'<config>\n  <db host="db" dbPassword="{_FAKE_ATTR}"/>\n</config>'
_PAIR_XML = (
    "<appSettings>\n"
    '  <add key="LogLevel" value="info"/>\n'
    f'  <add key="DbPassword" value="{_FAKE_PAIR}"/>\n'
    "</appSettings>"
)
_DATA = {
    "comment.yaml": _COMMENT_YAML,
    "broken.yaml": _BROKEN_YAML,
    "element.xml": _ELEMENT_XML,
    "attr.xml": _ATTR_XML,
    "pair.xml": _PAIR_XML,
}
_FAKES = (_FAKE_COMMENT, _FAKE_BROKEN, _FAKE_BLOCK, _FAKE_ELEMENT, _FAKE_ATTR, _FAKE_PAIR)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _shown_fakes(blob: str) -> list[str]:
    return [value for value in _FAKES if value in blob]


# ---------------------------------------------------------------------------
# The text steps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_COMMENT_YAML, f"db:\n  host: db\n  # dbPassword: {_MARKER}\n  port: 5432\n"),
        (
            f"#dbSecret: {_FAKE_COMMENT} # old\nhost: db\n",
            f"#dbSecret: {_MARKER} # old\nhost: db\n",
        ),
        (f"## - apiToken: '{_FAKE_COMMENT}'\n", f"## - apiToken: {_MARKER}\n"),
    ],
    ids=["indented", "inline-note", "list-item-quoted"],
)
def test_yaml_comment_line_hides_a_secret_named_value(text: str, expected: str) -> None:
    shown = hide_secret_text(text)
    assert shown == expected
    assert yaml.safe_load(shown) == yaml.safe_load(text)


def test_yaml_comment_next_to_a_hidden_value_is_hidden_too() -> None:
    text = f"password: {_FAKE_BROKEN}\n# dbPassword: {_FAKE_COMMENT}\n"
    assert hide_secret_text(text) == f'password: "{_MARKER}"\n# dbPassword: {_MARKER}\n'


def test_broken_yaml_hides_secret_named_lines_and_their_blocks() -> None:
    shown = hide_secret_text(_BROKEN_YAML)
    assert shown == (
        f"db:\n  dbPassword: {_MARKER}\n  hosts: [db-1\n  dbSecret: {_MARKER}\n  port: 5432\n"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"bad: [\r\ndbSecret:\r\n  current: {_FAKE_BLOCK}\r\n\r\nport: 1\r\n",
            f"bad: [\r\ndbSecret: {_MARKER}\r\n\r\nport: 1\r\n",
        ),
        (f"\tdbPassword: {_FAKE_BROKEN}\nx: y\n", f"\tdbPassword: {_MARKER}\nx: y\n"),
        (f"bad: [\n- dbPassword: {_FAKE_BROKEN}\n", f"bad: [\n- dbPassword: {_MARKER}\n"),
        (f"bad: [\n# dbPassword: {_FAKE_COMMENT}\n", f"bad: [\n# dbPassword: {_MARKER}\n"),
    ],
    ids=["nested-map-crlf", "tab-indent", "list-item", "comment"],
)
def test_broken_yaml_forms_are_hidden(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f'bad: [\n"dbPassword": {_FAKE_BROKEN}\n',
            f'bad: [\n"dbPassword": {_MARKER}\n',
        ),
        (
            f"bad: [\n'dbPassword': {_FAKE_BROKEN}\n",
            f"bad: [\n'dbPassword': {_MARKER}\n",
        ),
        (
            f'bad: [\n  - "apiToken" : {_FAKE_BROKEN}\nport: 1\n',
            f'bad: [\n  - "apiToken" : {_MARKER}\nport: 1\n',
        ),
    ],
    ids=["double-quoted-key", "single-quoted-key", "quoted-key-list-item"],
)
def test_broken_yaml_hides_values_under_quoted_keys(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"bad: [\ndbSecret: # current\n    {_FAKE_BLOCK}\nport: 1\n",
            f"bad: [\ndbSecret: {_MARKER} # current\nport: 1\n",
        ),
        (
            f"bad: [\ndbPassword: first\n  {_FAKE_BLOCK}\n  {_FAKE_BROKEN}\nport: 1\n",
            f"bad: [\ndbPassword: {_MARKER}\nport: 1\n",
        ),
        (
            f'bad: [\n  dbPassword: "first\n    {_FAKE_BLOCK}"\n  port: 1\n',
            f"bad: [\n  dbPassword: {_MARKER}\n  port: 1\n",
        ),
        (
            f"bad: [\n- dbPassword: first\n    {_FAKE_BLOCK}\n  host: db\n- port: 1\n",
            f"bad: [\n- dbPassword: {_MARKER}\n  host: db\n- port: 1\n",
        ),
    ],
    ids=["value-after-a-note", "plain-on-more-lines", "quoted-on-more-lines", "list-item"],
)
def test_broken_yaml_hides_every_line_of_a_secret_value(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"bad: [\ndbPassword: {_FAKE_BROKEN} # rotate monthly\nport: 1\n",
            f"bad: [\ndbPassword: {_MARKER} # rotate monthly\nport: 1\n",
        ),
        (
            f"bad: [\ndbPassword: '{_FAKE_BROKEN}'  # rotate monthly\r\n",
            f"bad: [\ndbPassword: {_MARKER}  # rotate monthly\r\n",
        ),
        (
            f"bad: [\ndbSecret: |  # the current key\n  {_FAKE_BLOCK}\nport: 1\n",
            f"bad: [\ndbSecret: {_MARKER}  # the current key\nport: 1\n",
        ),
        (
            f"# dbPassword: {_FAKE_COMMENT} # rotate monthly\nport: 1\n",
            f"# dbPassword: {_MARKER} # rotate monthly\nport: 1\n",
        ),
    ],
    ids=["plain-value", "quoted-value-crlf", "block-value", "yaml-comment"],
)
def test_note_after_a_hidden_value_stays(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


def test_broken_yaml_keeps_flags_empty_and_masked_values() -> None:
    text = "bad: [\npassword: true\ndbSecret:\ntoken: '[REDACTED:api_key]'\nnext: 1\n"
    assert hide_secret_text(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            _ELEMENT_XML,
            f"<config>\n  <dbPassword>{_MARKER}</dbPassword>\n  <host>db</host>\n</config>",
        ),
        (
            f"<c><ns:password>\n  <value>{_FAKE_ELEMENT}</value>\n</ns:password ></c>",
            f"<c><ns:password>{_MARKER}</ns:password ></c>",
        ),
        (
            f"<c><password><![CDATA[{_FAKE_ELEMENT}]]></password></c>",
            f"<c><password>{_MARKER}</password></c>",
        ),
        (_ATTR_XML, f'<config>\n  <db host="db" dbPassword="{_MARKER}"/>\n</config>'),
        (
            f"<db dbPassword = '{_FAKE_ATTR}' note=\"a>b\">x</db>",
            f"<db dbPassword = '{_MARKER}' note=\"a>b\">x</db>",
        ),
        (
            _PAIR_XML,
            "<appSettings>\n"
            '  <add key="LogLevel" value="info"/>\n'
            f'  <add key="DbPassword" value="{_MARKER}"/>\n'
            "</appSettings>",
        ),
        (
            f'<property value="{_FAKE_PAIR}" name="hibernate.connection.password"/>',
            f'<property value="{_MARKER}" name="hibernate.connection.password"/>',
        ),
    ],
    ids=[
        "element",
        "element-with-children",
        "cdata",
        "attribute",
        "attribute-single-quote",
        "key-value-pair",
        "name-value-pair",
    ],
)
def test_xml_hides_secret_named_values(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"<dbPassword><dbPassword>first</dbPassword>{_FAKE_ELEMENT}</dbPassword>",
            f"<dbPassword>{_MARKER}</dbPassword>",
        ),
        (
            f"<c><password><![CDATA[a</password>{_FAKE_ELEMENT}]]></password></c>",
            f"<c><password>{_MARKER}</password></c>",
        ),
        (
            f"<c><password><!-- </password> -->{_FAKE_ELEMENT}</password></c>",
            f"<c><password>{_MARKER}</password></c>",
        ),
        (
            f"<c><password>{_FAKE_ELEMENT}<br></password><host>db</host></c>",
            f"<c><password>{_MARKER}</password><host>db</host></c>",
        ),
        (
            f'<c><!-- <db password="{_FAKE_ATTR}"/> <token>{_FAKE_ELEMENT}</token> --></c>',
            f'<c><!-- <db password="{_MARKER}"/> <token>{_MARKER}</token> --></c>',
        ),
    ],
    ids=[
        "same-name-child",
        "closing-tag-in-cdata",
        "closing-tag-in-comment",
        "unclosed-child",
        "inside-a-comment",
    ],
)
def test_xml_secret_element_ends_at_its_own_closing_tag(text: str, expected: str) -> None:
    assert hide_secret_text(text) == expected


def test_xml_keeps_empty_flag_and_unclosed_secret_elements() -> None:
    text = (
        "<c><password/><token></token><apiKey>true</apiKey>"
        '<db password=""/></c> Use --token <token> to log in.'
    )
    assert hide_secret_text(text) == text


def test_xml_inside_a_json_text_is_hidden_and_the_text_stays_json() -> None:
    note = json.dumps({"web.config": _PAIR_XML, "app.xml": _ATTR_XML})
    shown = hide_secret_text(note)
    assert _shown_fakes(shown) == []
    assert 'key="LogLevel" value="info"' in json.loads(shown)["web.config"]


@pytest.mark.parametrize(
    "text",
    [
        "db:\n  host: db\n  # port: 5432 (the default)\n  # Note: see the docs\n",
        "#!/bin/sh\n# usage: run.sh <name>\necho done\n",
        "db:\n  hosts: [db-1\n  port: 5432\n  user: app\n",
        "\tkey: value\nnote: plain words\n",
        (
            '<?xml version="1.0"?>\n<config xmlns="https://example.test/ns">\n'
            '  <host name="db">db-1</host>\n  <add key="LogLevel" value="info"/>\n'
            "  <!-- port: 5432 -->\n</config>\n"
        ),
        "<p>Read the <a href='https://example.test/'>guide</a> &amp; go on.</p>",
    ],
    ids=["yaml-comment", "script-comment", "broken-yaml", "broken-yaml-tab", "xml", "html"],
)
def test_text_of_each_kind_without_a_secret_field_is_unchanged(text: str) -> None:
    assert hide_secret_text(text) == text


def test_declared_plain_field_keeps_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_secret_values._PLAIN_FIELDS, "test.config.read", frozenset({"settings"}))
    answer = {"settings": _PAIR_XML, "other": _PAIR_XML}
    shown = hide_secret_values("test.config.read", answer)
    assert shown["settings"] == _PAIR_XML
    assert _FAKE_PAIR not in shown["other"]


# ---------------------------------------------------------------------------
# k8s.configmap.info through the dispatcher
# ---------------------------------------------------------------------------


class _NoOpKubernetesConnector(Connector):
    product = "kubernetes"
    version = "1.x"
    impl_id = "kubernetes"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _config_map(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return configmap_info(
        V1ConfigMap(data=dict(_DATA), metadata=V1ObjectMeta(name="app", namespace="team-a"))
    )


async def _register(embedding_service: AsyncMock) -> None:
    register_connector_v2(
        product="kubernetes", version="", impl_id="", cls=_NoOpKubernetesConnector
    )
    await register_typed_operation(
        product="kubernetes",
        version="1.x",
        impl_id="kubernetes",
        op_id="k8s.configmap.info",
        handler=_config_map,
        summary="Read one ConfigMap.",
        description="Read one ConfigMap.",
        parameter_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _read(principal_kind: PrincipalKind, *, reveal: bool) -> Any:
    operator = Operator(
        sub="op-secret-markup-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a5ee"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )
    params: dict[str, Any] = {"name": "app"}
    if reveal:
        params["reveal_secret"] = True
    return await dispatch(
        operator=operator,
        connector_id="kubernetes-1.x",
        op_id="k8s.configmap.info",
        target=None,
        params=params,
    )


async def _audit_row() -> AuditLog:
    async with get_sessionmaker()() as fresh:
        result = await fresh.execute(select(AuditLog).where(AuditLog.path == "k8s.configmap.info"))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("name", list(_DATA))
async def test_each_form_is_hidden_in_the_view_and_the_audit_copy(
    name: str, stub_embedding_service: AsyncMock
) -> None:
    await _register(stub_embedding_service)

    result = await _read(PrincipalKind.AGENT, reveal=False)

    assert result.status == "ok", result.error
    shown = result.result["data"][name]
    assert _MARKER in shown
    assert _shown_fakes(shown) == []
    assert _shown_fakes(result.model_dump_json()) == []
    row = await _audit_row()
    assert row.raw_payload["data"][name].startswith(_FINGERPRINT)
    assert _shown_fakes(json.dumps(row.raw_payload)) == []


async def test_reveal_by_a_person_returns_the_raw_texts(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    result = await _read(PrincipalKind.USER, reveal=True)

    assert result.status == "ok", result.error
    assert result.result["data"] == _DATA
    row = await _audit_row()
    assert _shown_fakes(json.dumps(row.raw_payload)) == []

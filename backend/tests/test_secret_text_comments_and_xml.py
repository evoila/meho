# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The default view hides secret-named fields in YAML comments, broken YAML and XML.

* A ``key: value`` line in a YAML comment (``# dbPassword: ...``) gets its
  value hidden. The rest of the comment and the YAML stay as they are.
* A ``key: value`` line in a text that is not valid YAML gets its value
  hidden, also a block or a nested map under the key, and the ``value`` of
  an env entry whose ``name`` is a secret name.
* An XML element or attribute with a secret name gets its value hidden.
* The audit copy of the answer holds a fingerprint in place of such a text.
* Text of each kind without a secret-named field comes back unchanged.
* A field an operation declares as plain stays whole, and a person who asks
  with ``reveal_secret: true`` gets the raw text back.
* The pass takes time in line with the size of the text.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import yaml
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.secret_text import hide_secret_text, hide_secret_text_leaves
from meho_backplane.settings import get_settings

# Made-up values, built from parts so secret scanners do not flag the source.
_FAKE_COMMENT = "FAKE" + "-made-up-comment-1182"
_FAKE_BROKEN = "FAKE" + "-made-up-broken-2293"
_FAKE_ELEMENT = "FAKE" + "-made-up-element-3304"
_FAKE_ATTR = "FAKE" + "-made-up-attr-4415"
_MARKER = "[REDACTED:param_name]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"
_OP_ID = "k8s.configmap.info"

#: Generous for a slow CI runner; a scan that reads the text again for each
#: line or tag takes far longer on these inputs.
_TIME_LIMIT_S = 20.0

_COMMENT_YAML = f"db:\n  host: db\n  # dbPassword: {_FAKE_COMMENT}\n  port: 5432\n"
#: A tab before a key is not valid YAML indent.
_BROKEN_YAML = f"db:\n  host: db\n\tdbPassword: {_FAKE_BROKEN}\n"
_ELEMENT_XML = f"<config><user>app</user><dbPassword>{_FAKE_ELEMENT}</dbPassword></config>"
_ATTR_XML = f'<datasource user="app" dbPassword="{_FAKE_ATTR}" port="5432"/>'
_DATA = {
    "comment.yaml": _COMMENT_YAML,
    "broken.yaml": _BROKEN_YAML,
    "element.xml": _ELEMENT_XML,
    "attr.xml": _ATTR_XML,
}
_FAKES = (_FAKE_COMMENT, _FAKE_BROKEN, _FAKE_ELEMENT, _FAKE_ATTR)


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


def _operator(principal_kind: PrincipalKind) -> Operator:
    return Operator(
        sub="op-secret-comment-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a5ee"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


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


async def _config_files(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"name": "app", "data": dict(_DATA)}


async def _register(embedding_service: AsyncMock) -> None:
    register_connector_v2(
        product="kubernetes", version="", impl_id="", cls=_NoOpKubernetesConnector
    )
    await register_typed_operation(
        product="kubernetes",
        version="1.x",
        impl_id="kubernetes",
        op_id=_OP_ID,
        handler=_config_files,
        summary="Read one ConfigMap.",
        description="Read one ConfigMap.",
        parameter_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _read(operator: Operator, *, reveal: bool) -> Any:
    params: dict[str, Any] = {"name": "app"}
    if reveal:
        params["reveal_secret"] = True
    return await dispatch(
        operator=operator,
        connector_id="kubernetes-1.x",
        op_id=_OP_ID,
        target=None,
        params=params,
    )


async def _audit_row() -> AuditLog:
    async with get_sessionmaker()() as fresh:
        result = await fresh.execute(select(AuditLog).where(AuditLog.path == _OP_ID))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    return rows[0]


def _shown_fakes(blob: str) -> list[str]:
    return [value for value in _FAKES if value in blob]


# ---------------------------------------------------------------------------
# YAML comments
# ---------------------------------------------------------------------------


def test_yaml_comment_hides_a_secret_named_value_and_keeps_the_rest() -> None:
    shown = hide_secret_text(_COMMENT_YAML)
    assert shown == f'db:\n  host: db\n  # dbPassword: "{_MARKER}"\n  port: 5432\n'
    assert yaml.safe_load(shown) == {"db": {"host": "db", "port": 5432}}


@pytest.mark.parametrize(
    "text",
    [
        f"#dbPassword: {_FAKE_COMMENT}\nport: 1\n",
        f"port: 1  # dbPassword: {_FAKE_COMMENT}\n",
        f"## - dbPassword: {_FAKE_COMMENT}\nport: 1\n",
        f"port: 1\n# credentials:\n#   user: app\n#   other: {_FAKE_COMMENT}\nhost: db\n",
        f"port: 1\n# dbPassword: |\n#   {_FAKE_COMMENT}\n#   second line\nhost: db\n",
        f"env:\n# - name: DB_PASSWORD\n#   value: {_FAKE_COMMENT}\n- name: LOG\n  value: info\n",
        f"snmp:\n  version: 2c\n  # community: {_FAKE_COMMENT}\n",
        f"just some words # dbPassword: {_FAKE_COMMENT}\n",
    ],
    ids=[
        "no-space",
        "after-a-value",
        "list-item",
        "nested-map",
        "block-value",
        "env-pair",
        "snmp-community",
        "plain-text",
    ],
)
def test_yaml_comment_forms_are_hidden_and_the_yaml_still_reads(text: str) -> None:
    shown = hide_secret_text(text)
    assert _FAKE_COMMENT not in shown
    assert yaml.safe_load(shown) == yaml.safe_load(text)


def test_hash_inside_a_quoted_or_block_value_is_no_comment() -> None:
    text = 'note: "a # b: c"\nscript: |\n  echo "# x: y"\n  # port: 1\n'
    assert hide_secret_text(text) == text


def test_secret_line_inside_a_quoted_value_is_hidden_and_the_yaml_still_reads() -> None:
    text = f'note: "a # dbPassword: {_FAKE_COMMENT}"\nport: 1\n'
    shown = hide_secret_text(text)
    assert _FAKE_COMMENT not in shown
    assert yaml.safe_load(shown)["port"] == 1


def test_yaml_comment_without_a_secret_name_is_unchanged() -> None:
    text = (
        "db:\n  # host: the main database\n  port: 5432  # the default port\n# token bucket: on\n"
    )
    assert hide_secret_text(text) == text


# ---------------------------------------------------------------------------
# Text that is not valid YAML
# ---------------------------------------------------------------------------


def test_broken_yaml_hides_a_secret_named_line_and_keeps_the_rest() -> None:
    assert hide_secret_text(_BROKEN_YAML) == f'db:\n  host: db\n\tdbPassword: "{_MARKER}"\n'


@pytest.mark.parametrize(
    "text",
    [
        f"key: [unclosed\ndbPassword: {_FAKE_BROKEN}\n",
        f"key: [unclosed\n- dbPassword: {_FAKE_BROKEN}\n",
        f"key: [unclosed\ncredentials:\n  user: app\n\n  other: {_FAKE_BROKEN}\nport: 1\n",
        f"key: [unclosed\ndbPassword: >-\n  {_FAKE_BROKEN}\nport: 1\n",
        f"key: [unclosed\n- name: DB_PASSWORD\n  value: {_FAKE_BROKEN}\n",
        f"key: [unclosed\nsnmp:\n  community: {_FAKE_BROKEN}\n",
        f"key: [unclosed\n# dbPassword: {_FAKE_BROKEN}\n",
    ],
    ids=["line", "list-item", "nested-map", "block-value", "env-pair", "snmp-community", "comment"],
)
def test_broken_yaml_forms_are_hidden(text: str) -> None:
    assert _FAKE_BROKEN not in hide_secret_text(text)


def test_broken_yaml_hides_a_nested_map_under_a_secret_name_and_keeps_the_next_key() -> None:
    text = f"key: [unclosed\ncredentials:\n  other: {_FAKE_BROKEN}\nport: 1\n"
    assert hide_secret_text(text) == f'key: [unclosed\ncredentials: "{_MARKER}"\nport: 1\n'


@pytest.mark.parametrize(
    "text",
    [
        "key: [unclosed\nhost: db\nport: 5432\n",
        "Error: bad config\n\thost: db\n  - port: 1\n",
        "key: [unclosed\nenabled: true\ntoken: null\napiKey:\n",
        f"key: [unclosed\npassword: {_MARKER}\n",
        "key: [unclosed\n- name: LOG_LEVEL\n  value: info\n",
    ],
)
def test_broken_yaml_without_a_secret_value_is_unchanged(text: str) -> None:
    assert hide_secret_text(text) == text


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def test_xml_element_hides_a_secret_named_value_and_keeps_the_rest() -> None:
    assert hide_secret_text(_ELEMENT_XML) == (
        f"<config><user>app</user><dbPassword>{_MARKER}</dbPassword></config>"
    )


def test_xml_attribute_hides_a_secret_named_value_and_keeps_the_rest() -> None:
    assert hide_secret_text(_ATTR_XML) == (
        f'<datasource user="app" dbPassword="{_MARKER}" port="5432"/>'
    )


@pytest.mark.parametrize(
    "text",
    [
        f"<password><value>{_FAKE_ELEMENT}</value></password>",
        f"<a:password xmlns:a='urn:x'>{_FAKE_ELEMENT}</a:password >",
        f"<password><![CDATA[{_FAKE_ELEMENT}]]></password>",
        f"<credentials>\n  <user>app</user>\n  <key>{_FAKE_ELEMENT}</key>\n</credentials>",
        f"<conf><password>{_FAKE_ELEMENT}",
        f"<snmp><version>2c</version><community>{_FAKE_ELEMENT}</community></snmp>",
        f"<user password='{_FAKE_ELEMENT}' />",
        f'<user a:dbPassword = "{_FAKE_ELEMENT}">x</user>',
        json.dumps({"server.xml": f'<user dbPassword="{_FAKE_ELEMENT}"/>'}),
    ],
    ids=[
        "nested",
        "prefix",
        "cdata",
        "multi-line",
        "no-close-tag",
        "snmp-community",
        "single-quotes",
        "prefixed-attribute",
        "xml-inside-json",
    ],
)
def test_xml_forms_are_hidden(text: str) -> None:
    assert _FAKE_ELEMENT not in hide_secret_text(text)


@pytest.mark.parametrize(
    "text",
    [
        '<?xml version="1.0"?>\n<config><user>app</user><port>5432</port></config>',
        '<input type="password" name="login"/><!-- a note -->',
        "<password/><token></token><secret>  </secret>",
        f"<password>{_MARKER}</password>",
        "<team><community>platform</community></team>",
        "Send the header as Bearer <token> to the API.",
    ],
)
def test_xml_without_a_secret_value_is_unchanged(text: str) -> None:
    assert hide_secret_text(text) == text


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


async def test_each_form_is_hidden_in_the_view_and_the_audit_copy(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    result = await _read(_operator(PrincipalKind.AGENT), reveal=False)

    assert result.status == "ok", result.error
    assert _shown_fakes(result.model_dump_json()) == []
    data = result.result["data"]
    for name in _DATA:
        assert _MARKER in data[name], name
    row = await _audit_row()
    assert _shown_fakes(json.dumps(row.raw_payload)) == []
    for name in _DATA:
        assert row.raw_payload["data"][name].startswith(_FINGERPRINT), name
    assert row.raw_payload["name"] == "app"


async def test_reveal_by_a_person_returns_the_raw_text(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    result = await _read(_operator(PrincipalKind.USER), reveal=True)

    assert result.status == "ok", result.error
    assert result.result["data"] == _DATA
    row = await _audit_row()
    assert _shown_fakes(json.dumps(row.raw_payload)) == []


def test_declared_plain_field_keeps_each_form_whole() -> None:
    node = {"detail": dict(_DATA), "other": dict(_DATA)}
    shown = hide_secret_text_leaves(node, plain_names=frozenset({"detail"}))
    assert shown["detail"] == _DATA
    assert _shown_fakes(json.dumps(shown["other"])) == []


# ---------------------------------------------------------------------------
# Time in line with the size
# ---------------------------------------------------------------------------


def _shown_text(text: str) -> str:
    shown = hide_secret_values("test.text.read", {"text": text})
    assert isinstance(shown, dict)
    return str(shown["text"])


_COUNT = 100_000


@pytest.mark.parametrize(
    "text",
    [
        "".join(f"key_{i}: {i}  # note {i}\n# token_{i}: x\n" for i in range(_COUNT))
        + f"# dbPassword: {_FAKE_COMMENT}\n",
        "key: [unclosed\n"
        + "".join(f"key_{i}: {i}\n" for i in range(_COUNT))
        + f"dbPassword: {_FAKE_BROKEN}\n",
        "<config>"
        + "<password>" * _COUNT
        + "".join(f'<k{i} a="1"/>' for i in range(_COUNT))
        + f'<user dbPassword="{_FAKE_ATTR}"/><dbPassword>{_FAKE_ELEMENT}</dbPassword></config>',
    ],
    ids=["yaml-comments", "broken-yaml", "xml"],
)
def test_large_text_of_each_form_is_hidden_in_time(text: str) -> None:
    started = time.monotonic()
    shown = _shown_text(text)
    assert time.monotonic() - started < _TIME_LIMIT_S
    assert _shown_fakes(shown) == []


def test_large_plain_text_of_each_form_is_unchanged_in_time() -> None:
    texts = [
        "".join(f"key_{i}: {i}  # note {i}\n" for i in range(_COUNT)),
        "key: [unclosed\n" + "".join(f"key_{i}: {i}\n" for i in range(_COUNT)),
        "<config>" + "".join(f'<k{i} a="1">v</k{i}>' for i in range(_COUNT)) + "</config>",
    ]
    for text in texts:
        started = time.monotonic()
        assert _shown_text(text) == text
        assert time.monotonic() - started < _TIME_LIMIT_S

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The default view hides secret-named fields that stand inside a text value.

* A JSON or YAML file in a string (a ConfigMap entry, the last-applied note
  of kubectl) gets the value of each secret-named field hidden, also the
  ``value`` of an env entry whose ``name`` is a secret name.
* The audit copy of the answer holds a fingerprint in place of such a text.
* A field an operation declares as plain stays whole.
* A person who asks with ``reveal_secret: true`` gets the raw text back,
  also from the excerpt of a custom resource.
* The text keeps reading as YAML after a value is hidden.
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
from meho_backplane.connectors.kubernetes.ops_customresource import (
    CR_SPEC_EXCERPT_MAX_BYTES,
    custom_resource_row,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import reveal_secret_var
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.secret_text import hide_secret_text, hide_secret_text_leaves
from meho_backplane.settings import get_settings

# Made-up values, built from parts so secret scanners do not flag the source.
_FAKE = "FAKE" + "-made-up-7731"
_FAKE_ENV = "FAKE" + "-made-up-env-4410"
_FAKE_YAML = "FAKE" + "-made-up-yaml-2209"
_FAKE_SNMP = "FAKE" + "-made-up-snmp-3318"
_FAKE_LIST = "FAKE" + "-made-up-list-5527"
_FAKE_MAP = "FAKE" + "-made-up-map-6636"
_MARKER = "[REDACTED:param_name]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

_CONFIG_JSON = json.dumps({"db": {"host": "db", "password": _FAKE}})
_APP_YAML = f"db:\n  port: 5432\n  dbPassword: {_FAKE_YAML}\n"
_ENV_YAML = f"env:\n- name: DB_USER\n  value: app\n- name: DB_PASSWORD\n  value: {_FAKE_ENV}\n"
_SNMP_YAML = f"snmp:\n  version: 2c\n  community: {_FAKE_SNMP}\n"
_PAIRS_YAML = (
    f"- name: DB_PASSWORD\n  value: [{_FAKE_LIST}]\n"
    f"- name: API_TOKEN\n  value:\n    current: {_FAKE_MAP}\n"
    "- name: LOG_LEVEL\n  value: [info]\n"
)
_DATA = {
    "config.json": _CONFIG_JSON,
    "app.yaml": _APP_YAML,
    "env.yaml": _ENV_YAML,
    "snmp.yaml": _SNMP_YAML,
    "pairs.yaml": _PAIRS_YAML,
}
_FAKES = (_FAKE, _FAKE_ENV, _FAKE_YAML, _FAKE_SNMP, _FAKE_LIST, _FAKE_MAP)


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
        sub="op-secret-text-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a5ed"),
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


def _config_map(*, with_last_applied: bool) -> V1ConfigMap:
    annotations: dict[str, str] = {"owner": "platform"}
    if with_last_applied:
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "data": _DATA,
            "metadata": {"name": "app", "namespace": "team-a"},
        }
        # kubectl writes compact JSON and a line break at the end.
        annotations[_LAST_APPLIED] = json.dumps(manifest, separators=(",", ":")) + "\n"
    return V1ConfigMap(
        data=dict(_DATA),
        metadata=V1ObjectMeta(name="app", namespace="team-a", annotations=annotations),
    )


async def _plain_config_map(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return configmap_info(_config_map(with_last_applied=False))


async def _applied_config_map(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return configmap_info(_config_map(with_last_applied=True))


def _custom_resource(*, padding: int = 0) -> dict[str, Any]:
    return {
        "apiVersion": "db.example.test/v1",
        "kind": "Database",
        "metadata": {"name": "app", "namespace": "team-a"},
        "spec": {
            "engine": "postgres",
            "password": _FAKE,
            "config": _APP_YAML,
            "notes": "x" * padding,
        },
    }


async def _custom_resource_info(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return custom_resource_row(_custom_resource(), ["spec.password", "spec.config"])


async def _register(
    handler: Any, embedding_service: AsyncMock, op_id: str = "k8s.configmap.info"
) -> None:
    register_connector_v2(
        product="kubernetes", version="", impl_id="", cls=_NoOpKubernetesConnector
    )
    await register_typed_operation(
        product="kubernetes",
        version="1.x",
        impl_id="kubernetes",
        op_id=op_id,
        handler=handler,
        summary="Read one ConfigMap.",
        description="Read one ConfigMap.",
        parameter_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _read(operator: Operator, *, reveal: bool, op_id: str = "k8s.configmap.info") -> Any:
    params: dict[str, Any] = {"name": "app"}
    if reveal:
        params["reveal_secret"] = True
    return await dispatch(
        operator=operator,
        connector_id="kubernetes-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


async def _audit_row(op_id: str = "k8s.configmap.info") -> AuditLog:
    async with get_sessionmaker()() as fresh:
        result = await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    return rows[0]


def _shown_fakes(blob: str) -> list[str]:
    return [value for value in _FAKES if value in blob]


# ---------------------------------------------------------------------------
# The text step
# ---------------------------------------------------------------------------


def test_json_text_hides_a_secret_named_field() -> None:
    assert json.loads(hide_secret_text(_CONFIG_JSON)) == {"db": {"host": "db", "password": _MARKER}}


def test_yaml_text_hides_a_key_without_quotes_and_keeps_the_rest() -> None:
    assert hide_secret_text(_APP_YAML) == f'db:\n  port: 5432\n  dbPassword: "{_MARKER}"\n'


def test_yaml_env_list_hides_the_value_of_a_secret_named_entry() -> None:
    shown = hide_secret_text(_ENV_YAML)
    assert _FAKE_ENV not in shown
    assert "- name: DB_USER\n  value: app\n" in shown
    assert f'- name: DB_PASSWORD\n  value: "{_MARKER}"\n' in shown


def test_yaml_block_value_and_nested_map_are_hidden_whole() -> None:
    text = f"password: |\n  {_FAKE}\n  second line\ncredentials:\n  pass: {_FAKE_ENV}\nport: 1\n"
    assert hide_secret_text(text) == (f'password: "{_MARKER}"\ncredentials: "{_MARKER}"\nport: 1\n')


def test_yaml_flags_empty_values_and_masked_values_stay() -> None:
    text = f'enabled: true\ntoken: null\napiKey: ""\npassword: "{_MARKER}"\n'
    assert hide_secret_text(text) == text


def test_yaml_number_under_a_secret_name_is_hidden() -> None:
    assert hide_secret_text("password: 12345678\n") == f'password: "{_MARKER}"\n'


def test_yaml_snmp_community_is_hidden_in_an_snmp_place_only() -> None:
    assert hide_secret_text(_SNMP_YAML) == f'snmp:\n  version: 2c\n  community: "{_MARKER}"\n'
    flat = f"snmp_version: 2\ncommunity: {_FAKE_SNMP}\n"
    assert _FAKE_SNMP not in hide_secret_text(flat)
    listed = f"snmpCommunities:\n- community: {_FAKE_SNMP}\n  access: ro\n"
    assert _FAKE_SNMP not in hide_secret_text(listed)
    other = "team:\n  community: platform\n"
    assert hide_secret_text(other) == other


def test_yaml_pair_value_that_is_a_list_or_a_map_is_hidden() -> None:
    shown = hide_secret_text(_PAIRS_YAML)
    assert _FAKE_LIST not in shown
    assert _FAKE_MAP not in shown
    assert yaml.safe_load(shown) == [
        {"name": "DB_PASSWORD", "value": _MARKER},
        {"name": "API_TOKEN", "value": _MARKER},
        {"name": "LOG_LEVEL", "value": ["info"]},
    ]


def test_yaml_nested_map_before_an_indented_field_still_reads() -> None:
    text = f"db:\n  credentials:\n    user: app\n    pass: {_FAKE}\n  port: 5432\nname: app\n"
    shown = hide_secret_text(text)
    assert _FAKE not in shown
    assert yaml.safe_load(shown) == {
        "db": {"credentials": _MARKER, "port": 5432},
        "name": "app",
    }


def test_yaml_block_list_under_a_secret_name_still_reads() -> None:
    text = f"passwords:\n- {_FAKE}\n- {_FAKE_ENV}\nport: 1\n"
    shown = hide_secret_text(text)
    assert yaml.safe_load(shown) == {"passwords": _MARKER, "port": 1}


@pytest.mark.parametrize(
    "text",
    [
        f"password: &pw {_FAKE}\nother: *pw\n",
        f"password: !!str &pw {_FAKE}\nother: *pw\n",
        f"password: &pw\n  user: app\n  value: {_FAKE}\nother: *pw\n",
        f"shared: &pw {_FAKE}\npassword: *pw\n",
        f'notes: &n "password: {_FAKE}"\ncopy: *n\n',
    ],
)
def test_yaml_anchor_of_a_hidden_value_stays_for_its_alias(text: str) -> None:
    shown = hide_secret_text(text)
    assert _FAKE not in shown
    loaded = yaml.safe_load(shown)
    assert _FAKE not in json.dumps(loaded)
    assert len(set(map(json.dumps, loaded.values()))) == 1


def test_yaml_text_with_many_documents_hides_the_field_in_each_document() -> None:
    fakes = [f"{_FAKE_YAML}-{index}" for index in range(20)]
    text = "".join(
        f"---\nkind: Secret\nmetadata:\n  name: app-{index}\nstringData:\n  password: {fake}\n"
        for index, fake in enumerate(fakes)
    )
    shown = hide_secret_text(text)
    assert not [fake for fake in fakes if fake in shown]
    assert shown.count(f'password: "{_MARKER}"') == len(fakes)
    assert "name: app-19" in shown
    view = hide_secret_values("other.read", {"data": {"all.yaml": text}})
    assert not [fake for fake in fakes if fake in view["data"]["all.yaml"]]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain words",
        "Note: the token was renewed",
        "https://registry.test/v2/",
        "2026-11-19T10:00:00Z",
        "key: [unclosed",
    ],
)
def test_text_without_a_secret_field_is_unchanged(text: str) -> None:
    assert hide_secret_text(text) == text


def test_yaml_inside_a_json_note_is_hidden_and_the_note_stays_json() -> None:
    note = json.dumps({"data": {"app.yaml": _APP_YAML, "env.yaml": _ENV_YAML}})
    shown = hide_secret_text(note)
    assert _shown_fakes(shown) == []
    parsed = json.loads(shown)
    assert parsed["data"]["app.yaml"] == f'db:\n  port: 5432\n  dbPassword: "{_MARKER}"\n'
    assert "value: app" in parsed["data"]["env.yaml"]


def test_leaves_walk_keeps_plain_fields_whole() -> None:
    node = {"token": {"detail": f"password: {_FAKE}"}, "other": [f"password: {_FAKE}"]}
    shown = hide_secret_text_leaves(node, plain_names=frozenset({"token"}))
    assert shown["token"] == node["token"]
    assert shown["other"] == [f'password: "{_MARKER}"']


def test_declared_plain_field_of_rke2_posture_stays_in_the_default_view() -> None:
    # Importing the module declares its plain fields.
    import meho_backplane.connectors.rke2.ops_read  # noqa: F401

    token = {
        "path": "/var/lib/rancher/rke2/server/token",
        "present": True,
        "status": "present",
        "mode": "0600",
        "owner": "root",
        "group": "root",
        "detail": "token: file present",
        "redacted": True,
    }
    answer = {"config_files": [], "token": token}
    assert hide_secret_values("rke2.posture.show", answer)["token"] == token
    assert hide_secret_values("other.read", answer)["token"] == _MARKER


# ---------------------------------------------------------------------------
# k8s.configmap.info through the dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler", [_plain_config_map, _applied_config_map])
async def test_configmap_info_shows_no_secret_in_the_view_or_the_audit_copy(
    handler: Any, stub_embedding_service: AsyncMock
) -> None:
    await _register(handler, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.AGENT), reveal=False)

    assert result.status == "ok", result.error
    assert _shown_fakes(result.model_dump_json()) == []
    data = result.result["data"]
    assert json.loads(data["config.json"]) == {"db": {"host": "db", "password": _MARKER}}
    assert data["app.yaml"] == f'db:\n  port: 5432\n  dbPassword: "{_MARKER}"\n'
    assert "- name: DB_USER\n  value: app\n" in data["env.yaml"]
    assert result.result["metadata"]["annotations"]["owner"] == "platform"

    row = await _audit_row()
    stored = json.dumps(row.raw_payload)
    assert _shown_fakes(stored) == []
    for name in _DATA:
        assert row.raw_payload["data"][name].startswith(_FINGERPRINT)
    assert row.raw_payload["metadata"]["annotations"]["owner"] == "platform"
    if handler is _applied_config_map:
        note = result.result["metadata"]["annotations"][_LAST_APPLIED]
        assert json.loads(note)["metadata"]["name"] == "app"
        assert row.raw_payload["metadata"]["annotations"][_LAST_APPLIED].startswith(_FINGERPRINT)


async def test_configmap_info_reveal_by_a_person_returns_the_raw_text(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(_applied_config_map, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.USER), reveal=True)

    assert result.status == "ok", result.error
    assert result.result["data"] == _DATA
    note = json.loads(result.result["metadata"]["annotations"][_LAST_APPLIED])
    assert note["data"] == _DATA
    row = await _audit_row()
    assert _shown_fakes(json.dumps(row.raw_payload)) == []


# ---------------------------------------------------------------------------
# k8s.cr.info through the dispatcher
# ---------------------------------------------------------------------------


async def test_cr_info_hides_spec_secrets_in_the_view_and_the_audit_copy(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(_custom_resource_info, stub_embedding_service, op_id="k8s.cr.info")

    result = await _read(_operator(PrincipalKind.AGENT), reveal=False, op_id="k8s.cr.info")

    assert result.status == "ok", result.error
    assert _shown_fakes(result.model_dump_json()) == []
    spec = json.loads(result.result["spec_excerpt"])
    assert spec["password"] == _MARKER
    assert spec["engine"] == "postgres"
    row = await _audit_row("k8s.cr.info")
    assert _shown_fakes(json.dumps(row.raw_payload)) == []


async def test_cr_info_reveal_by_a_person_returns_the_raw_excerpt_and_fields(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(_custom_resource_info, stub_embedding_service, op_id="k8s.cr.info")

    result = await _read(_operator(PrincipalKind.USER), reveal=True, op_id="k8s.cr.info")

    assert result.status == "ok", result.error
    spec = json.loads(result.result["spec_excerpt"])
    assert spec["password"] == _FAKE
    assert spec["config"] == _APP_YAML
    assert result.result["fields"] == {"spec.password": _FAKE, "spec.config": _APP_YAML}
    row = await _audit_row("k8s.cr.info")
    assert _shown_fakes(json.dumps(row.raw_payload)) == []


def test_cr_excerpt_that_is_cut_stays_hidden_on_a_reveal() -> None:
    token = reveal_secret_var.set(True)
    try:
        row = custom_resource_row(_custom_resource(padding=CR_SPEC_EXCERPT_MAX_BYTES))
    finally:
        reveal_secret_var.reset(token)
    assert row["spec_truncated"] is True
    assert _FAKE not in row["spec_excerpt"]
    assert _FAKE_YAML not in row["spec_excerpt"]

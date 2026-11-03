# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``k8s.secret.list`` and ``k8s.secret.info``: key names only, never a value.

The Kubernetes answers are recorded JSON (the shape the API server sends),
turned into ``V1Secret`` objects by the real ``kubernetes_asyncio``
deserializer. Two Secrets matter most:

* one whose values match no known credential pattern, so a green test
  proves the values are dropped by structure, not by a lucky pattern hit;
* one with a ``kubectl.kubernetes.io/last-applied-configuration``
  annotation that repeats the whole Secret, values included.

The dispatch tests run the real dispatcher, reducer and audit writer and
check that no value reaches the result, the stored result, the audit row
or the broadcast feed. All names and values are obvious fakes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from kubernetes_asyncio.client import ApiClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KUBERNETES_OPS, KubernetesConnector
from meho_backplane.connectors.kubernetes.ops_secret_list import (
    SECRET_LIST_OPS,
    _second_net,
    secret_info,
    secret_list_row,
)
from meho_backplane.connectors.kubernetes.redaction import (
    LAST_APPLIED_ANNOTATION,
    REDACTED_PREFIX,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.result_handle_store import SpillResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

_LIST = "k8s.secret.list"
_INFO = "k8s.secret.info"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000005135")

# Made-up values. They match no credential pattern (no "password=", no
# "Bearer", no JWT shape). None of them may show up anywhere.
_PLAIN_VALUE = "plain-words-no-label-DO-NOT-ECHO"
_PLAIN_B64 = "cGxhaW4td29yZHMtbm8tbGFiZWwtRE8tTk9ULUVDSE8="  # base64 of _PLAIN_VALUE
_OTHER_B64 = "c2Vjb25kLW1hZGUtdXAtdmFsdWU="  # "second-made-up-value"
_STRING_DATA_VALUE = "string-data-made-up-DO-NOT-ECHO"
_ANNOTATION_VALUE = "annotation-value-DO-NOT-ECHO"
_LAST_APPLIED = json.dumps(
    {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "app-creds", "namespace": "apps"},
        "data": {"api-key": _PLAIN_B64, "url": _OTHER_B64},
    }
)
_LEAKS = (
    _PLAIN_VALUE,
    _PLAIN_B64,
    _OTHER_B64,
    _STRING_DATA_VALUE,
    _ANNOTATION_VALUE,
    "second-made-up-value",
)


def _plain_secret_json(name: str = "plain-creds", namespace: str = "apps") -> dict[str, Any]:
    """A Secret whose values match no known credential pattern."""
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "resourceVersion": "4711",
            "creationTimestamp": "2026-09-01T10:00:00Z",
            "labels": {"app": "web"},
            "annotations": {"team-note": _ANNOTATION_VALUE},
        },
        "type": "Opaque",
        "data": {"token": _PLAIN_B64, "endpoint": _OTHER_B64},
    }


def _applied_secret_json() -> dict[str, Any]:
    """A Secret made by ``kubectl apply``: the annotation repeats every value."""
    return {
        "metadata": {
            "name": "app-creds",
            "namespace": "apps",
            "resourceVersion": "99",
            "creationTimestamp": "2026-09-02T08:30:00Z",
            "labels": {"app": "api", "tier": "back"},
            "annotations": {LAST_APPLIED_ANNOTATION: _LAST_APPLIED},
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "api",
                    "uid": "00000000-0000-0000-0000-000000000001",
                }
            ],
        },
        "type": "kubernetes.io/basic-auth",
        "immutable": True,
        "data": {"api-key": _PLAIN_B64, "url": _OTHER_B64},
        "stringData": {"extra": _STRING_DATA_VALUE},
    }


class _Recorded:
    """The body of a recorded API answer, as ``ApiClient.deserialize`` reads it."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.data = json.dumps(body)


async def _recorded(body: dict[str, Any], response_type: str) -> Any:
    """Turn a recorded JSON answer into the typed model the API client returns."""
    async with ApiClient() as api:
        return api.deserialize(_Recorded(body), response_type)


async def _secret(body: dict[str, Any]) -> Any:
    return await _recorded(body, "V1Secret")


async def _secret_list(items: list[dict[str, Any]]) -> Any:
    return await _recorded(
        {"kind": "SecretList", "apiVersion": "v1", "items": items}, "V1SecretList"
    )


def _assert_no_value(blob: str) -> None:
    for leak in _LEAKS:
        assert leak not in blob, leak


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    clear_registry()


@dataclass
class _StubTarget:
    name: str = "cluster-a"
    host: str = "cluster-a.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/cluster-a"
    product: str = "k8s"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    auth_model: str | None = "shared_service_account"
    fqdn: str | None = None
    id: object = field(default_factory=uuid4)
    tenant_id: object = _TENANT_ID


_TARGET = _StubTarget()


def _operator() -> Operator:
    return Operator(
        sub="op-secret-reader",
        name="Secret Read Test Operator",
        email=None,
        raw_jwt="op.secret.list.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _core_v1_patch() -> Any:
    return patch("meho_backplane.connectors.kubernetes.ops_secret_list.client.CoreV1Api")


@pytest.fixture
def _fake_api_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    api_client = MagicMock(close=AsyncMock())
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", AsyncMock(return_value=api_client))
    return api_client


@pytest.fixture
async def _registered_ops() -> None:
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()


class _RecordingStore:
    """Stand-in for the result handle store: keeps the rows the reducer stores."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def spill(self, *, rows: list[dict[str, Any]], **kwargs: Any) -> SpillResult:
        self.rows.extend(rows)
        return SpillResult(stored_rows=len(rows), document_stored=False)


async def _audit_blob() -> str:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog))).scalars().all()
    return json.dumps([[r.path, r.payload, r.raw_payload] for r in rows], default=str)


async def _announcement_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(AgentAnnouncement))).scalar_one()
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_ops_are_registered_as_safe_reads() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    for op_id in (_LIST, _INFO):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert op.group_key == "config"
        assert getattr(KubernetesConnector, op.handler_attr, None) is not None
    assert {op.op_id for op in SECRET_LIST_OPS} == {_LIST, _INFO}


def test_list_params_match_the_configmap_list() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    secret = by_id[_LIST].parameter_schema
    configmap = by_id["k8s.configmap.list"].parameter_schema
    assert secret["properties"] == configmap["properties"]
    assert secret["oneOf"] == configmap["oneOf"]


def test_guidance_names_the_kubernetes_rights() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    assert "'list' on 'secrets'" in json.dumps(by_id[_LIST].llm_instructions)
    assert "'get' on 'secrets'" in json.dumps(by_id[_INFO].llm_instructions)


# ---------------------------------------------------------------------------
# Row and info shapes
# ---------------------------------------------------------------------------


async def test_list_row_has_names_only() -> None:
    row = secret_list_row(await _secret(_plain_secret_json()))
    assert row == {
        "name": "plain-creds",
        "namespace": "apps",
        "type": "Opaque",
        "keys": ["endpoint", "token"],
        "key_count": 2,
        "labels": {"app": "web"},
        "annotation_keys": ["team-note"],
        "owner_count": 0,
        "created": "2026-09-01T10:00:00+00:00",
        "resource_version": "4711",
    }
    _assert_no_value(json.dumps(row))


async def test_list_row_drops_the_last_applied_annotation_value() -> None:
    row = secret_list_row(await _secret(_applied_secret_json()))
    assert row["annotation_keys"] == [LAST_APPLIED_ANNOTATION]
    assert row["keys"] == ["api-key", "extra", "url"]
    assert row["key_count"] == 3
    assert row["owner_count"] == 1
    _assert_no_value(json.dumps(row))
    assert REDACTED_PREFIX not in json.dumps(row)  # nothing was there to redact


async def test_info_has_owners_immutable_and_sizes_but_no_value() -> None:
    info = secret_info(await _secret(_applied_secret_json()))
    assert info["name"] == "app-creds"
    assert info["type"] == "kubernetes.io/basic-auth"
    assert info["immutable"] is True
    assert info["owners"] == [{"kind": "Deployment", "name": "api"}]
    assert info["owner_count"] == 1
    assert info["labels"] == {"app": "api", "tier": "back"}
    assert info["annotation_keys"] == [LAST_APPLIED_ANNOTATION]
    assert info["key_sizes"] == {
        "api-key": len(_PLAIN_VALUE),
        "extra": len(_STRING_DATA_VALUE),
        "url": len("second-made-up-value"),
    }
    _assert_no_value(json.dumps(info))


async def test_info_of_a_plain_secret_is_not_immutable() -> None:
    info = secret_info(await _secret(_plain_secret_json()))
    assert info["immutable"] is False
    assert info["owners"] == []
    assert info["key_sizes"] == {"endpoint": 20, "token": len(_PLAIN_VALUE)}


def test_second_net_redacts_a_data_map_that_slipped_in() -> None:
    out = _second_net({"name": "x", "data": {"token": _PLAIN_B64}})
    assert set(out) == {"name", "data"}
    assert out["data"]["token"].startswith(REDACTED_PREFIX)
    _assert_no_value(json.dumps(out))


async def test_answer_fits_the_response_schema() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    row = secret_list_row(await _secret(_applied_secret_json()))
    Draft202012Validator(by_id[_LIST].response_schema).validate({"rows": [row], "total": 1})
    info = secret_info(await _secret(_applied_secret_json()))
    Draft202012Validator(by_id[_INFO].response_schema).validate(info)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def test_list_in_one_namespace_forwards_the_label_selector(
    _fake_api_client: MagicMock,
) -> None:
    core = MagicMock()
    core.list_namespaced_secret = AsyncMock(return_value=await _secret_list([_plain_secret_json()]))
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await KubernetesConnector().k8s_secret_list(
            operator=_operator(),
            target=_TARGET,
            params={"namespace": "apps", "label_selector": "app=web"},
        )
    core.list_namespaced_secret.assert_awaited_once_with(namespace="apps", label_selector="app=web")
    assert result["total"] == 1
    assert result["rows"][0]["name"] == "plain-creds"


async def test_list_in_all_namespaces(_fake_api_client: MagicMock) -> None:
    core = MagicMock()
    core.list_secret_for_all_namespaces = AsyncMock(
        return_value=await _secret_list([_plain_secret_json(), _applied_secret_json()])
    )
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await KubernetesConnector().k8s_secret_list(
            operator=_operator(), target=_TARGET, params={"all_namespaces": True}
        )
    core.list_secret_for_all_namespaces.assert_awaited_once_with()
    assert result["total"] == 2
    _assert_no_value(json.dumps(result))


async def test_info_reads_one_secret(_fake_api_client: MagicMock) -> None:
    core = MagicMock()
    core.read_namespaced_secret = AsyncMock(return_value=await _secret(_applied_secret_json()))
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await KubernetesConnector().k8s_secret_info(
            operator=_operator(), target=_TARGET, params={"name": "app-creds", "namespace": "apps"}
        )
    core.read_namespaced_secret.assert_awaited_once_with(name="app-creds", namespace="apps")
    assert result["key_count"] == 3
    _assert_no_value(json.dumps(result))


# ---------------------------------------------------------------------------
# Full dispatch: result, stored result, audit row, broadcast feed
# ---------------------------------------------------------------------------


async def test_long_list_goes_behind_a_handle_and_no_value_leaks(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    store = _RecordingStore()
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))  # type: ignore[arg-type]
    items = [_plain_secret_json(name=f"plain-{n}") for n in range(59)] + [_applied_secret_json()]
    core = MagicMock()
    core.list_namespaced_secret = AsyncMock(return_value=await _secret_list(items))
    announcements_before = await _announcement_count()

    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_LIST,
            target=_TARGET,
            params={"namespace": "apps"},
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert len(store.rows) == 60
    assert {row["name"] for row in store.rows} >= {"plain-0", "app-creds"}
    _assert_no_value(json.dumps(result.model_dump(mode="json"), default=str))
    _assert_no_value(json.dumps(store.rows))

    audit = await _audit_blob()
    assert _LIST in audit
    _assert_no_value(audit)
    # Dispatches publish nothing on the broadcast feed.
    assert await _announcement_count() == announcements_before


async def test_info_dispatch_leaks_no_value(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = MagicMock()
    core.read_namespaced_secret = AsyncMock(return_value=await _secret(_applied_secret_json()))
    announcements_before = await _announcement_count()

    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_INFO,
            target=_TARGET,
            params={"name": "app-creds", "namespace": "apps"},
        )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["keys"] == ["api-key", "extra", "url"]
    _assert_no_value(json.dumps(result.model_dump(mode="json"), default=str))

    audit = await _audit_blob()
    assert _INFO in audit
    _assert_no_value(audit)
    assert await _announcement_count() == announcements_before

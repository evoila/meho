# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.finalizer.remove``: free one object that is stuck while being deleted.

What these tests prove:

* **Registration.** The op is registered with the destructive risk level,
  approval, and a preview builder.
* **The handler** (a fake API client): it sends a JSON Patch that tests the
  whole finalizer list and writes the list without the named finalizer,
  for a custom resource, a core kind and a cluster-scoped kind. Other
  finalizers stay.
* **Refusals, nothing written:** an object that is not being deleted, a
  finalizer the object does not have, a missing object, a changed list
  (``expected_finalizers``), a list that changes during the call (422), a
  core ``secrets`` object. Other API errors propagate.
* **The preview** shows the object, its deletion time and finalizers, and
  fails for every refusal; it declines without a connector.
* **The governed flow.** A call without the bound ``preview_hash`` is
  refused, an agent's call is refused, a refusal in the preview stops the
  call before it waits for approval.

All names are obvious fakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes import ops_finalizer as module
from meho_backplane.connectors.kubernetes.ops_finalizer import (
    FINALIZER_OPS,
    KubernetesFinalizerError,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings

_OP = "k8s.finalizer.remove"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000005932")
_HOLD = "example.io/cleanup"
_OTHER = "example.io/other"
_WHEN = "2026-01-02T03:04:05Z"

_CR_PARAMS: dict[str, Any] = {
    "group": "example.io",
    "version": "v1alpha1",
    "plural": "widgets",
    "namespace": "apps",
    "name": "old-widget",
    "finalizer": _HOLD,
}


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


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _operator(*, principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="op-requester",
        name="Finalizer Test Operator",
        email=None,
        raw_jwt="op.finalizer.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


def _stuck(
    finalizers: list[str] | None = None, *, deleting: bool = True, kind: str = "Widget"
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "old-widget", "finalizers": finalizers}
    if finalizers is None:
        metadata.pop("finalizers")
    if deleting:
        metadata["deletionTimestamp"] = _WHEN
    return {"kind": kind, "apiVersion": "example.io/v1alpha1", "metadata": metadata}


def _api(answer: dict[str, Any] | Exception, patch_error: Exception | None = None) -> MagicMock:
    """A fake ApiClient: GET answers *answer* (or raises it); PATCH answers {} or raises."""
    api = MagicMock()

    async def _call_api(path: str, method: str, **kwargs: Any) -> Any:
        if method == "GET":
            if isinstance(answer, Exception):
                raise answer
            return answer
        if patch_error is not None:
            raise patch_error
        return {}

    api.call_api = AsyncMock(side_effect=_call_api)
    return api


def _patch_calls(api: MagicMock) -> list[Any]:
    return [c for c in api.call_api.await_args_list if c.args[1] == "PATCH"]


async def _remove(api: MagicMock, params: dict[str, Any]) -> dict[str, Any]:
    conn = _make_connector()
    with patch.object(conn, "_get_api_client", AsyncMock(return_value=api)):
        return await conn.k8s_finalizer_remove(operator=_operator(), target=_TARGET, params=params)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_op_is_destructive_needs_approval_and_has_a_preview_builder() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    assert by_id[_OP].safety_level == "destructive"
    assert by_id[_OP].requires_approval is True
    assert "destructive" in by_id[_OP].tags
    assert _PREVIEW_BUILDERS[_OP] is module._finalizer_remove_preview
    assert classify_op(_OP) == "write"
    for op in FINALIZER_OPS:
        assert getattr(KubernetesConnector, op.handler_attr, None) is not None


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def test_removes_only_the_named_finalizer_with_a_tested_json_patch() -> None:
    api = _api(_stuck([_HOLD, _OTHER]))
    result = await _remove(api, dict(_CR_PARAMS))

    (call,) = _patch_calls(api)
    assert call.args[0] == "/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}"
    assert call.kwargs["path_params"] == {
        "group": "example.io",
        "version": "v1alpha1",
        "plural": "widgets",
        "name": "old-widget",
        "namespace": "apps",
    }
    assert call.kwargs["header_params"]["Content-Type"] == "application/json-patch+json"
    assert call.kwargs["body"] == [
        {"op": "test", "path": "/metadata/finalizers", "value": [_HOLD, _OTHER]},
        {"op": "replace", "path": "/metadata/finalizers", "value": [_OTHER]},
    ]
    assert result["removed"] is True
    assert result["remaining_finalizers"] == [_OTHER]


async def test_last_finalizer_leaves_an_empty_list() -> None:
    api = _api(_stuck([_HOLD]))
    result = await _remove(api, dict(_CR_PARAMS))
    assert _patch_calls(api)[0].kwargs["body"][1]["value"] == []
    assert result["remaining_finalizers"] == []


async def test_core_kind_uses_the_core_root() -> None:
    api = _api(_stuck(["kubernetes.io/pvc-protection"], kind="PersistentVolumeClaim"))
    params = {
        "group": "",
        "version": "v1",
        "plural": "persistentvolumeclaims",
        "namespace": "apps",
        "name": "old-claim",
        "finalizer": "kubernetes.io/pvc-protection",
    }
    await _remove(api, params)
    assert _patch_calls(api)[0].args[0] == "/api/{version}/namespaces/{namespace}/{plural}/{name}"
    assert "group" not in _patch_calls(api)[0].kwargs["path_params"]


async def test_cluster_scoped_kind_has_no_namespace_in_the_path() -> None:
    api = _api(_stuck([_HOLD]))
    params = {k: v for k, v in _CR_PARAMS.items() if k != "namespace"}
    result = await _remove(api, params)
    assert _patch_calls(api)[0].args[0] == "/apis/{group}/{version}/{plural}/{name}"
    assert result["namespace"] is None


@pytest.mark.parametrize(
    ("answer", "params_extra", "match"),
    [
        (_stuck([_HOLD], deleting=False), {}, "not being deleted"),
        (_stuck([_OTHER]), {}, "has no finalizer"),
        (_stuck(None), {}, "has no finalizer"),
        (_stuck([_HOLD, _OTHER]), {"expected_finalizers": [_HOLD]}, "changed since you looked"),
        (ApiException(status=404, reason="Not Found"), {}, "does not exist"),
    ],
)
async def test_refusals_write_nothing(
    answer: dict[str, Any] | Exception, params_extra: dict[str, Any], match: str
) -> None:
    api = _api(answer)
    with pytest.raises(KubernetesFinalizerError, match=match) as info:
        await _remove(api, {**_CR_PARAMS, **params_extra})
    assert _patch_calls(api) == []
    assert str(info.value) == info.value.remediation


async def test_expected_finalizers_that_match_are_accepted() -> None:
    api = _api(_stuck([_HOLD, _OTHER]))
    await _remove(api, {**_CR_PARAMS, "expected_finalizers": [_HOLD, _OTHER]})
    assert len(_patch_calls(api)) == 1


@pytest.mark.parametrize("status", [409, 422])
async def test_list_changed_during_the_call_is_a_clear_error(status: int) -> None:
    api = _api(_stuck([_HOLD]), patch_error=ApiException(status=status, reason="x"))
    with pytest.raises(KubernetesFinalizerError, match="changed while the call ran"):
        await _remove(api, dict(_CR_PARAMS))


async def test_object_gone_during_the_call_is_a_clear_error() -> None:
    api = _api(_stuck([_HOLD]), patch_error=ApiException(status=404, reason="nf"))
    with pytest.raises(KubernetesFinalizerError, match="gone already"):
        await _remove(api, dict(_CR_PARAMS))


async def test_other_api_errors_propagate() -> None:
    api = _api(ApiException(status=500, reason="boom"))
    with pytest.raises(ApiException):
        await _remove(api, dict(_CR_PARAMS))
    api = _api(_stuck([_HOLD]), patch_error=ApiException(status=403, reason="forbidden"))
    with pytest.raises(ApiException):
        await _remove(api, dict(_CR_PARAMS))


@pytest.mark.parametrize("plural", ["secrets", "Secrets"])
async def test_core_secrets_are_refused_before_any_call(plural: str) -> None:
    api = _api(_stuck([_HOLD]))
    params = {**_CR_PARAMS, "group": "", "version": "v1", "plural": plural}
    with pytest.raises(KubernetesFinalizerError, match=r"k8s\.secret\.delete"):
        await _remove(api, params)
    api.call_api.assert_not_awaited()


async def test_a_secrets_plural_in_another_group_is_not_refused() -> None:
    api = _api(_stuck([_HOLD]))
    await _remove(api, {**_CR_PARAMS, "plural": "secrets"})
    assert len(_patch_calls(api)) == 1


async def test_path_parameters_go_through_call_api_not_into_the_template() -> None:
    api = _api(_stuck([_HOLD]))
    await _remove(api, {**_CR_PARAMS, "name": "../x"})
    call = _patch_calls(api)[0]
    assert "../x" not in call.args[0]
    assert call.kwargs["path_params"]["name"] == "../x"


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


def _ctx(params: dict[str, Any], connector: KubernetesConnector | None) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=_OP),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_TARGET if connector is not None else None,
        params=params,
        connector_id=_CONNECTOR_ID,
    )


async def _preview(api: MagicMock, params: dict[str, Any]) -> dict[str, Any] | None:
    conn = _make_connector()
    with patch.object(conn, "_get_api_client", AsyncMock(return_value=api)):
        return await build_proposed_effect(_ctx(params, conn))


async def test_preview_shows_object_deletion_time_finalizers_and_warning() -> None:
    effect = await _preview(_api(_stuck([_HOLD, _OTHER])), dict(_CR_PARAMS))

    assert effect is not None
    preview = effect["preview"]
    assert preview["finalizer"] == _HOLD
    assert "clean-up" in preview["warning"]
    blast = preview["blast_radius"]
    assert blast["object"]["name"] == "old-widget"
    assert blast["object"]["kind"] == "Widget"
    assert blast["object"]["deletion_timestamp"] == _WHEN
    assert blast["object"]["finalizers"] == [_HOLD, _OTHER]
    assert blast["children"] == []
    assert blast_radius_missing_reason(preview) is None


@pytest.mark.parametrize(
    ("answer", "text"),
    [
        (_stuck([_HOLD], deleting=False), "not being deleted"),
        (_stuck([_OTHER]), "has no finalizer"),
        (ApiException(status=404, reason="Not Found"), "does not exist"),
    ],
)
async def test_preview_of_a_refusal_fails_the_blast_radius_gate(
    answer: dict[str, Any] | Exception, text: str
) -> None:
    effect = await _preview(_api(answer), dict(_CR_PARAMS))

    assert effect is not None
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert text in reason


async def test_preview_declines_without_a_connector() -> None:
    assert await module._finalizer_remove_preview(_ctx(dict(_CR_PARAMS), None)) is None


# ---------------------------------------------------------------------------
# The governed flow
# ---------------------------------------------------------------------------


@pytest.fixture
async def _registered_ops() -> None:
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()


def _fake_api(monkeypatch: pytest.MonkeyPatch, api: MagicMock) -> None:
    """The dispatcher builds its own connector; give it the fake API client."""
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", AsyncMock(return_value=api))


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _call(
    params: dict[str, Any], *, operator: Operator | None = None, preview_hash: str | None = None
) -> Any:
    return await dispatch(
        operator=operator or _operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
        preview_hash=preview_hash,
    )


async def _preview_of(params: dict[str, Any]) -> dict[str, Any]:
    return await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )


async def test_call_without_a_preview_hash_is_refused(
    _registered_ops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(_stuck([_HOLD]))
    _fake_api(monkeypatch, api)
    result = await _call(dict(_CR_PARAMS))
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert _patch_calls(api) == []
    assert await _pending_count() == 0


async def test_call_with_the_bound_hash_waits_for_approval_and_writes_nothing(
    _registered_ops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(_stuck([_HOLD, _OTHER]))
    _fake_api(monkeypatch, api)
    preview = await _preview_of(dict(_CR_PARAMS))
    assert preview["status"] == "ok", preview
    result = await _call(dict(_CR_PARAMS), preview_hash=preview["preview_hash"])
    assert result.status == "awaiting_approval", result
    assert _patch_calls(api) == []
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["finalizers"] == [_HOLD, _OTHER]
    assert effect["blast_radius"]["object"]["deletion_timestamp"] == _WHEN


async def test_object_that_is_not_being_deleted_is_refused_before_approval(
    _registered_ops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(_stuck([_HOLD], deleting=False))
    _fake_api(monkeypatch, api)
    preview = await _preview_of(dict(_CR_PARAMS))
    result = await _call(dict(_CR_PARAMS), preview_hash=preview.get("preview_hash"))
    assert result.status == "denied", result
    assert result.extras["error_code"] == "blast_radius_required"
    assert "not being deleted" in (result.error or "")
    assert _patch_calls(api) == []
    assert await _pending_count() == 0


async def test_agent_cannot_remove_a_finalizer(
    _registered_ops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(_stuck([_HOLD]))
    _fake_api(monkeypatch, api)
    result = await _call(dict(_CR_PARAMS), operator=_operator(principal_kind=PrincipalKind.AGENT))
    assert result.status == "denied", result
    assert _patch_calls(api) == []
    assert await _pending_count() == 0

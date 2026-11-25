# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.apply`` refuses a manifest it cannot apply before the call waits.

What these tests prove, through the real dispatch over a stand-in cluster
(made-up names):

* each form that the run cannot parse is refused at once, with the same
  text on the call and on ``preview_operation``'s path; no approval
  request is stored, no audit row of a waiting call is written, and the
  cluster gets no request;
* three documents split by ``---`` wait for approval once, and after the
  approval they are applied in order;
* an empty text and a list as the value of ``manifest`` are still refused
  by the params check;
* a manifest that passes the check passes the parse step of the run.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops_write_dangerous import _parse_manifest
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings
from tests.test_connectors_k8s_finalizer_remove import _StubTarget

_OP = "k8s.apply"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000006982")
_TARGET = _StubTarget(tenant_id=_TENANT_ID)

_FORM = (
    "Send one text with the documents split by `---`. "
    "Each document has `apiVersion`, `kind` and `metadata.name`"
)

_NAMESPACE = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: web\n"
_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: web\n"
    "spec:\n  replicas: 1\n"
)
_SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: web\n"
_THREE = "---\n".join([_NAMESPACE, _DEPLOYMENT, _SERVICE])

#: (manifest, the part of the reason that names what is wrong)
_REFUSED: dict[str, tuple[str, str]] = {
    "json-array": ('[{"apiVersion": "v1", "kind": "Namespace"}]', "document 0 is not a mapping"),
    "yaml-list": ("- apiVersion: v1\n  kind: Namespace\n", "document 0 is not a mapping"),
    "one-word": ("hello", "document 0 is not a mapping"),
    "list-without-name": (
        "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: Namespace\n"
        "    metadata: {name: web}\n",
        "document 0 (v1/List) is missing metadata.name",
    ),
    "no-kind": (
        "apiVersion: v1\nmetadata:\n  name: web\n",
        "document 0 is missing apiVersion/kind",
    ),
    "no-api-version": (
        "kind: Namespace\nmetadata:\n  name: web\n",
        "document 0 is missing apiVersion/kind",
    ),
    "no-name": (
        "apiVersion: v1\nkind: Namespace\nmetadata: {}\n",
        "document 0 (v1/Namespace) is missing metadata.name",
    ),
    "only-spaces": ("   \n  \n", "the text has no documents"),
    "only-comments": ("# a\n# b\n", "the text has no documents"),
    "only-separators": ("---\n---\n---\n", "the text has no documents"),
    "bad-yaml-in-the-middle": (
        _NAMESPACE + "---\na: [b\n---\n" + _SERVICE,
        "the text is not valid YAML or JSON",
    ),
    "no-kind-in-the-middle": (
        _NAMESPACE + "---\nmetadata: {name: x}\n---\n" + _SERVICE,
        "document 1 is missing apiVersion/kind",
    ),
    "word-in-the-middle": (
        _NAMESPACE + "---\nhello\n---\n" + _SERVICE,
        "document 1 is not a mapping",
    ),
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


@pytest.fixture
async def cluster(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Register the ops; any use of the cluster client is a call to this stand-in."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()
    get_client = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", get_client)
    return get_client


def _operator() -> Operator:
    return Operator(
        sub="op-apply-form",
        name="Apply Form Test Operator",
        email=None,
        raw_jwt="op.apply.form.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _call(params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )


async def _count(model: Any) -> int:
    async with get_sessionmaker()() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.parametrize("name", list(_REFUSED))
async def test_a_manifest_the_run_cannot_parse_is_refused_before_the_wait(
    name: str, cluster: AsyncMock
) -> None:
    manifest, wrong = _REFUSED[name]
    params = {"manifest": manifest}

    result = await _call(params)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )

    assert result.status == "denied", result
    assert result.extras["error_code"] == "refused_before_approval"
    reason = result.extras["reason"]
    assert reason == f"The manifest cannot be applied: {wrong}. {_FORM}"
    assert preview["error"] == result.error
    assert preview["extras"]["reason"] == reason
    assert await _count(ApprovalRequest) == 0
    async with get_sessionmaker()() as s:
        audit = list((await s.execute(select(AuditLog))).scalars().all())
    assert [r.payload["result_status"] for r in audit] == ["denied"]
    cluster.assert_not_awaited()


async def test_the_refusal_does_not_repeat_a_value_of_the_manifest(cluster: AsyncMock) -> None:
    result = await _call({"manifest": "apiVersion: v1\nkind: Namespace\nmetadata: {x: sentinel-1}"})
    assert result.status == "denied"
    assert "sentinel-1" not in result.error
    bad = await _call({"manifest": "a: [sentinel-2"})
    assert bad.status == "denied"
    assert "sentinel-2" not in bad.error


async def test_three_documents_wait_once_and_apply_in_order(
    cluster: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _call({"manifest": _THREE})
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1

    seen: list[str] = []

    async def _apply_one(_dyn: Any, doc: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        seen.append(doc["kind"])
        return {"kind": doc["kind"]}

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous._apply_one", _apply_one
    )
    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
        AsyncMock(return_value=MagicMock()),
    )
    ran = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params={"manifest": _THREE},
        _approved=True,
    )
    assert ran.status == "ok", ran
    assert seen == ["Namespace", "Deployment", "Service"]


@pytest.mark.parametrize("manifest", ["", ["a"]], ids=["empty", "list-value"])
async def test_an_empty_text_and_a_list_are_still_refused_by_the_params_check(
    manifest: Any, cluster: AsyncMock
) -> None:
    result = await _call({"manifest": manifest})
    assert result.status != "awaiting_approval"
    assert result.extras.get("error_code") != "refused_before_approval"
    assert await _count(ApprovalRequest) == 0


@pytest.mark.parametrize(
    "manifest",
    [
        _NAMESPACE,
        _THREE,
        _NAMESPACE + "---\n" + "# note\n",
        '{"apiVersion": "v1", "kind": "X", "metadata": {"name": "a"}}',
    ],
    ids=["one", "three", "trailing-empty", "json"],
)
async def test_a_manifest_that_passes_the_check_passes_the_parse_step(
    manifest: str, cluster: AsyncMock
) -> None:
    from meho_backplane.connectors.kubernetes.ops_write_preview import _apply_park_check
    from meho_backplane.operations._preview import PreviewContext

    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP),
        connector_instance=None,
        operator=_operator(),
        target=_TARGET,
        params={"manifest": manifest},
        connector_id=_CONNECTOR_ID,
    )
    assert await _apply_park_check(ctx) is None
    assert _parse_manifest(manifest)


async def test_the_hint_says_what_stays_applied() -> None:
    from meho_backplane.connectors.kubernetes.ops_write_meta import WRITE_DANGEROUS_OPS

    op = next(o for o in WRITE_DANGEROUS_OPS if o.op_id == _OP)
    assert op.llm_instructions is not None
    assert (
        "When the cluster refuses one document of the text, the documents before it stay applied."
        in op.llm_instructions["parameter_hints"]["manifest"]
    )

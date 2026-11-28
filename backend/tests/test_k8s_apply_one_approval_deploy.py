# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One deploy through ``k8s.apply`` needs one approval.

With the switch ``K8S_APPLY_ONE_APPROVAL_HINT`` on, the agent texts of
``k8s.apply`` say two things: ``preview_operation`` needs no approval and shows
the manifest text that was sent, and all manifests of one deploy go in one
``k8s.apply`` call, as one manifest text with the documents split by ``---``.
These tests run that path against a stand-in cluster:

* the preview of three manifests creates no approval request, and it does not
  contact the cluster;
* the one real call with three manifests creates exactly one approval request;
* after the approval, the handler applies the three manifests in order;
* an exact-op ``search_operations`` answer carries the switch-on texts where
  the agent reads them.

The stand-in cluster is a fake of the Kubernetes dynamic client. It refuses an
object whose namespace does not exist yet, like a real API server does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import (
    call_operation,
    preview_operation,
    search_operations,
)
from meho_backplane.operations.typed_register import (
    clear_typed_op_snapshot,
    run_typed_op_registrars,
)
from meho_backplane.settings import get_settings
from tests._people_behind_helpers import give_known_person

_TENANT = UUID("00000000-0000-0000-0000-000000006921")
_CONNECTOR_ID = "k8s-1.x"

_DEPLOY = "\n---\n".join(
    [
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: web\n",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: web\n"
        + "spec:\n  replicas: 1\n",
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: web\n"
        + "spec:\n  ports:\n    - port: 80\n",
    ]
)


class _Response:
    """The raw answer ``server_side_apply(serialize=False)`` gives."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.reason = "OK" if status < 300 else "Not Found"
        self.headers: CIMultiDictProxy[str] = CIMultiDictProxy(CIMultiDict())
        self._body = json.dumps(body)

    async def text(self) -> str:
        return self._body


@dataclass
class _FakeCluster:
    """A stand-in cluster: it keeps namespaces and records every apply request."""

    namespaces: set[str] = field(default_factory=set)
    requests: list[tuple[str, str, bool]] = field(default_factory=list)

    async def server_side_apply(
        self, resource: Any, *, body: dict[str, Any], **kwargs: Any
    ) -> _Response:
        dry_run = kwargs.get("dry_run") == "All"
        meta = body["metadata"]
        self.requests.append((body["kind"], meta["name"], dry_run))
        namespace = meta.get("namespace")
        if namespace and namespace not in self.namespaces:
            status = {"kind": "Status", "status": "Failure", "code": 404}
            return _Response(404, {**status, "message": f'namespaces "{namespace}" not found'})
        if not dry_run and body["kind"] == "Namespace":
            self.namespaces.add(meta["name"])
        stored = {} if dry_run else {"resourceVersion": "7", "uid": "u-" + meta["name"]}
        return _Response(200, {"kind": body["kind"], "metadata": stored})

    @property
    def real_applies(self) -> list[tuple[str, str]]:
        return [(kind, name) for kind, name, dry in self.requests if not dry]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("K8S_APPLY_ONE_APPROVAL_HINT", raising=False)
    monkeypatch.delenv("OPERATION_CONTRACT_GUIDANCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    builders = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    clear_typed_op_snapshot()
    yield
    reset_dispatcher_caches()
    clear_registry()
    clear_typed_op_snapshot()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders)


@pytest.fixture
def cluster() -> Iterator[_FakeCluster]:
    fake = _FakeCluster()
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = fake.server_side_apply

    async def _dynamic(_api_client: Any) -> Any:
        return dyn

    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    with (
        patch.object(KubernetesConnector, "_get_api_client", AsyncMock(return_value=MagicMock())),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
            side_effect=_dynamic,
        ),
    ):
        yield fake


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="Deploy Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _seed() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await run_typed_op_registrars(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT,
                name="cluster-a",
                aliases=[],
                product="k8s",
                host="cluster.invalid",
                port=6443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "1.34"},
                preferred_impl_id="k8s",
                notes="k8s.apply deploy test",
            )
        )
        await session.commit()


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


def _args(manifest: str = _DEPLOY) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": "k8s.apply",
        "target": "cluster-a",
        "params": {"manifest": manifest},
    }


@pytest.mark.asyncio
async def test_preview_makes_no_approval_and_does_not_contact_the_cluster(
    cluster: _FakeCluster,
) -> None:
    await _seed()
    preview = await preview_operation(_operator("op-requester"), _args())
    assert preview["status"] == "ok", preview
    assert preview["op_id"] == "k8s.apply"
    # It shows the manifests that the call would apply. It builds no plan from the cluster.
    assert preview["plan_shown"] is False
    shown = json.dumps(preview)
    for name in ("Namespace", "Deployment", "Service"):
        assert name in shown
    assert await _approval_rows() == []
    assert cluster.requests == []


@pytest.mark.asyncio
async def test_one_deploy_call_with_three_manifests_makes_one_approval_and_applies_in_order(
    cluster: _FakeCluster,
) -> None:
    await _seed()
    requester = _operator("op-requester")
    preview = await preview_operation(requester, _args())
    assert preview["status"] == "ok", preview
    assert await _approval_rows() == []

    call = await call_operation(requester, _args())
    assert call["status"] == "awaiting_approval", call
    rows = await _approval_rows()
    assert len(rows) == 1
    # Nothing was applied for real. The card reads the cluster with dry runs only.
    assert cluster.real_applies == []

    approver = _operator("op-approver").model_copy(update={"self_approver": True})
    await give_known_person(approver)
    async with get_sessionmaker()() as session:
        await approve_request(session, rows[0].id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, rows[0].id)
        assert row is not None
        result = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert result.status == "ok", result
    assert cluster.real_applies == [
        ("Namespace", "web"),
        ("Deployment", "web"),
        ("Service", "web"),
    ]
    assert len(await _approval_rows()) == 1


@pytest.mark.asyncio
async def test_approval_card_of_a_new_namespace_deploy_says_the_check_failed(
    cluster: _FakeCluster,
) -> None:
    """The dry run of an object in a namespace that does not exist yet fails on the card.

    The call still waits for one approval, and the real apply then works, as
    the test above shows. The hint says so.
    """
    await _seed()
    call = await call_operation(_operator("op-requester"), _args())
    assert call["status"] == "awaiting_approval", call
    (row,) = await _approval_rows()
    effect = dict(row.proposed_effect)
    assert effect["preview_unavailable"] is True
    assert 'namespaces "web" not found' in str(effect["preview_error"])


@pytest.mark.asyncio
async def test_a_dry_run_call_waits_for_approval_too(cluster: _FakeCluster) -> None:
    await _seed()
    params = {"manifest": _DEPLOY, "dry_run": "server"}
    call = await call_operation(_operator("op-requester"), {**_args(), "params": params})
    assert call["status"] == "awaiting_approval", call
    assert len(await _approval_rows()) == 1


_SWITCH_ON_SUMMARY = (
    "Server-side apply one or more manifests in order; needs one approval per call."
)


@pytest.mark.parametrize(
    ("hint_switch", "guidance_switch"),
    [(True, True), (True, False), (False, False)],
)
@pytest.mark.asyncio
async def test_search_operations_shows_the_texts_where_the_agent_reads_them(
    cluster: _FakeCluster,
    monkeypatch: pytest.MonkeyPatch,
    hint_switch: bool,
    guidance_switch: bool,
) -> None:
    """The summary shows with the hint switch alone; ``when_to_use`` needs the guidance switch."""
    monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", str(hint_switch).lower())
    monkeypatch.setenv("OPERATION_CONTRACT_GUIDANCE", str(guidance_switch).lower())
    get_settings.cache_clear()
    await _seed()
    answer = await search_operations(
        _operator("op-requester"), {"connector_id": _CONNECTOR_ID, "op_id": "k8s.apply"}
    )
    operation = answer["operation"]
    if not hint_switch:
        assert operation["summary"] != _SWITCH_ON_SUMMARY
        assert "dry_run='server' previews" in operation["summary"]
        return
    assert operation["summary"] == _SWITCH_ON_SUMMARY
    if guidance_switch:
        when_to_use = operation["guidance"]["when_to_use"]
        assert "preview_operation needs no approval" in when_to_use
        assert "documents split by '---'" in when_to_use
        assert "still waits for approval" in operation["guidance"]["parameter_hints"]["dry_run"]
    else:
        assert "guidance" not in operation

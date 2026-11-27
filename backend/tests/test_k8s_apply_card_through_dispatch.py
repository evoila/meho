# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``k8s.apply``, through the real dispatch.

Each call goes through ``dispatch`` to the stored approval request, over a
stand-in Kubernetes API server that records every request. The card is then
read where a person and a caller read it: the console page, the REST view
and the MCP approvals tool.

* The same apply waits for a person by token, a person in the web console and
  an agent with a grant: each call makes one approval request. Neither the
  stored request nor its audit row holds a made-up Secret value.
* The live object is read only for a login with the role Operator or higher.
  For a login with the Read-only role the stand-in server records no read,
  and the card says "not read".
* When the dry run fails, the card still shows the manifest and the subject
  line, says which document the dry run stopped at, and says that the summary
  of the objects could not be made.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import structlog
import yaml
from sqlalchemy import func, select

from meho_backplane.api.v1.approvals import _proposed_effect_for
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest, AuditLog, PermissionVerdict
from meho_backplane.mcp.tools.approvals import _row_to_dict
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from tests.test_k8s_apply_object_summary import _Response
from tests.test_ui_approvals import (
    _RESOLVE_OPERATOR,
    _authenticated_client,
    _bff_env,
    _seed_session_sync,
    _seed_tenant,
)
from tests.test_ui_approvals import _operator as _console_viewer

__all__ = ["_bff_env"]

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c4d2")
_CONNECTOR_ID = "k8s-1.x"
_AGENT_CLIENT = "agent:apply-card"
_SUMMARY_FAILED = "The summary of the objects could not be made, because the dry run failed."
_NOT_READ = "The live object was not read: the login that asked has a role below Operator."


@pytest.fixture(autouse=True)
def _kubernetes_registry() -> Iterator[None]:
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@dataclass
class _Target:
    name: str = "cluster-a"
    host: str = "cluster-a.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/cluster-a"
    product: str = "k8s"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    auth_model: str | None = "shared_service_account"
    fqdn: str | None = None
    id: object = field(default_factory=uuid.uuid4)
    tenant_id: object = _TENANT


class _StandInServer:
    """A stand-in Kubernetes API server that records every request.

    An object in a namespace of *missing_namespaces* is refused with 404, as a
    real server refuses it while its namespace does not exist yet. A kind of
    *refused_kinds* is refused with 422, as an admission check refuses it.
    """

    def __init__(
        self,
        *,
        missing_namespaces: frozenset[str] = frozenset(),
        refused_kinds: frozenset[str] = frozenset(),
        live: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.missing_namespaces = missing_namespaces
        self.refused_kinds = refused_kinds
        self.live = live or {}
        self.requests: list[tuple[str, str, str]] = []
        self.resources = SimpleNamespace(get=self._discover)

    async def _discover(self, *, api_version: str, kind: str) -> Any:
        return SimpleNamespace(kind=kind, namespaced=kind != "Namespace")

    async def server_side_apply(self, resource: Any, **kwargs: Any) -> _Response:
        self.requests.append(("apply", resource.kind, kwargs["name"]))
        assert kwargs["dry_run"] == "All"
        namespace = kwargs.get("namespace")
        if namespace in self.missing_namespaces:
            message = f"namespaces {namespace} not found"
            return _Response(404, {"kind": "Status", "message": message})
        if resource.kind in self.refused_kinds:
            return _Response(422, {"kind": "Status", "message": "refused by an admission check"})
        body = kwargs["body"]
        return _Response(200, {**body, "metadata": {**body["metadata"], "managedFields": []}})

    async def get(self, resource: Any, **kwargs: Any) -> _Response:
        self.requests.append(("get", resource.kind, kwargs["name"]))
        live = self.live.get(kwargs["name"])
        return _Response(200, live) if live else _Response(404, {"kind": "Status", "code": 404})

    def reads(self) -> int:
        return sum(1 for verb, _, _ in self.requests if verb == "get")


def _login(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    kind: PrincipalKind = PrincipalKind.USER,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
        client_id=_AGENT_CLIENT if kind is PrincipalKind.AGENT else None,
    )


def _agent() -> Operator:
    return _login(str(uuid.uuid5(uuid.NAMESPACE_URL, _AGENT_CLIENT)), kind=PrincipalKind.AGENT)


async def _register_ops() -> None:
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()


async def _grant_the_agent() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT_CLIENT,
                op_pattern="k8s.apply",
                verdict=PermissionVerdict.AUTO_EXECUTE.value,
                target_scope="*",
                created_by_sub="test-admin",
            )
        )


def _setup(monkeypatch: pytest.MonkeyPatch, server: _StandInServer) -> None:
    _seed_tenant(_TENANT, "apply-card")
    asyncio.run(_register_ops())

    async def _dynamic_client(_api_client: Any) -> Any:
        return server

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient", _dynamic_client
    )
    monkeypatch.setattr(
        KubernetesConnector, "_get_api_client", AsyncMock(return_value=MagicMock(close=AsyncMock()))
    )


def _park(operator: Operator, manifest: str) -> ApprovalRequest:
    """Dispatch one apply that waits; return its stored approval request."""

    async def _do() -> ApprovalRequest:
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": manifest},
        )
        assert result.status == "awaiting_approval", result
        async with get_sessionmaker()() as session:
            request_id = uuid.UUID(result.extras["approval_request_id"])
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            session.expunge(row)
            return row

    return asyncio.run(_do())


def _console_card(row: ApprovalRequest) -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT)
    viewer = _console_viewer(tenant_id=_TENANT)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=viewer):
            response = client.get(f"/ui/approvals/{row.id}")
    assert response.status_code == 200, response.text
    return response.text


def _views(row: ApprovalRequest) -> dict[str, dict[str, Any]]:
    """The card as the person REST view, the agent REST view and the MCP tool give it."""
    return {
        "rest-person": _proposed_effect_for(_login("viewer"), row),
        "rest-agent": _proposed_effect_for(_agent(), row),
        "mcp": _row_to_dict(row)["proposed_effect"],
    }


def _made_up_name() -> str:
    return "ab" + secrets.token_hex(10) + "z"


def _widget_with_a_secret_template(values: list[str]) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "example.test/v1",
            "kind": "Widget",
            "metadata": {"name": "w1", "namespace": "prod"},
            "spec": {
                "template": {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "inner"},
                    "stringData": {
                        "secretName": values[0],
                        "serviceAccountName": values[1],
                        "other": values[2],
                    },
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# The same apply waits for each caller
# ---------------------------------------------------------------------------


def test_the_same_apply_waits_for_a_token_person_a_console_person_and_an_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    asyncio.run(_grant_the_agent())
    values = [_made_up_name() for _ in range(3)]
    manifest = _widget_with_a_secret_template(values)
    callers = [
        _login("person-token"),
        _login("person-console", web_session=True),
        _agent(),
    ]

    rows = [_park(caller, manifest) for caller in callers]

    async def _stored() -> tuple[int, list[AuditLog]]:
        async with get_sessionmaker()() as session:
            count = (
                await session.execute(select(func.count()).select_from(ApprovalRequest))
            ).scalar_one()
            audits = [await session.get(AuditLog, row.request_audit_id) for row in rows]
        return int(count), [audit for audit in audits if audit is not None]

    count, audits = asyncio.run(_stored())
    assert count == len(callers)
    assert len(audits) == len(callers)
    assert [row.principal_sub for row in rows] == [caller.sub for caller in callers]
    # Only dry runs reached the stand-in server.
    assert {verb for verb, _, _ in server.requests} <= {"apply", "get"}
    for row, audit in zip(rows, audits, strict=True):
        # The manifest holds a Secret, so the stored request keeps it only in
        # the encrypted handoff of the run: the row has no params.
        assert row.params is None
        assert row.execution_handle is not None
        stored = json.dumps(
            [row.params, row.proposed_effect, audit.payload, audit.raw_payload], default=str
        )
        for value in values:
            assert value not in stored
        card = _console_card(row)
        for value in values:
            assert value not in card
        for view in _views(row).values():
            for value in values:
                assert value not in json.dumps(view)


# ---------------------------------------------------------------------------
# The live read depends on the role of the login that asked
# ---------------------------------------------------------------------------

_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: prod}\n"
    "spec: {template: {spec: {containers: [{name: app, image: web:2}]}}}\n"
)
_LIVE = {
    "web": {
        "kind": "Deployment",
        "metadata": {"name": "web", "managedFields": [{"manager": "helm", "operation": "Update"}]},
    }
}


def test_a_read_only_login_makes_no_live_read(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer(live=_LIVE)
    _setup(monkeypatch, server)

    row = _park(_login("reader", role=TenantRole.READ_ONLY), _DEPLOYMENT)

    assert server.reads() == 0
    (entry,) = row.proposed_effect["preview"]["object_summary"]
    assert entry["state"] == "not_read"
    card = _console_card(row)
    assert "not read" in card
    assert _NOT_READ in card
    assert "helm" not in card


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
def test_an_operator_or_admin_login_reads_the_live_object(
    monkeypatch: pytest.MonkeyPatch, role: TenantRole
) -> None:
    server = _StandInServer(live=_LIVE)
    _setup(monkeypatch, server)

    row = _park(_login("writer", role=role), _DEPLOYMENT)

    assert server.reads() == 1
    card = _console_card(row)
    assert "exists already" in card
    assert "helm (Update)" in card


# ---------------------------------------------------------------------------
# A failed dry run keeps the manifest and the subject line
# ---------------------------------------------------------------------------

_NEW_NAMESPACE = (
    "apiVersion: v1\nkind: Namespace\nmetadata: {name: web}\n"
    "---\n"
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api, namespace: web}\n"
    "spec: {replicas: 2, template: {spec: {containers: [{name: app, image: api:1}]}}}\n"
    "---\n"
    "apiVersion: v1\nkind: Service\nmetadata: {name: api, namespace: web}\n"
    "spec: {ports: [{port: 80, targetPort: 8181}]}\n"
)


def _assert_failed_card(row: ApprovalRequest, stop: str) -> None:
    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert "object_summary" not in json.dumps(effect)
    assert effect["subject"] == "Namespace/web, Deployment/web/api, Service/web/api"
    assert effect["dry_run_stop"] == stop
    views = _views(row)
    person = views.pop("rest-person")
    assert [doc["kind"] for doc in person["manifest"]] == ["Namespace", "Deployment", "Service"]
    assert person["manifest"][2]["spec"]["ports"][0]["targetPort"] == 8181
    assert person["subject"] == effect["subject"]
    assert person["dry_run_stop"] == stop
    # A caller that is not a person reads the marker and the error, without
    # the manifest, the subject or the stop.
    for name, view in {"rest-person": person, **views}.items():
        assert view["preview_unavailable"] is True, name
        assert view["preview_error"] == effect["preview_error"], name
        assert "object_summary" not in json.dumps(view), name
    for name, view in views.items():
        assert not {"manifest", "subject", "dry_run_stop", "run_commands"} & set(view), name
        assert "8181" not in json.dumps(view), name
    card = _console_card(row)
    assert "What the apply sends" in card
    assert stop in card
    assert "8181" in card
    assert _SUMMARY_FAILED in card


def test_a_new_namespace_card_keeps_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer(missing_namespaces=frozenset({"web"}))
    _setup(monkeypatch, server)

    row = _park(_login("person-token"), _NEW_NAMESPACE)

    assert [kind for verb, kind, _ in server.requests if verb == "apply"] == [
        "Namespace",
        "Deployment",
    ]
    _assert_failed_card(
        row,
        "The dry run stopped at document 2 of 3 (Deployment/web/api)."
        " The documents after it had no dry run.",
    )


def test_a_refused_object_in_an_existing_namespace_keeps_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"Service"}))
    _setup(monkeypatch, server)

    row = _park(_login("person-token"), _NEW_NAMESPACE)

    _assert_failed_card(row, "The dry run stopped at document 3 of 3 (Service/web/api).")


def test_an_unreadable_manifest_card_says_the_summary_could_not_be_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)

    row = _park(_login("person-token"), "a: [b")

    assert row.proposed_effect["preview_unavailable"] is True
    assert server.requests == []
    card = _console_card(row)
    assert "Blast radius unknown" in card
    assert _SUMMARY_FAILED in card


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_a_rejected_name_with_a_token_is_hidden_in_both_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"Service"}))
    _setup(monkeypatch, server)
    token = ".".join([_b64(b'{"alg":"HS256"}'), _b64(b'{"sub":"x"}'), secrets.token_hex(16)])
    manifest = (
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: web}\n"
        "---\n"
        f"apiVersion: v1\nkind: Service\nmetadata: {{name: 'web-{token}', namespace: web}}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert effect["subject"].startswith("Namespace/web, Service/web/web-")
    assert effect["dry_run_stop"].startswith("The dry run stopped at document 2 of 2 (Service/")
    for label in (effect["subject"], effect["dry_run_stop"]):
        assert token not in label
    assert token not in json.dumps(effect)
    assert token not in _console_card(row)


def test_a_date_in_a_config_map_still_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"ConfigMap"}))
    _setup(monkeypatch, server)
    manifest = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
        "data: {day: 2024-01-31, at: 2024-01-31T10:00:00Z}\n"
        "---\n"
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        "spec: {containers: [{name: app, image: report:1, args: [2024-01-31]}]}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert effect["manifest"][0]["data"] == {"day": "2024-01-31", "at": "2024-01-31T10:00:00+00:00"}
    assert effect["run_commands"][0]["args"] == ["2024-01-31"]
    assert "2024-01-31" in _console_card(row)


def test_a_malformed_secret_puts_no_value_in_the_error_or_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    value = _made_up_name()
    manifest = (
        "apiVersion: v1\nkind: Secret\nmetadata: {name: s, namespace: prod}\n"
        f"stringData:\n  note: {value}: x\n"
    )

    with structlog.testing.capture_logs() as logs:
        row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert "line 5, column" in effect["preview_error"]
    assert row.params is None
    assert value not in json.dumps(effect)
    assert value not in json.dumps(logs, default=str)
    assert any(entry.get("event") == "proposed_effect_preview_failed" for entry in logs)
    assert value not in _console_card(row)

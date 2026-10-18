# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch behaviour of the destructive vCenter tagging removals (#3827).

The VMware floor puts the ingested category/tag ``DELETE``s and the three tag
detach actions in the ``destructive`` risk level, and
``tagging_delete_preview.py`` registers a blast-radius builder for every
op_id spelling. These tests prove the dispatch contract that follows:

* the preview builders are registered for every spelling and list the
  affected objects from read-only vCenter calls;
* a human operator cannot run a tagging removal without a bound preview
  (``preview_binding_required``), and with one the op waits for a second
  person with the blast radius on the approval row;
* a second operator approves and the delete runs exactly once;
* a service account with a live matching grant never auto-executes a tag
  detach (the destructive tier is not grantable), and the grant itself is
  refused at creation.

Mirrors the fixtures of ``test_destructive_preview_binding.py`` (recording
HTTP connector, in-memory SQLite via the autouse-migrated engine).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

import meho_backplane.broadcast.publisher as _publisher
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest import tagging_delete_preview as previews
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    ServicePrincipalGrant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-000000003827")
_CONNECTOR_ID = "vmware-rest-9.0"
_CATEGORY_DELETE = "DELETE:/cis/tagging/category/{categoryId}"
_TAG_DETACH = "POST:/cis/tagging/tag-association/{tagId}?action=detach"
_CATEGORY_PARAMS = {"categoryId": "cat-1"}
_DETACH_PARAMS = {"tagId": "tag-1", "body": {"object_id": {"type": "Datastore", "id": "ds-7"}}}
_PRINCIPAL = "svc:tag-bot"


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
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry; keep the preview registry."""
    builders_snapshot = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    previews.register_tagging_removal_previews()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders_snapshot)


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_publisher, "publish_event", _noop)


@pytest.fixture
def embedding() -> list[float]:
    return [0.1] * 384


def _operator(
    *, sub: str = "op-requester", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="Tagging Delete Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """In-memory target shape the resolver / dispatcher / connectors read."""

    def __init__(self, *, name: str = "vcenter-lab") -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.name = name
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


def _not_found(method: str, path: str) -> httpx.HTTPStatusError:
    request = httpx.Request(method, f"https://vcenter.test{path}")
    return httpx.HTTPStatusError("404", request=request, response=httpx.Response(404))


class _FakeVcenter(HttpConnector):
    """vCenter stand-in: answers the tagging reads, records every write."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    categories: ClassVar[dict[str, dict[str, Any]]] = {
        "cat-1": {"name": "storage-policy", "cardinality": "MULTIPLE"},
    }
    tags: ClassVar[dict[str, dict[str, Any]]] = {
        "tag-1": {"name": "gold", "category_id": "cat-1"},
        "tag-2": {"name": "silver", "category_id": "cat-1"},
    }
    attached: ClassVar[dict[str, list[dict[str, str]]]] = {
        "tag-1": [{"type": "Datastore", "id": "ds-7"}],
        "tag-2": [],
    }

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[dict[str, Any]] = []

    async def _request_json(  # type: ignore[override]
        self, target: Any, method: str, path: str, *, operator: Operator, **_kw: Any
    ) -> Any:
        if path.startswith("/cis/tagging/category/"):
            detail = self.categories.get(path.removeprefix("/cis/tagging/category/"))
        elif path.startswith("/cis/tagging/tag/"):
            detail = self.tags.get(path.removeprefix("/cis/tagging/tag/"))
        else:
            detail = None
        if detail is None:
            raise _not_found(method, path)
        return dict(detail)

    async def _post_json(  # type: ignore[override]
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Any:
        if verb == "POST" and path.endswith("?action=list-tags-for-category"):
            category_id = (json or {}).get("category_id")
            return [
                tag_id for tag_id, tag in self.tags.items() if tag["category_id"] == category_id
            ]
        if verb == "POST" and path.endswith("?action=list-attached-objects"):
            tag_id = path.removeprefix("/cis/tagging/tag-association/").split("?", 1)[0]
            return list(self.attached.get(tag_id, []))
        self.writes.append({"verb": verb, "path": path, "json": json})
        return {}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


def _register_fake_vcenter() -> _FakeVcenter:
    register_connector_v2(product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVcenter)
    connector = get_or_create_connector_instance(_FakeVcenter)
    assert isinstance(connector, _FakeVcenter)
    return connector


async def _insert_descriptor(
    *, op_id: str, method: str, params: dict[str, Any], embedding: list[float]
) -> None:
    """Seed one enabled, ``destructive`` tagging removal the way the floor stores it."""
    path = op_id.split(":", 1)[1]
    properties: dict[str, Any] = {
        name: {"type": "string", "x-meho-param-loc": "path"} for name in params if name != "body"
    }
    if "body" in params:
        properties["body"] = {"type": "object", "x-meho-param-loc": "body"}
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary="Tagging removal test op.",
        description=None,
        tags=[],
        parameter_schema={"type": "object", "properties": properties},
        response_schema=None,
        llm_instructions=None,
        safety_level="destructive",
        requires_approval=True,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with get_sessionmaker()() as s:
        s.add(descriptor)
        await s.commit()


async def _seed_target(*, name: str = "vcenter-lab") -> TargetORM:
    async with get_sessionmaker()() as s:
        target = TargetORM(
            tenant_id=_TENANT,
            name=name,
            aliases=[],
            product="vmware",
            host="vcenter.test",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "9.0"},
            preferred_impl_id="vmware-rest",
            notes="seeded by test_connectors_vmware_rest_tagging_delete_dispatch",
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)
        s.expunge(target)
        return target


async def _seed_grant(*, op_id: str) -> uuid.UUID:
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                id=grant_id,
                tenant_id=_TENANT,
                principal_sub=_PRINCIPAL,
                op_id=op_id,
                connector_id=_CONNECTOR_ID,
                target_id=None,
                target_product="vmware",
                reason="unattended tag cleanup",
                created_by_sub="op-admin",
            )
        )
        await s.commit()
    return grant_id


async def _approval_row_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _grant_use_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(AuditLog).where(
                AuditLog.method == "APPROVAL", AuditLog.path == "approval.decision"
            )
        )
        return [r for r in result.scalars().all() if r.payload.get("decision") == "auto-approved"]


def _ctx(connector: _FakeVcenter, op_id: str, params: dict[str, Any]) -> PreviewContext:
    descriptor = EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        source_kind="ingested",
        method=op_id.split(":", 1)[0],
        path=op_id.split(":", 1)[1],
        safety_level="destructive",
        requires_approval=True,
        parameter_schema={},
    )
    return PreviewContext(
        descriptor=descriptor,
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params=params,
        connector_id=_CONNECTOR_ID,
    )


# ---------------------------------------------------------------------------
# Preview builders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    [
        "DELETE:/cis/tagging/category/{categoryId}",
        "DELETE:/api/cis/tagging/category/{category_id}",
        "DELETE:/cis/tagging/tag/{tagId}",
        "DELETE:/api/cis/tagging/tag/{tag_id}",
        "POST:/cis/tagging/tag-association/{tagId}?action=detach",
        "POST:/api/cis/tagging/tag-association/{tag_id}?action=detach",
        "POST:/cis/tagging/tag-association?action=detach-multiple-tags-from-object",
        "POST:/api/cis/tagging/tag-association?action=detach-multiple-tags-from-object",
        "POST:/cis/tagging/tag-association/{tagId}?action=detach-tag-from-multiple-objects",
    ],
)
def test_every_tagging_removal_spelling_has_a_preview_builder(op_id: str) -> None:
    """The vendor-spec and appliance spellings, with and without ``/api``, resolve a builder."""
    assert op_id in previews.TAGGING_REMOVAL_OP_IDS
    assert op_id in _PREVIEW_BUILDERS


@pytest.mark.asyncio
async def test_category_delete_preview_lists_tags_and_attached_objects() -> None:
    """The category blast radius names the category, every tag in it, and attached objects."""
    connector = _FakeVcenter()

    preview = await previews.category_delete_preview(
        _ctx(connector, _CATEGORY_DELETE, _CATEGORY_PARAMS)
    )

    assert preview is not None
    block = preview["blast_radius"]
    assert block["object"] == {
        "kind": "tag_category",
        "id": "cat-1",
        "name": "storage-policy",
        "description": None,
        "cardinality": "MULTIPLE",
        "associable_types": None,
        "tag_count": 2,
        "present": True,
    }
    assert block["irreversibility"] == previews.IRREVERSIBLE
    assert [child["id"] for child in block["children"]] == ["tag-1", "tag-2"]
    assert block["children"][0]["name"] == "gold"
    assert block["children"][0]["attached_object_count"] == 1
    assert block["children"][0]["attached_objects"] == [{"kind": "Datastore", "id": "ds-7"}]
    assert connector.writes == []


@pytest.mark.asyncio
async def test_category_delete_preview_for_absent_category_is_honest() -> None:
    """A category vCenter does not know parks with ``present=False`` and no children."""
    preview = await previews.category_delete_preview(
        _ctx(_FakeVcenter(), _CATEGORY_DELETE, {"categoryId": "gone"})
    )

    assert preview is not None
    block = preview["blast_radius"]
    assert block["object"] == {"kind": "tag_category", "id": "gone", "present": False}
    assert block["children"] == []
    assert block["irreversibility"] == previews.ALREADY_ABSENT


@pytest.mark.asyncio
async def test_tag_delete_preview_lists_attached_objects() -> None:
    preview = await previews.tag_delete_preview(
        _ctx(_FakeVcenter(), "DELETE:/cis/tagging/tag/{tagId}", {"tagId": "tag-1"})
    )

    assert preview is not None
    block = preview["blast_radius"]
    assert block["object"]["name"] == "gold"
    assert block["object"]["attached_object_count"] == 1
    assert block["children"] == [{"kind": "Datastore", "id": "ds-7"}]
    assert block["irreversibility"] == previews.IRREVERSIBLE


@pytest.mark.asyncio
async def test_detach_previews_name_the_tag_and_the_objects() -> None:
    """The three detach actions list what is detached and say the change can be undone."""
    connector = _FakeVcenter()

    single = await previews.tag_detach_preview(_ctx(connector, _TAG_DETACH, _DETACH_PARAMS))
    assert single is not None
    assert single["blast_radius"]["object"]["name"] == "gold"
    assert single["blast_radius"]["children"] == [{"kind": "Datastore", "id": "ds-7"}]
    assert single["blast_radius"]["irreversibility"] == previews.REVERSIBLE_BY_REATTACH

    from_objects = await previews.detach_tag_from_objects_preview(
        _ctx(
            connector,
            "POST:/cis/tagging/tag-association/{tagId}?action=detach-tag-from-multiple-objects",
            {
                "tagId": "tag-1",
                "body": {"object_ids": [{"type": "VirtualMachine", "id": "vm-1"}]},
            },
        )
    )
    assert from_objects is not None
    assert from_objects["blast_radius"]["children"] == [{"kind": "VirtualMachine", "id": "vm-1"}]

    tags_from_object = await previews.detach_tags_from_object_preview(
        _ctx(
            connector,
            "POST:/cis/tagging/tag-association?action=detach-multiple-tags-from-object",
            {"body": {"object_id": {"type": "Datastore", "id": "ds-7"}, "tag_ids": ["tag-2"]}},
        )
    )
    assert tags_from_object is not None
    block = tags_from_object["blast_radius"]
    assert block["object"] == {"kind": "Datastore", "id": "ds-7", "present": True}
    assert [child["name"] for child in block["children"]] == ["silver"]
    assert connector.writes == []


# ---------------------------------------------------------------------------
# Human operator: preview required, then a second person approves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_category_delete_refused_without_preview(embedding: list[float]) -> None:
    """No bound preview → ``preview_binding_required``; nothing runs, nothing parks."""
    connector = _register_fake_vcenter()
    await _insert_descriptor(
        op_id=_CATEGORY_DELETE, method="DELETE", params=_CATEGORY_PARAMS, embedding=embedding
    )

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_CATEGORY_DELETE,
        target=_FakeTarget(),
        params=_CATEGORY_PARAMS,
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert connector.writes == []
    assert await _approval_row_count() == 0


@pytest.mark.asyncio
async def test_operator_category_delete_waits_for_approval_with_blast_radius(
    embedding: list[float],
) -> None:
    """With a bound preview the delete parks; the row lists the tags that would go."""
    connector = _register_fake_vcenter()
    await _insert_descriptor(
        op_id=_CATEGORY_DELETE, method="DELETE", params=_CATEGORY_PARAMS, embedding=embedding
    )
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_CATEGORY_DELETE,
        target=_FakeTarget(),
        params=_CATEGORY_PARAMS,
    )
    assert preview["status"] == "ok"

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_CATEGORY_DELETE,
        target=_FakeTarget(),
        params=_CATEGORY_PARAMS,
        preview_hash=preview["preview_hash"],
    )

    assert result.status == "awaiting_approval", result
    assert connector.writes == []
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["id"] == "cat-1"
    assert effect["blast_radius"]["object"]["tag_count"] == 2
    assert [child["id"] for child in effect["blast_radius"]["children"]] == [
        "tag-1",
        "tag-2",
    ]


@pytest.mark.asyncio
async def test_second_operator_approves_and_the_delete_runs_once(embedding: list[float]) -> None:
    """Preview → park → a different operator approves → the DELETE reaches vCenter once."""
    connector = _register_fake_vcenter()
    await _seed_target()
    await _insert_descriptor(
        op_id=_CATEGORY_DELETE, method="DELETE", params=_CATEGORY_PARAMS, embedding=embedding
    )
    requester = _operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _CATEGORY_DELETE,
        "target": "vcenter-lab",
        "params": _CATEGORY_PARAMS,
    }

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert connector.writes == []

    approver = _operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resumed = await resume_dispatch_after_approval(operator=approver, request=row, params=None)

    assert resumed.status == "ok"
    assert connector.writes == [
        {"verb": "DELETE", "path": "/cis/tagging/category/cat-1", "json": None}
    ]


# ---------------------------------------------------------------------------
# Service account: a standing grant never covers a tag detach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_account_grant_never_runs_tag_detach(embedding: list[float]) -> None:
    """A live matching grant does not auto-execute a detach: the op still parks."""
    connector = _register_fake_vcenter()
    await _insert_descriptor(
        op_id=_TAG_DETACH, method="POST", params=_DETACH_PARAMS, embedding=embedding
    )
    await _seed_grant(op_id=_TAG_DETACH)
    service = _operator(sub=_PRINCIPAL, principal_kind=PrincipalKind.SERVICE)
    preview = await preview_dispatch(
        operator=service,
        connector_id=_CONNECTOR_ID,
        op_id=_TAG_DETACH,
        target=_FakeTarget(),
        params=_DETACH_PARAMS,
    )
    assert preview["status"] == "ok"

    result = await dispatch(
        operator=service,
        connector_id=_CONNECTOR_ID,
        op_id=_TAG_DETACH,
        target=_FakeTarget(),
        params=_DETACH_PARAMS,
        preview_hash=preview["preview_hash"],
    )

    assert result.status == "awaiting_approval", result
    assert connector.writes == []
    assert await _grant_use_rows() == []


@pytest.mark.asyncio
async def test_grant_for_tag_detach_is_refused_at_creation(embedding: list[float]) -> None:
    """The detach is a POST, so only the destructive tier stops a grant: it does."""
    await _insert_descriptor(
        op_id=_TAG_DETACH, method="POST", params=_DETACH_PARAMS, embedding=embedding
    )
    payload = ServiceGrantCreate(
        principal_sub=_PRINCIPAL,
        op_id=_TAG_DETACH,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended tag cleanup",
        expires_at=None,
    )

    with pytest.raises(GrantValidationError) as exc:
        await ServicePrincipalGrantService().create(_TENANT, "op-admin", payload)

    assert "destructive" in str(exc.value)

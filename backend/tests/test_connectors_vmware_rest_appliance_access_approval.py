# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""More generic vCenter changes wait for approval (#4973).

As generic (ingested) writes these operations were ``caution`` without
approval, so a person's call ran at once:

* appliance restore, update rollback, update stage-and-install, network
  identity change and network reset;
* permission writes, acting as another user and ending another user's
  session;
* TLS certificate and VMCA root replacement;
* applying a software image to hosts;
* vSAN file share, share snapshot, file service domain, iSCSI LUN and iSCSI
  target removals, and the hybrid-link delete sent as ``POST``.

The VMware floor (``ingest_safety.py``) raises the first four groups to
``dangerous`` with approval on the 8.0 and 9.0 catalogs. The removals come
from the shared delete-name rule (``meho_backplane/delete_names.py``): a
delete word after a vSAN prefix, and the ``action=delete`` query. So the
grant checks and the stored-delete review see them as deletes too.

The pinned-spec lane checks every rule against the vendor files on the spec
shelf; it skips when the shelf is not configured (see ``tests/_spec_shelf.py``)
and runs in CI.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.ingest_safety import (
    VMWARE_APPLIANCE_AND_ACCESS_WRITES,
    is_vmware_appliance_or_access_write,
    vmware_rest_safety_floor,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest._internals import ConnectorScope
from meho_backplane.operations.ingest.delete_review import build_delete_review
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.service_grants import delete_shaped_refusal_reason
from meho_backplane.settings import get_settings
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_RESTORE = "POST:/appliance/recovery/restore/job"

#: The floor rules: appliance, permission, session, certificate and image writes.
_FLOOR_OPS_ON_BOTH = frozenset(
    {
        _RESTORE,
        "POST:/appliance/update/pending?action=rollback",
        "POST:/appliance/update/pending/{version}?action=stage-and-install",
        "POST:/appliance/networking?action=change&vmw-task=true",
        "POST:/appliance/networking?action=reset",
        "PUT:/vcenter/certificate-management/vcenter/tls",
        "POST:/vcenter/certificate-management/vcenter/tls?action=replace-vmca-signed",
        "POST:/vcenter/certificate-management/vcenter/vmca-root",
    }
)
_FLOOR_OPS_9_0_ONLY = frozenset(
    {
        "POST:/AuthorizationManager/{moId}/SetEntityPermissions",
        "POST:/AuthorizationManager/{moId}/ResetEntityPermissions",
        "POST:/AuthorizationManager/{moId}/MergePermissions",
        "POST:/vcenter/authorization/permissions",
        "PATCH:/vcenter/authorization/permissions/{permission}",
        "POST:/SessionManager/{moId}/ImpersonateUser",
        "POST:/SessionManager/{moId}/TerminateSession",
        "POST:/esx/settings/inventory?action=apply&vmw-task=true",
    }
)
#: The removals the delete-name rule catches.
_VSAN_REMOVALS = frozenset(
    {
        "POST:/vsan/VsanFileServiceSystem/{moId}/VsanClusterRemoveShare",
        "POST:/vsan/VsanFileServiceSystem/{moId}/VsanClusterRemoveShareSnapshot",
        "POST:/vsan/VsanFileServiceSystem/{moId}/VsanClusterRemoveFsDomain",
        "POST:/vsan/VsanIscsiTargetSystem/{moId}/VsanVitRemoveIscsiLUN",
        "POST:/vsan/VsanIscsiTargetSystem/{moId}/VsanVitRemoveIscsiTarget",
    }
)
_HVC_LINK_DELETE = "POST:/hvc/links/{link}?action=delete"
_REMOVALS = _VSAN_REMOVALS | {_HVC_LINK_DELETE}
_ALL_GATED = _FLOOR_OPS_ON_BOTH | _FLOOR_OPS_9_0_ONLY | _REMOVALS

#: Reads and other actions next to the gated ones keep their parsed level.
_UNCHANGED = frozenset(
    {
        "POST:/vsan/VsanIscsiTargetSystem/{moId}/VsanVitGetIscsiLUNs",
        "POST:/AuthorizationManager/{moId}/RetrieveEntityPermissions",
        "POST:/vcenter/authorization/permissions?action=list",
        "POST:/appliance/update/pending/{version}?action=stage",
        "POST:/appliance/update/pending/{version}?action=precheck",
        "POST:/appliance/recovery/restore/job?action=cancel",
        "POST:/esx/settings/inventory?action=check&vmw-task=true",
        "POST:/vcenter/compute/policies?action=delete-tags-and-policies",
        "GET:/appliance/recovery/restore/job",
    }
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004973")


def _hand_made_spec() -> str:
    """One small OpenAPI file with every gated op and the neighbours that stay."""
    paths: dict[str, list[str]] = {}
    for op_id in sorted(_ALL_GATED | _UNCHANGED):
        method, path = op_id.split(":", 1)
        paths.setdefault(path, []).append(method.lower())
    lines = [
        "openapi: 3.0.3",
        "info: {title: vcenter appliance and access fixture, version: '1.0'}",
        "paths:",
    ]
    for path, methods in paths.items():
        lines.append(f"  '{path}':")
        for method in methods:
            lines.append(f"    {method}:")
            lines.append("      responses: {'200': {description: ok}}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _floored(
    protos: list[EndpointDescriptorProto], version: str
) -> dict[str, EndpointDescriptorProto]:
    return {
        p.op_id: apply_safety_floor(
            product="vmware", version=version, impl_id="vmware-rest", proto=p
        )
        for p in protos
    }


def _ingested(version: str) -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///appliance-access.yaml", content=_hand_made_spec())
    return _floored(rows, version)


def _waits_for_approval(proto: EndpointDescriptorProto) -> bool:
    return proto.safety_level in {"dangerous", "destructive"} and proto.requires_approval is True


# ---------------------------------------------------------------------------
# Ingest of a hand-made spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_every_operation_in_the_table_waits_for_approval(version: str) -> None:
    ops = _ingested(version)

    assert set(ops) == _ALL_GATED | _UNCHANGED
    not_gated = sorted(op_id for op_id in _ALL_GATED if not _waits_for_approval(ops[op_id]))
    assert not_gated == []
    for op_id in _ALL_GATED:
        assert ops[op_id].safety_level == "dangerous", op_id


@pytest.mark.parametrize("op_id", sorted(_UNCHANGED))
def test_reads_and_other_actions_keep_their_level(op_id: str) -> None:
    rows = {p.op_id: p for p in parse_openapi("file:///a.yaml", content=_hand_made_spec())}
    ops = _ingested("9.0")

    assert ops[op_id].safety_level == rows[op_id].safety_level
    assert ops[op_id].requires_approval is False


def test_floor_rules_do_not_apply_to_an_unlisted_catalog_version() -> None:
    """The floor is scoped to the 8.0 and 9.0 catalogs; the delete-name rule is not."""
    ops = _ingested("7.0")

    assert ops[_RESTORE].safety_level == "caution"
    assert ops[_RESTORE].requires_approval is False
    for op_id in _REMOVALS:
        assert _waits_for_approval(ops[op_id]), op_id


@pytest.mark.parametrize("op_id", sorted(_ALL_GATED))
def test_no_rule_lowers_a_level(op_id: str) -> None:
    """A row that is already ``destructive`` stays ``destructive`` with approval."""
    method, path = op_id.split(":", 1)
    proto = EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


#: ``(method, path, the rule accepts it)``.
_RULE_CASES: list[tuple[str, str, bool]] = [
    ("POST", "/appliance/recovery/restore/job", True),
    ("POST", "/api/appliance/recovery/restore/job", True),
    ("post", "/appliance/recovery/restore/job", True),
    ("POST", "/appliance/update/pending/{ver}?action=stage-and-install", True),
    ("POST", "/AuthorizationManager/{mo_id}/SetEntityPermissions", True),
    ("PATCH", "/api/vcenter/authorization/permissions/{id}", True),
    ("GET", "/appliance/recovery/restore/job", False),
    ("POST", "/appliance/recovery/restore/job?action=cancel", False),
    ("POST", "/appliance/update/pending/{version}?action=stage", False),
    ("POST", "/appliance/networking?action=change", False),
    ("PATCH", "/vcenter/certificate-management/vcenter/tls", False),
    ("POST", "/AuthorizationManager/{moId}/RetrieveEntityPermissions", False),
]


@pytest.mark.parametrize(("method", "path", "expected"), _RULE_CASES)
def test_is_vmware_appliance_or_access_write(method: str, path: str, expected: bool) -> None:
    assert is_vmware_appliance_or_access_write(method, path) is expected


def test_every_floor_rule_is_in_the_test_table() -> None:
    """The floor list and this test's list are the same operations."""
    listed = set()
    for op_id in _FLOOR_OPS_ON_BOTH | _FLOOR_OPS_9_0_ONLY:
        method, path = op_id.split(":", 1)
        listed.add((method, re.sub(r"\{[^/{}]+\}", "{}", path)))
    assert listed == VMWARE_APPLIANCE_AND_ACCESS_WRITES


# ---------------------------------------------------------------------------
# Grants and the stored-delete review agree with the delete-name rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", sorted(_REMOVALS))
def test_a_standing_grant_for_a_removal_is_refused(op_id: str) -> None:
    reason = delete_shaped_refusal_reason(op_id, ())
    assert reason is not None
    assert "never grantable" in reason


def _stored_row(op_id: str) -> EndpointDescriptor:
    """A row an earlier ingest stored at ``caution`` without approval."""
    method, path = op_id.split(":", 1)
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level="caution",
        requires_approval=False,
        is_enabled=True,
    )


def test_stored_delete_review_flags_the_old_removal_rows() -> None:
    scope = ConnectorScope(product="vmware", version="9.0", impl_id="vmware-rest", tenant_id=None)
    rows = [_stored_row(op_id) for op_id in sorted(_REMOVALS | {_RESTORE})]

    report = {entry.op_id: entry for entry in build_delete_review(rows, scope)}

    assert set(report) == _REMOVALS
    for entry in report.values():
        assert entry.needs_correction is True
        assert entry.expected_safety_level == "dangerous"
        assert entry.expected_requires_approval is True


# ---------------------------------------------------------------------------
# A person's call waits for approval
# ---------------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id = uuid.uuid4()
        self.name = "vcenter-test"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


class _FakeVcenter(HttpConnector):
    """vCenter stand-in that records every call it gets."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def _request_json(  # type: ignore[override]
        self, target: Any, method: str, path: str, **_kw: Any
    ) -> Any:
        self.calls.append(f"{method} {path}")
        return {}

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, verb: str = "POST", **_kw: Any
    ) -> Any:
        self.calls.append(f"{verb} {path}")
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


async def _store_ingested_restore() -> None:
    """Store the restore op with the level a fresh ingest gives it."""
    proto = _ingested("9.0")[_RESTORE]
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=_RESTORE,
                source_kind="ingested",
                method="POST",
                path="/appliance/recovery/restore/job",
                handler_ref=None,
                summary="Start a restore of the vCenter appliance.",
                tags=[],
                parameter_schema={
                    "type": "object",
                    "properties": {"body": {"type": "object", "x-meho-param-loc": "body"}},
                },
                safety_level=proto.safety_level,
                requires_approval=proto.requires_approval,
                is_enabled=True,
                embedding=[0.1] * 384,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_env")
async def test_a_persons_restore_call_waits_for_approval() -> None:
    register_connector_v2(product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVcenter)
    connector = get_or_create_connector_instance(_FakeVcenter)
    assert isinstance(connector, _FakeVcenter)
    await _store_ingested_restore()
    person = Operator(
        sub="op-requester",
        name="Restore Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )

    result = await dispatch(
        operator=person,
        connector_id="vmware-rest-9.0",
        op_id=_RESTORE,
        target=_FakeTarget(),
        params={"body": {"location_type": "HTTPS", "location": "https://backup.test/x"}},
    )

    assert result.status == "awaiting_approval", result
    assert connector.calls == []
    async with get_sessionmaker()() as session:
        count = (
            await session.execute(select(func.count()).select_from(ApprovalRequest))
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# The pinned vendor files serve the rules (shelf-gated, runs in CI)
# ---------------------------------------------------------------------------

#: ``(shelf product dir, file, catalog version, op ids the file must serve)``.
_PINNED: list[tuple[str, str, str, frozenset[str]]] = [
    (
        "vcenter-9.0",
        "vcenter.yaml",
        "9.0",
        frozenset(
            op_id for op_id in _FLOOR_OPS_ON_BOTH | _FLOOR_OPS_9_0_ONLY if "Manager/" not in op_id
        )
        | {_HVC_LINK_DELETE},
    ),
    (
        "vcenter-9.0",
        "vi-json.yaml",
        "9.0",
        frozenset(op_id for op_id in _FLOOR_OPS_9_0_ONLY if "Manager/" in op_id) | _VSAN_REMOVALS,
    ),
    ("vcenter-8.0", "vcenter.yaml", "8.0", _FLOOR_OPS_ON_BOTH | {_HVC_LINK_DELETE}),
]


@pytest.mark.parametrize(
    ("product_dir", "filename", "version", "expected"),
    _PINNED,
    ids=[f"{d}/{f}" for d, f, _, _ in _PINNED],
)
def test_pinned_vendor_spec_serves_the_rules(
    product_dir: str, filename: str, version: str, expected: frozenset[str]
) -> None:
    """Every rule matches an operation in the pinned file, and lands waiting for approval."""
    spec_path = require_shelf_spec(product_dir, filename)
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served(expected, set(ops), spec_label=f"{product_dir}/{filename}")
    not_gated = sorted(op_id for op_id in expected if not _waits_for_approval(ops[op_id]))
    assert not_gated == []

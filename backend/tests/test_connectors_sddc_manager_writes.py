# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SDDC Manager curated workload-domain write ops (#3497).

Coverage matrix (per Task #3497 acceptance criteria):

* **Registration shape.** The five WLD write ops carry the correct
  ``safety_level`` (network-pool create + validations ``caution``; host
  commission + domain create ``dangerous``), the mutating ones carry
  ``requires_approval=True`` (validations do not), each is grouped under
  ``sddc-lifecycle``, tagged ``write``, and its ``handler_attr`` resolves to
  a real bound method on the connector (the dispatcher's ``import_handler``
  contract). ``sddc.task.get`` is ``safe`` / no-approval and carries the
  JSONFlux ``result_scalars`` + ``result_digest`` hints.
* **Approval park (AC: approval-gated).** ``sddc.domain.create`` and
  ``sddc.host.commission`` are not dispatchable without the elevated policy
  path: with ``requires_approval`` set, dispatch parks at
  ``status="awaiting_approval"`` and the vendor endpoint is never hit.
* **Dispatch (AC: dispatchable + park-for-approval + audit).** The
  non-approval validation ops dispatch through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked SDDC
  Manager on the post-#2290 token session and return ``status="ok"``.
  ``sddc.task.get`` dispatches read-only and interpolates the task id.
* **Approved-path wire shape.** Called directly (the handler that runs once
  an operator approves), each write POSTs its vendor path with the caller's
  ``spec`` as the JSON body (a single object for network-pool / domain, a
  JSON array for host commission).
* **NFS-shaped DomainCreationSpec validates (AC).** The domain ops'
  parameter schema accepts a well-formed NFS-principal
  ``DomainCreationSpec`` (``...datastoreSpec.nfsDatastoreSpecs[].nasVolume``)
  and rejects a malformed ``nasVolume`` (the vSAN-reuse trap) and a missing
  ``spec``.

Mirrors :mod:`tests.test_connectors_sddc_manager_typed_reads` for the
dispatch lifecycle + embedding stub + SDDC token-session mock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_DOMAIN_CREATE_OP_ID,
    SDDC_DOMAIN_VALIDATE_OP_ID,
    SDDC_HOST_COMMISSION_OP_ID,
    SDDC_HOST_VALIDATE_OP_ID,
    SDDC_IMPL_ID,
    SDDC_NETWORK_POOL_CREATE_OP_ID,
    SDDC_PRODUCT,
    SDDC_TYPED_OPS,
    SDDC_VERSION,
    SddcManagerConnector,
    register_sddc_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings

_SDDC_HOST = "sddc-writes.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"
_TOKEN_PATH = "/v1/tokens"
_ACCESS_TOKEN = "writes-access-token"

_WRITE_OP_IDS = frozenset(
    {
        SDDC_NETWORK_POOL_CREATE_OP_ID,
        SDDC_HOST_VALIDATE_OP_ID,
        SDDC_HOST_COMMISSION_OP_ID,
        SDDC_DOMAIN_VALIDATE_OP_ID,
        SDDC_DOMAIN_CREATE_OP_ID,
    }
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SDDC_PRODUCT,
        version=SDDC_VERSION,
        impl_id=SDDC_IMPL_ID,
        cls=SddcManagerConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _SddcWriteTarget:
    """Target satisfying both ``SddcTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = SDDC_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SDDC_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "sddc-writes"
        self.host = _SDDC_HOST
        self.port = 443
        self.secret_ref = "targets/op-writes/sddc-writes"
        self.auth_model = "shared_service_account"
        self.sso_realm = "vsphere.local"


def _make_operator() -> Operator:
    return Operator(
        sub="op-writes-sddc",
        name="SDDC Writes Operator",
        email=None,
        raw_jwt="op.writes.sddc.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _sddc_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "sddc-writes-svc", "password": "sddc-writes-pw"}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> SddcManagerConnector:
    await register_sddc_typed_operations()
    instance = get_or_create_connector_instance(SddcManagerConnector)
    instance._credentials_loader = _sddc_credentials_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Canonical NFS-principal DomainCreationSpec (the Envision estate shape)
# ---------------------------------------------------------------------------

#: A well-formed NFS-principal DomainCreationSpec: the datastore carries
#: ``nfsDatastoreSpecs[].nasVolume`` (serverName[] / path / readOnly), NOT
#: ``vsanDatastoreSpec`` (the trap the field notes warn about — reusing a
#: vSAN spec and swapping only the datastore).
_NFS_DOMAIN_SPEC: dict[str, Any] = {
    "domainName": "wld-envision-01",
    "vcenterSpec": {"name": "wld-vc01", "networkDetailsSpec": {}},
    "computeSpec": {
        "clusterSpecs": [
            {
                "name": "wld-cl01",
                "hostSpecs": [{"id": "host-1"}, {"id": "host-2"}],
                "datastoreSpec": {
                    "nfsDatastoreSpecs": [
                        {
                            "datastoreName": "wld-nfs01",
                            "nasVolume": {
                                "serverName": ["10.6.7.10"],
                                "path": "/exports/wld01",
                                "readOnly": False,
                            },
                        }
                    ]
                },
                "networkSpec": {},
            }
        ]
    },
}


def _domain_ops() -> list[Any]:
    return [
        o
        for o in SDDC_TYPED_OPS
        if o.op_id in (SDDC_DOMAIN_CREATE_OP_ID, SDDC_DOMAIN_VALIDATE_OP_ID)
    ]


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


def test_write_ops_registration_shape() -> None:
    """The five WLD write ops carry the correct safety/approval/group/tag shape."""
    by_id = {o.op_id: o for o in SDDC_TYPED_OPS}
    expected = {
        SDDC_NETWORK_POOL_CREATE_OP_ID: ("caution", True),
        SDDC_HOST_VALIDATE_OP_ID: ("caution", False),
        SDDC_HOST_COMMISSION_OP_ID: ("dangerous", True),
        SDDC_DOMAIN_VALIDATE_OP_ID: ("caution", False),
        SDDC_DOMAIN_CREATE_OP_ID: ("dangerous", True),
    }
    for op_id, (safety, approval) in expected.items():
        op = by_id[op_id]
        assert op.safety_level == safety, op_id
        assert op.requires_approval is approval, op_id
        assert op.group_key == "sddc-lifecycle", op_id
        assert "write" in op.tags, op_id
        assert op.llm_instructions and op.llm_instructions["when_to_use"], op_id


def test_write_op_handlers_resolve_to_connector_bound_methods() -> None:
    """Every write op's handler_attr names a real async method (dispatcher binds it)."""
    for op in SDDC_TYPED_OPS:
        if op.op_id in _WRITE_OP_IDS or op.op_id == "sddc.task.get":
            handler = getattr(SddcManagerConnector, op.handler_attr, None)
            assert handler is not None, op.op_id
            assert callable(handler), op.op_id


def test_task_get_registration_shape() -> None:
    """sddc.task.get is a safe read carrying the JSONFlux reduction hints."""
    op = next(o for o in SDDC_TYPED_OPS if o.op_id == "sddc.task.get")
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == "sddc-tasks-typed"
    instr = op.llm_instructions
    assert instr is not None
    assert instr["result_scalars"]["keys"] == ["id", "name", "status", "type", "creationTimestamp"]
    assert instr["result_digest"]["collection"] == "subTasks"
    assert "FAILED" in instr["result_digest"]["failed_states"]


def test_validation_ops_carry_result_scalars() -> None:
    """The validation ops keep the Validation poll keys on a reduced result."""
    for op_id in (SDDC_HOST_VALIDATE_OP_ID, SDDC_DOMAIN_VALIDATE_OP_ID):
        op = next(o for o in SDDC_TYPED_OPS if o.op_id == op_id)
        assert op.llm_instructions is not None
        assert op.llm_instructions["result_scalars"]["keys"] == [
            "id",
            "description",
            "executionStatus",
            "resultStatus",
        ]


# ---------------------------------------------------------------------------
# NFS-shaped DomainCreationSpec validates (acceptance criterion)
# ---------------------------------------------------------------------------


def test_nfs_shaped_domain_spec_validates() -> None:
    """A well-formed NFS-principal DomainCreationSpec passes the domain ops' schema."""
    for op in _domain_ops():
        errors = list(validate_params(op.parameter_schema, {"spec": _NFS_DOMAIN_SPEC}))
        assert errors == [], (op.op_id, errors)


def test_malformed_nfs_nas_volume_is_rejected() -> None:
    """An NFS spec whose nasVolume drops the required `path` fails validation.

    Guards the vSAN-reuse trap: the schema meaningfully checks the NFS
    sub-shape rather than waving any object through.
    """
    bad_spec = {
        "domainName": "wld-bad",
        "vcenterSpec": {"name": "vc"},
        "computeSpec": {
            "clusterSpecs": [
                {
                    "datastoreSpec": {
                        "nfsDatastoreSpecs": [
                            {"nasVolume": {"serverName": ["10.6.7.10"], "readOnly": False}}
                        ]
                    }
                }
            ]
        },
    }
    for op in _domain_ops():
        errors = list(validate_params(op.parameter_schema, {"spec": bad_spec}))
        assert errors, op.op_id


def test_domain_ops_require_spec() -> None:
    """A missing `spec` param fails validation for both domain ops."""
    for op in _domain_ops():
        errors = list(validate_params(op.parameter_schema, {}))
        assert errors, op.op_id


# ---------------------------------------------------------------------------
# Approval park — the mutating writes are not dispatchable without approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "path", "spec"),
    [
        (SDDC_DOMAIN_CREATE_OP_ID, "/v1/domains", _NFS_DOMAIN_SPEC),
        (
            SDDC_HOST_COMMISSION_OP_ID,
            "/v1/hosts",
            [
                {
                    "fqdn": "esxi-1.wld.test",
                    "username": "root",
                    "password": "pw",
                    "storageType": "NFS",
                    "networkPoolId": "np-01",
                }
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_mutating_write_parks_for_approval(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
    spec: Any,
) -> None:
    """A dangerous + requires_approval write parks; the vendor endpoint isn't hit."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        write_route = mock.post(path).respond(202, json={"id": "task-x", "status": "IN_PROGRESS"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id=op_id,
            target=_SddcWriteTarget(),
            params={"spec": spec},
        )

    assert result.status == "awaiting_approval", result
    assert not write_route.called


# ---------------------------------------------------------------------------
# Dispatch — the non-approval validation ops + the task poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_validate_dispatches(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """sddc.domain.validate (caution, no approval) POSTs the spec and returns ok."""
    await _register_and_resolve(_stub_embedding)
    validation = {"id": "val-1", "executionStatus": "IN_PROGRESS", "resultStatus": "UNKNOWN"}

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.post("/v1/domains/validations").respond(202, json=validation)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id=SDDC_DOMAIN_VALIDATE_OP_ID,
            target=_SddcWriteTarget(),
            params={"spec": _NFS_DOMAIN_SPEC},
        )

    assert result.status == "ok", result.error
    assert result.result == validation
    assert route.called and route.call_count == 1
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {_ACCESS_TOKEN}"


@pytest.mark.asyncio
async def test_task_get_dispatches_and_builds_path_from_id(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """sddc.task.get (safe) interpolates the task id and returns the Task."""
    await _register_and_resolve(_stub_embedding)
    task = {"id": "task-42", "name": "Commission Hosts", "status": "IN_PROGRESS", "subTasks": []}

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.get("/v1/tasks/task-42").respond(200, json=task)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id="sddc.task.get",
            target=_SddcWriteTarget(),
            params={"id": "task-42"},
        )

    assert result.status == "ok", result.error
    assert result.result == task
    assert route.called and route.call_count == 1


# ---------------------------------------------------------------------------
# Approved-path wire shape — the handler that runs once an operator approves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_pool_create_posts_spec_as_object_body(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The approved network-pool create POSTs the NetworkPool spec as the JSON body."""
    instance = await _register_and_resolve(_stub_embedding)
    pool = {"name": "wld-np01", "networks": [{"type": "VMOTION"}, {"type": "NFS"}]}

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.post("/v1/network-pools").respond(201, json={"id": "np-9", **pool})
        result = await instance.network_pool_create(
            _make_operator(), _SddcWriteTarget(), {"spec": pool}
        )

    assert result["id"] == "np-9"
    assert route.called
    import json as _json

    assert _json.loads(route.calls[0].request.content) == pool


@pytest.mark.asyncio
async def test_host_commission_posts_spec_as_array_body(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The approved host commission POSTs the HostCommissionSpec **array** verbatim."""
    instance = await _register_and_resolve(_stub_embedding)
    hosts = [
        {
            "fqdn": "esxi-1.wld.test",
            "username": "root",
            "password": "pw1",
            "storageType": "NFS",
            "networkPoolId": "np-01",
        },
        {
            "fqdn": "esxi-2.wld.test",
            "username": "root",
            "password": "pw2",
            "storageType": "NFS",
            "networkPoolId": "np-01",
        },
    ]

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.post("/v1/hosts").respond(202, json={"id": "task-hc", "status": "IN_PROGRESS"})
        result = await instance.host_commission(
            _make_operator(), _SddcWriteTarget(), {"spec": hosts}
        )

    assert result["id"] == "task-hc"
    assert route.called
    import json as _json

    body = _json.loads(route.calls[0].request.content)
    assert isinstance(body, list) and len(body) == 2
    assert body[0]["fqdn"] == "esxi-1.wld.test"


@pytest.mark.asyncio
async def test_domain_create_posts_nfs_spec_as_object_body(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The approved domain create POSTs the NFS-shaped DomainCreationSpec verbatim."""
    instance = await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.post("/v1/domains").respond(
            202, json={"id": "task-dc", "status": "IN_PROGRESS"}
        )
        result = await instance.domain_create(
            _make_operator(), _SddcWriteTarget(), {"spec": _NFS_DOMAIN_SPEC}
        )

    assert result["id"] == "task-dc"
    assert route.called
    import json as _json

    body = _json.loads(route.calls[0].request.content)
    nfs = body["computeSpec"]["clusterSpecs"][0]["datastoreSpec"]["nfsDatastoreSpecs"][0]
    assert nfs["nasVolume"]["path"] == "/exports/wld01"

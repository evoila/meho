# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vSphere Replication connector: login, ``vr.about``, ``vr.api_file.get``.

What is covered, with a respx-mocked appliance and no ingested catalog:

* the login (``POST /api/rest/vr/v2/session`` with HTTP Basic ->
  ``session_id``) and the ``x-dr-session`` header on later calls;
* ``vr.about`` through ``dispatch()``: the server summary, certificates
  reduced to a short view; a vendor error reaches the answer;
* two targets on one address, as on a VCF 9.1 Protection and Recovery
  appliance: an ``srm`` target and a ``vr`` target resolve to the two
  connector classes, and each uses its own login;
* a ``401`` on the ``vr`` target makes a new login for that target only;
  the ``srm`` target keeps its session;
* an ``srm.*`` op with a ``vr`` target, and a ``vr.*`` op with an ``srm``
  target, end with an error and send no HTTP request; each product's
  stored handler names its own connector class;
* ``vr.api_file.get`` for both files; an unknown file name sends no HTTP
  request; the pinned 9.1 vendor file comes back with its example
  secrets replaced (only when the spec shelf is present);
* no password or session id in a result; registration shape; fingerprint.

The login, session cache and API file read are shared with the SRM
connector (``connectors/vmware_shared/dr_rest.py``, ``dr_api_file.py``); their
edge cases are tested in ``test_connectors_srm.py``.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_secrets import REDACTED
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.srm import (
    SRM_CONNECTOR_ID,
    SRM_IMPL_ID,
    SRM_PRODUCT,
    SRM_VERSION,
    SrmConnector,
    register_srm_typed_operations,
)
from meho_backplane.connectors.vr import (
    VR_CONNECTOR_ID,
    VR_IMPL_ID,
    VR_PRODUCT,
    VR_TYPED_OPS,
    VR_VERSION,
    VrConnector,
    register_vr_typed_operations,
)
from meho_backplane.connectors.vr.connector import API_FILES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings
from tests._spec_shelf import require_shelf_spec

_HOST = "vr-typed.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSION = "/api/rest/vr/v2/session"
_INFO = "/api/rest/vr/v2/info"
_VR_FILE = "/api/rest/vr/v2/open-api.yaml"
_CONFIGURE_FILE = "/api/rest/configure/v2/open-api.yaml"
_SRM_SESSION = "/api/rest/srm/v2/session"
_SRM_INFO = "/api/rest/srm/v2/server/info"
_PASSWORD = "typed-password-value"

_PEM = "-----BEGIN CERTIFICATE-----\nMIIBmadeupcertificatetext\n-----END CERTIFICATE-----"
_CERT = {
    "serial_number": "0d1e2f",
    "not_after": 1893456000000,
    "not_before": 1700000000000,
    "subject_dn": "CN=<vr-name>",
    "issuer_dn": "CN=<ca-name>",
    "thumbprint": "DD:EE:FF",
    "certificate": _PEM,
    "public_key": "made-up-public-key",
    "signature": "made-up-signature",
}
_INFO_PAYLOAD: dict[str, Any] = {
    "id": "vr-id-0001",
    "url": "https://<vr-name>:443",
    "name": "<vr-name>",
    "server_status": "OK",
    "version": "9.1.1.0",
    "build_number": "22222222",
    "vc_guid": "vc-guid-0001",
    "vc_name": "<vcenter-name>",
    "extension_key": "com.vmware.vcHms",
    "certificates": [_CERT],
    "certificate_warnings": [],
    "_meta": {"errors": []},
}
_SRM_INFO_PAYLOAD: dict[str, Any] = {**_INFO_PAYLOAD, "id": "srm-id-0001", "name": "<srm-name>"}
_YAML = "openapi: 3.0.1\ninfo:\n  title: made-up API\n  version: 2.0.0\npaths: {}\n"


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
        product=VR_PRODUCT, version=VR_VERSION, impl_id=VR_IMPL_ID, cls=VrConnector
    )
    register_connector_v2(
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
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
    async with get_sessionmaker()() as s:
        yield s


class _Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(self, *, product: str = VR_PRODUCT, version: str = VR_VERSION) -> None:
        self.product = product
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = f"{product}-typed-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/dr-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-vr",
        name="VR Reads Operator",
        email=None,
        raw_jwt="op.reads.vr.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "dr-typed-svc", "password": _PASSWORD}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> VrConnector:
    await register_vr_typed_operations()
    instance = get_or_create_connector_instance(VrConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, VrConnector)
    return instance


def _srm_instance() -> SrmConnector:
    instance = get_or_create_connector_instance(SrmConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, SrmConnector)
    return instance


def _login(session_id: str = "vr-session-1") -> httpx.Response:
    return httpx.Response(200, json={"session_id": session_id})


def _mock_login_and_info(mock: respx.MockRouter) -> None:
    mock.post(_SESSION).mock(return_value=_login())
    mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)


async def _dispatch(op_id: str, params: dict[str, Any], target: _Target | None = None) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=VR_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params,
    )


async def _dispatch_srm_about(target: _Target) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=SRM_CONNECTOR_ID,
        op_id="srm.about",
        target=target,
        params={},
    )


# ---------------------------------------------------------------------------
# Login and vr.about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_logs_in_with_basic_and_returns_the_server_summary(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION).mock(return_value=_login())
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        result = await _dispatch("vr.about", {})

    assert result.status == "ok", result.error
    assert login.call_count == 1
    assert login.calls[0].request.headers["authorization"].startswith("Basic ")
    assert info.calls[0].request.headers["x-dr-session"] == "vr-session-1"
    body = result.result
    assert body["version"] == "9.1.1.0"
    assert body["build_number"] == "22222222"
    assert body["server_status"] == "OK"
    assert body["extension_key"] == "com.vmware.vcHms"
    assert body["certificates"] == [
        {
            "subject_dn": "CN=<vr-name>",
            "issuer_dn": "CN=<ca-name>",
            "serial_number": "0d1e2f",
            "not_before": 1700000000000,
            "not_after": 1893456000000,
            "thumbprint": "DD:EE:FF",
        }
    ]
    assert "_meta" not in body
    assert _PEM not in json.dumps(body)


@pytest.mark.asyncio
async def test_login_401_surfaces_an_error_and_sends_no_info_read(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).respond(401, json={"error_code": "made-up"})
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        result = await _dispatch("vr.about", {})

    assert result.status == "error"
    assert info.call_count == 0
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_vendor_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(_INFO).respond(403, json={"error_code": "made-up", "message": "no access"})
        result = await _dispatch("vr.about", {})

    assert result.status == "error"
    assert "403" in (result.error or "") + json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# Two targets on one 9.1 appliance: srm and vr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_srm_and_vr_targets_on_one_address_resolve_to_their_own_class_and_login(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    vr = await _register_and_resolve(_stub_embedding)
    await register_srm_typed_operations()
    srm = _srm_instance()
    srm_target = _Target(product=SRM_PRODUCT, version=SRM_VERSION)
    vr_target = _Target()
    assert srm_target.host == vr_target.host

    assert resolve_connector(srm_target) is SrmConnector
    assert resolve_connector(vr_target) is VrConnector

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        srm_login = mock.post(_SRM_SESSION).mock(return_value=_login("srm-session-own"))
        srm_info = mock.get(_SRM_INFO).respond(200, json=_SRM_INFO_PAYLOAD)
        vr_login = mock.post(_SESSION).mock(return_value=_login("vr-session-own"))
        vr_info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        srm_result = await _dispatch_srm_about(srm_target)
        vr_result = await _dispatch("vr.about", {}, vr_target)

    assert srm_result.status == "ok", srm_result.error
    assert vr_result.status == "ok", vr_result.error
    assert srm_result.result["name"] == "<srm-name>"
    assert vr_result.result["name"] == "<vr-name>"
    assert srm_login.call_count == 1
    assert vr_login.call_count == 1
    assert srm_info.calls[0].request.headers["x-dr-session"] == "srm-session-own"
    assert vr_info.calls[0].request.headers["x-dr-session"] == "vr-session-own"
    assert srm is not vr
    assert list(srm._session_ids.values()) == ["srm-session-own"]
    assert list(vr._session_ids.values()) == ["vr-session-own"]


@pytest.mark.asyncio
async def test_401_on_the_vr_target_logs_in_again_for_it_only(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    await register_srm_typed_operations()
    _srm_instance()
    srm_target = _Target(product=SRM_PRODUCT, version=SRM_VERSION)
    vr_target = _Target()
    rejected: set[str] = set()

    def _vr_info(request: httpx.Request) -> httpx.Response:
        if request.headers["x-dr-session"] in rejected:
            return httpx.Response(401)
        return httpx.Response(200, json=_INFO_PAYLOAD)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        srm_login = mock.post(_SRM_SESSION).mock(return_value=_login("srm-1"))
        srm_info = mock.get(_SRM_INFO).respond(200, json=_SRM_INFO_PAYLOAD)
        vr_login = mock.post(_SESSION)
        vr_login.side_effect = [_login("vr-1"), _login("vr-2")]
        vr_info = mock.get(_INFO).mock(side_effect=_vr_info)

        assert (await _dispatch_srm_about(srm_target)).status == "ok"
        assert (await _dispatch("vr.about", {}, vr_target)).status == "ok"
        rejected.add("vr-1")
        retried = await _dispatch("vr.about", {}, vr_target)
        srm_again = await _dispatch_srm_about(srm_target)

    assert retried.status == "ok", retried.error
    assert srm_again.status == "ok", srm_again.error
    assert vr_login.call_count == 2
    assert srm_login.call_count == 1
    assert [c.request.headers["x-dr-session"] for c in vr_info.calls] == ["vr-1", "vr-1", "vr-2"]
    assert [c.request.headers["x-dr-session"] for c in srm_info.calls] == ["srm-1", "srm-1"]


# ---------------------------------------------------------------------------
# An op of one product never runs on the other product's target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connector_id", "op_id", "params", "target_product", "target_version"),
    [
        (SRM_CONNECTOR_ID, "srm.about", {}, VR_PRODUCT, VR_VERSION),
        (SRM_CONNECTOR_ID, "srm.api_file.get", {"file": "srm"}, VR_PRODUCT, VR_VERSION),
        (VR_CONNECTOR_ID, "vr.about", {}, SRM_PRODUCT, SRM_VERSION),
        (VR_CONNECTOR_ID, "vr.api_file.get", {"file": "vr"}, SRM_PRODUCT, SRM_VERSION),
    ],
)
async def test_an_op_of_one_product_sends_nothing_to_the_other_products_target(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
    target_product: str,
    target_version: str,
) -> None:
    """``srm.*`` with a ``vr`` target, and the reverse, stop before any HTTP request.

    The target resolves to the other product's connector. The stored
    handler names the op's own class, so the dispatcher cannot bind it to
    that connector and answers with an error instead of running the other
    product's read under this op's name.
    """
    await _register_and_resolve(_stub_embedding)
    await register_srm_typed_operations()
    _srm_instance()
    target = _Target(product=target_product, version=target_version)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.route().respond(200, json={"session_id": "never-used"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    assert result.status == "error"
    assert result.op_id == op_id
    assert result.error is not None and result.error.startswith("connector_error")
    assert mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_each_product_stores_a_handler_on_its_own_class(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_vr_typed_operations()
    await register_srm_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product.in_([VR_PRODUCT, SRM_PRODUCT]),
                    EndpointDescriptor.impl_id.in_([VR_IMPL_ID, SRM_IMPL_ID]),
                    EndpointDescriptor.op_id.in_(
                        ["vr.about", "vr.api_file.get", "srm.about", "srm.api_file.get"]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    class_by_product = {VR_PRODUCT: "VrConnector", SRM_PRODUCT: "SrmConnector"}
    for row in rows:
        assert row.handler_ref is not None
        assert f".{class_by_product[row.product]}." in row.handler_ref, row.handler_ref
    shared = [row for row in rows if row.op_id.endswith((".about", ".api_file.get"))]
    assert len(shared) == 4
    for row in shared:
        attr = "about" if row.op_id.endswith(".about") else "api_file_get"
        assert row.handler_ref.endswith(f".{class_by_product[row.product]}.{attr}"), row.handler_ref


# ---------------------------------------------------------------------------
# vr.api_file.get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "path"), [("vr", _VR_FILE), ("configure", _CONFIGURE_FILE)])
async def test_api_file_returns_status_size_sha_version_and_parts(
    _stub_embedding: AsyncMock, session: AsyncSession, name: str, path: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        route = mock.get(path).respond(
            200, text=_YAML, headers={"Content-Type": "application/yaml"}
        )
        result = await _dispatch("vr.api_file.get", {"file": name})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert route.calls[0].request.headers["x-dr-session"] == "vr-session-1"
    body = result.result
    assert body["file"] == name
    assert body["path"] == path == API_FILES[name]
    assert body["status"] == 200
    assert body["size"] == len(_YAML.encode())
    assert body["sha256"] == hashlib.sha256(_YAML.encode()).hexdigest()
    assert body["version"] == "9.1.1.0"
    assert body["build"] == "22222222"
    assert body["version_error"] is None
    assert body["parts"] == 1
    assert body["items"] == [{"index": 0, "text": _YAML}]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["srm", "other", "../vr", "/api/rest/vr/v2/open-api.yaml", ""])
async def test_unknown_file_name_sends_no_http_request(
    _stub_embedding: AsyncMock, session: AsyncSession, name: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        result = await _dispatch("vr.api_file.get", {"file": name})
        sent = len(mock.calls)

    assert result.status == "error"
    assert sent == 0


@pytest.mark.asyncio
async def test_401_on_the_file_makes_a_new_login_and_the_read_then_succeeds(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("vr-session-1"), _login("vr-session-2")]
        mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        route = mock.get(_VR_FILE)
        route.side_effect = [httpx.Response(401), httpx.Response(200, text=_YAML)]
        result = await _dispatch("vr.api_file.get", {"file": "vr"})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert route.calls[0].request.headers["x-dr-session"] == "vr-session-1"
    assert route.calls[1].request.headers["x-dr-session"] == "vr-session-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "spec_file", "expected_replaced"),
    [("vr", "replication-vr-v2.yaml", 7), ("configure", "appliance-configure-v2.yaml", 10)],
)
async def test_pinned_vendor_file_comes_back_through_the_read(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    name: str,
    spec_file: str,
    expected_replaced: int,
) -> None:
    """The pinned 9.1 vendor files come back, with their example secrets replaced.

    Runs only when the spec shelf is present. The vSphere Replication file
    has ``Authorization: Basic ...`` examples in its description text; the
    read returns them replaced instead of stopping, and the parts join back
    to the copy that ``size`` and ``sha256`` describe.
    """
    spec_path = require_shelf_spec("vcf-protection-and-recovery-9.1", spec_file)
    content = spec_path.read_bytes()
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(API_FILES[name]).respond(
            200, content=content, headers={"Content-Type": "application/yaml"}
        )
        result = await _dispatch("vr.api_file.get", {"file": name})

    assert result.status == "ok", result.error
    body = result.result
    assert body["secrets_replaced"] == expected_replaced
    assert body["parts"] > 1
    assert [item["index"] for item in body["items"]] == list(range(body["parts"]))
    assert all(len(item["text"].encode()) <= 64 * 1024 for item in body["items"])
    joined = "".join(item["text"] for item in body["items"]).encode()
    assert body["size"] == len(joined)
    assert body["sha256"] == hashlib.sha256(joined).hexdigest()


# ---------------------------------------------------------------------------
# Secrets, registration shape, fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_hold_no_password_or_session_id(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_VR_FILE).respond(200, text=_YAML)
        about = await _dispatch("vr.about", {})
        api_file = await _dispatch("vr.api_file.get", {"file": "vr"})

    for result in (about, api_file):
        assert result.status == "ok", result.error
        rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
        assert _PASSWORD not in rendered
        assert "vr-session-1" not in rendered


@pytest.mark.asyncio
async def test_registered_ops_are_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_vr_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VR_PRODUCT,
                    EndpointDescriptor.impl_id == VR_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in VR_TYPED_OPS}
    assert {r.op_id for r in rows} == {
        "vr.about",
        "vr.api_file.get",
        "vr.pairing.list",
        "vr.replication.list",
        "vr.replication.issues",
        "vr.vm.check",
        "vr.task.get",
    }
    for row in rows:
        assert row.source_kind == "typed"
        assert row.handler_ref is not None
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.parameter_schema["additionalProperties"] is False
        assert len(row.parameter_schema["examples"]) == 1
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
        assert "\n" not in row.summary and len(row.description) <= 300
    by_id = {row.op_id: row for row in rows}
    file_schema = by_id["vr.api_file.get"].parameter_schema["properties"]["file"]
    assert file_schema["enum"] == list(API_FILES)


def test_package_registers_versioned_connector_and_wildcard() -> None:
    import meho_backplane.connectors.vr  # noqa: F401  (import-time registration)

    clear_registry()
    register_connector_v2(
        product=VR_PRODUCT, version=VR_VERSION, impl_id=VR_IMPL_ID, cls=VrConnector
    )
    register_connector_v2(product=VR_PRODUCT, version="", impl_id="", cls=VrConnector)
    registry = all_connectors_v2()
    assert registry[(VR_PRODUCT, VR_VERSION, VR_IMPL_ID)] is VrConnector
    assert registry[(VR_PRODUCT, "", "")] is VrConnector
    assert VrConnector.supported_version_range == ">=8.8,<10.0"
    assert VR_CONNECTOR_ID == "vr-rest-9.1"


@pytest.mark.parametrize("version", ["8.8", "9.0", "9.1"])
def test_each_covered_version_resolves_to_the_connector(version: str) -> None:
    assert resolve_connector(_Target(version=version)) is VrConnector


@pytest.mark.asyncio
async def test_fingerprint_reports_version_and_unreachable_shape() -> None:
    connector = VrConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        fp = await connector.fingerprint(_Target(), _make_operator())
    assert (fp.vendor, fp.product, fp.reachable) == ("vmware", "vr", True)
    assert (fp.version, fp.build) == ("9.1.1.0", "22222222")
    assert fp.probe_method == f"GET {_INFO}"
    await connector.aclose()

    connector = VrConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).respond(401)
        fp = await connector.fingerprint(_Target(), _make_operator())
        probe = await connector.probe(_Target())
    assert fp.reachable is False
    assert "401" in str(fp.extras["error"])
    assert probe.ok is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_rejects_other_auth_models() -> None:
    connector = VrConnector(session_loader=_session_loader)
    target = _Target()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, _make_operator())


# ---------------------------------------------------------------------------
# The flight recorder and the 401 body: only the checked copy leaves the read
# ---------------------------------------------------------------------------

_RECORDER_SECRET = "made-up-recorder-secret"
_SECRET_YAML = (
    "openapi: 3.0.1\ninfo:\n  title: made-up API\n  version: 2.0.0\n"
    f"  x-example:\n    password: {_RECORDER_SECRET}\npaths: {{}}\n"
)


def _recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Catch what the read hands the flight recorder."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        flight_recorder_capture,
        "record_vendor_call",
        lambda _span, **kwargs: recorded.append(kwargs),
    )
    return recorded


def _file_calls(recorded: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    return [call for call in recorded if call["response"].url.path == path]


async def _audit_payloads(op_id: str) -> list[str]:
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [json.dumps(row.payload, default=str) for row in rows]


@pytest.mark.asyncio
async def test_flight_recorder_gets_the_checked_file_not_the_password(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_VR_FILE).respond(200, text=_SECRET_YAML)
        result = await _dispatch("vr.api_file.get", {"file": "vr"})

    assert result.status == "ok", result.error
    (call,) = _file_calls(recorded, _VR_FILE)
    assert REDACTED.encode() in call["response_body"]
    assert _RECORDER_SECRET.encode() not in call["response_body"]


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_file_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_VR_FILE).respond(200, text="info: {password: " + _RECORDER_SECRET)
        result = await _dispatch("vr.api_file.get", {"file": "vr"})

    assert result.status == "error"
    (call,) = _file_calls(recorded, _VR_FILE)
    assert call["response_body"] == b""


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_stored_record_does_not_fit(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file fits the download limit, but its stored record does not: no body is recorded."""
    recorded = _recorded_calls(monkeypatch)
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "1000")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    data = b'"' + b'\\"' * 494 + b'"'
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_VR_FILE).respond(200, content=data)
        result = await _dispatch("vr.api_file.get", {"file": "vr"})

    assert result.status == "error"
    assert "does not fit in one result handle" in result.extras["exception_message"]
    (call,) = _file_calls(recorded, _VR_FILE)
    assert call["response"].status_code == 200
    assert call["response_body"] == b""


@pytest.mark.asyncio
async def test_401_body_is_checked_before_it_is_raised_and_the_retry_still_runs(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("vr-session-1"), _login("vr-session-2")]
        mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        route = mock.get(_VR_FILE).respond(401, text=_SECRET_YAML)
        result = await _dispatch("vr.api_file.get", {"file": "vr"})

    assert result.status == "error"
    assert "401" in (result.error or "")
    assert login.call_count == 2
    assert route.call_count == 2
    rendered = (
        (result.error or "") + json.dumps(result.result) + json.dumps(result.extras, default=str)
    )
    assert _RECORDER_SECRET not in rendered
    calls = _file_calls(recorded, _VR_FILE)
    assert len(calls) == 2
    for call in calls:
        assert REDACTED.encode() in call["response_body"]
        assert _RECORDER_SECRET.encode() not in call["response_body"]
    payloads = await _audit_payloads("vr.api_file.get")
    assert payloads
    assert all(_RECORDER_SECRET not in payload for payload in payloads)

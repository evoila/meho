# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SDDC Manager audited-read typed ops (#2306).

Coverage matrix (per Task #2306 acceptance criteria):

* **Zero-catalog typed dispatch (AC #1).** Each audited read dispatches
  through :func:`~meho_backplane.operations.dispatch` against a
  respx-mocked SDDC Manager -- with **only** the typed registrar run (no
  ingested descriptor rows) -- and returns ``status="ok"`` on the
  post-#2290 token session (``POST /v1/tokens`` -> Bearer). The persisted
  descriptors carry ``source_kind="typed"``.
* **Credential-read gating (AC #2).** ``sddc.credential.list`` is not
  dispatchable without the elevated policy path: with ``requires_approval``
  set, dispatch parks at ``status="awaiting_approval"`` and the
  ``/v1/credentials`` endpoint is never hit. Its op-id classifies as
  ``credential_read`` (audit/broadcast redaction), and the handler scrubs
  every secret-keyed value so no credential material rides the result.
* **Session recovery via the #2067 seam (AC #3).** A 401 on the first
  downstream GET is recovered by the dispatcher's auth-class arm calling
  the connector's public ``invalidate_session`` hook (#2290) and
  re-dispatching once; the op returns ``status="ok"``.
* **Registration-shape invariants.** The 11 non-gated reads carry
  ``safety_level="safe"``, ``requires_approval=False``, a ``read-only``
  tag, and non-empty llm_instructions; no write op is registered.

Mirrors :mod:`tests.test_connectors_nsx_typed_reads` for the dispatch
lifecycle + embedding stub and
:mod:`tests.test_connectors_sddc_manager_session_dispatch` for the SDDC
token-session mock + credentials-loader stub.
"""

from __future__ import annotations

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
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_IMPL_ID,
    SDDC_PRODUCT,
    SDDC_TYPED_OPS,
    SDDC_VERSION,
    SddcManagerConnector,
    register_sddc_typed_operations,
)
from meho_backplane.connectors.sddc_manager.typed_reads import REDACTED
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_SDDC_HOST = "sddc-typed.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"
_TOKEN_PATH = "/v1/tokens"
_ACCESS_TOKEN = "typed-access-token"

_CREDENTIAL_OP_ID = "sddc.credential.list"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
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
    """Deterministic embedding stub so registration/search don't pull ONNX."""
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
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _SddcReadTarget:
    """Target satisfying both ``SddcTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = SDDC_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SDDC_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
        self.name = "sddc-typed"
        self.host = _SDDC_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/sddc-typed"
        self.auth_model = "shared_service_account"
        self.sso_realm = "vsphere.local"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-sddc",
        name="SDDC Reads Operator",
        email=None,
        raw_jwt="op.reads.sddc.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _sddc_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Static credentials loader -- bypasses the live operator-context Vault read."""
    return {"username": "sddc-typed-svc", "password": "sddc-typed-pw"}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> SddcManagerConnector:
    """Run the typed registrar (only) and return the credentials-stubbed instance."""
    await register_sddc_typed_operations()
    instance = get_or_create_connector_instance(SddcManagerConnector)
    instance._credentials_loader = _sddc_credentials_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# AC #1 -- zero-catalog typed dispatch (the 10 no-param reads)
# ---------------------------------------------------------------------------

_ENVELOPE: dict[str, Any] = {
    "elements": [{"id": "row-1"}],
    "pageMetadata": {"totalElements": 1},
}
_SYSTEM_PAYLOAD: dict[str, Any] = {"proxyConfiguration": {"isEnabled": False}}


@pytest.mark.parametrize(
    ("op_id", "path"),
    [
        ("sddc.domain.list", "/v1/domains"),
        ("sddc.cluster.list", "/v1/clusters"),
        ("sddc.host.list", "/v1/hosts"),
        ("sddc.vcenter.list", "/v1/vcenters"),
        ("sddc.nsxt_cluster.list", "/v1/nsxt-clusters"),
        ("sddc.task.list", "/v1/tasks"),
        ("sddc.system.info", "/v1/system"),
        ("sddc.vcf_service.list", "/v1/vcf-services"),
        ("sddc.manager.list", "/v1/sddc-managers"),
        ("sddc.license.list", "/v1/license-keys"),
    ],
)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
) -> None:
    """AC #1: each audited read dispatches typed with zero ingested catalog state."""
    await _register_and_resolve(_stub_embedding)
    payload = _SYSTEM_PAYLOAD if op_id == "sddc.system.info" else _ENVELOPE

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id=op_id,
            target=_SddcReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    # SDDC Manager is token-only: the session mint ran and the Bearer token
    # rides the read -- never HTTP Basic.
    assert token_route.called
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {_ACCESS_TOKEN}"


@pytest.mark.asyncio
async def test_domain_status_builds_path_from_id(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: sddc.domain.status interpolates the domain id into the path."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        route = mock.get("/v1/domains/domain-mgmt/status").respond(200, json={"status": "ACTIVE"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id="sddc.domain.status",
            target=_SddcReadTarget(),
            params={"id": "domain-mgmt"},
        )

    assert result.status == "ok", result.error
    assert result.result == {"status": "ACTIVE"}
    assert route.called and route.call_count == 1


@pytest.mark.asyncio
async def test_filters_forwarded_as_query_params(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: cluster/host/task filters forward as query params; omitted keys drop."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        host_route = mock.get("/v1/hosts").respond(200, json=_ENVELOPE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id="sddc.host.list",
            target=_SddcReadTarget(),
            params={"domainId": "domain-mgmt", "status": "ASSIGNED"},
        )

    assert result.status == "ok", result.error
    sent = host_route.calls[0].request.url
    assert sent.params.get("domainId") == "domain-mgmt"
    assert sent.params.get("status") == "ASSIGNED"
    # clusterId was not supplied -> not forwarded.
    assert "clusterId" not in sent.params


@pytest.mark.asyncio
async def test_registered_ops_are_source_kind_typed(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: the persisted descriptor rows carry ``source_kind='typed'``."""
    await register_sddc_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == SDDC_PRODUCT,
                    EndpointDescriptor.impl_id == SDDC_IMPL_ID,
                    EndpointDescriptor.op_id.in_([op.op_id for op in SDDC_TYPED_OPS]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in SDDC_TYPED_OPS}
    assert all(r.source_kind == "typed" for r in rows)
    assert all(r.handler_ref is not None for r in rows)


# ---------------------------------------------------------------------------
# AC #2 -- credential-read gating (sddc.credential.list)
# ---------------------------------------------------------------------------

_CREDENTIALS_PAYLOAD: dict[str, Any] = {
    "elements": [
        {
            "id": "cred-esxi-01",
            "resource": {"resourceName": "esx-01.test.invalid", "resourceType": "ESXI"},
            "accountType": "USER",
            "credentialType": "SSH",
            "username": "root",
            "password": "super-secret-esxi-pw",
        },
        {
            "id": "cred-vc-01",
            "resource": {"resourceName": "vcenter-mgmt.test.invalid", "resourceType": "VCENTER"},
            "accountType": "SYSTEM",
            "credentialType": "SSO",
            "username": "administrator@vsphere.local",
            "password": "super-secret-vc-pw",
        },
    ]
}


@pytest.mark.asyncio
async def test_credential_list_is_gated_awaiting_approval(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #2: dispatch parks at awaiting_approval; the credentials endpoint is never hit."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        creds_route = mock.get("/v1/credentials").respond(200, json=_CREDENTIALS_PAYLOAD)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id=_CREDENTIAL_OP_ID,
            target=_SddcReadTarget(),
            params={},
        )

    # The elevated policy path (approval queue) fires: the op is NOT
    # dispatchable without an operator approval, so the read never runs.
    assert result.status == "awaiting_approval", result
    assert not creds_route.called
    # No credential value could have leaked -- the endpoint wasn't called.
    assert "super-secret-esxi-pw" not in repr(result)


@pytest.mark.asyncio
async def test_credential_list_handler_redacts_secrets(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #2: the handler scrubs every secret value even on the approved read path.

    The approval gate blocks a full dispatch, so the handler's boundary
    redaction is exercised directly (the code that runs once an operator
    approves and the run resumes).
    """
    instance = await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
        mock.get("/v1/credentials").respond(200, json=_CREDENTIALS_PAYLOAD)
        result = await instance.credential_list(_make_operator(), _SddcReadTarget(), {})

    elements = result["elements"]
    assert [e["password"] for e in elements] == [REDACTED, REDACTED]
    # Non-secret identity fields survive so the inventory is still useful.
    assert elements[0]["username"] == "root"
    assert elements[0]["resource"]["resourceType"] == "ESXI"
    # No secret value appears anywhere in the returned structure.
    blob = repr(result)
    assert "super-secret-esxi-pw" not in blob
    assert "super-secret-vc-pw" not in blob


def test_credential_op_registration_shape() -> None:
    """AC #2: the credential op is gated via the existing safety_level/policy mechanism."""
    op = next(o for o in SDDC_TYPED_OPS if o.op_id == _CREDENTIAL_OP_ID)
    assert op.requires_approval is True
    assert op.safety_level == "caution"
    assert "credential-read" in op.tags


def test_credential_op_classifies_as_credential_read() -> None:
    """AC #2: classify_op returns credential_read so audit/broadcast rows redact."""
    assert classify_op(_CREDENTIAL_OP_ID) == "credential_read"


# ---------------------------------------------------------------------------
# AC #3 -- session recovery via the #2067 dispatch-path seam
# ---------------------------------------------------------------------------


def test_connector_advertises_public_invalidate_session() -> None:
    """AC #3: the #2067 duck-typed hook is present (established by #2290)."""
    hook = getattr(SddcManagerConnector, "invalidate_session", None)
    assert callable(hook)


@pytest.mark.asyncio
async def test_typed_dispatch_recovers_from_session_expiry(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #3: a 401 on the first read is recovered via invalidate_session + re-dispatch."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        token_route = mock.post(_TOKEN_PATH)
        token_route.side_effect = [
            httpx.Response(200, json={"accessToken": _ACCESS_TOKEN}),
            httpx.Response(200, json={"accessToken": f"{_ACCESS_TOKEN}-2"}),
        ]
        domains_route = mock.get("/v1/domains")
        domains_route.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json=_ENVELOPE),
        ]
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id="sddc.domain.list",
            target=_SddcReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == _ENVELOPE
    # One 401 + one recovered 200 = two domain calls; two token mints.
    assert domains_route.call_count == 2
    assert token_route.call_count == 2


# ---------------------------------------------------------------------------
# search_operations visibility + registration-shape invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The registered typed ops are retrievable via search_operations."""
    await register_sddc_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": SDDC_CONNECTOR_ID,
            "query": "sddc vcf domains hosts credentials tasks licenses",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    expected = {op.op_id for op in SDDC_TYPED_OPS}
    assert expected <= found, f"missing from search: {expected - found}"


_EXPECTED_OP_IDS = {
    "sddc.domain.list",
    "sddc.domain.status",
    "sddc.cluster.list",
    "sddc.host.list",
    "sddc.vcenter.list",
    "sddc.nsxt_cluster.list",
    "sddc.credential.list",
    "sddc.task.list",
    "sddc.system.info",
    "sddc.vcf_service.list",
    "sddc.manager.list",
    "sddc.license.list",
}


def test_typed_ops_table_is_exactly_the_audited_read_set() -> None:
    assert {op.op_id for op in SDDC_TYPED_OPS} == _EXPECTED_OP_IDS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS - {_CREDENTIAL_OP_ID}))
def test_each_non_credential_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in SDDC_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_when_to_use_and_output_shape(op_id: str) -> None:
    op = next(o for o in SDDC_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    assert op.llm_instructions.get("when_to_use", "").strip() != ""
    assert "output_shape" in op.llm_instructions


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in SDDC_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only Task: no create/write op ships (SDDC writes are out of scope)."""
    for op in SDDC_TYPED_OPS:
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags

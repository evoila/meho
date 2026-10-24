# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``sddc.license.list``: each license key comes back masked.

The rows of ``GET /v1/license-keys`` hold each license key in their ``key``
field. The op returns ``key_masked`` in its place (only the last 5 characters
visible) and carries the tag ``license-key``, so the flight recorder stores no
body of the vendor call. These tests check that no full key reaches:

* the result of a dispatch, an audit row or a stored result (a result handle);
* a flight-recorder span. A control case without the tag shows the key in the
  span, so that check is not empty.

They also check that the rows match the published response schema, where
``key_masked`` is text or null.

Every SDDC Manager answer is a hand-written fake in the shape of the vendor
``PageOfLicenseKey`` schema. The keys are made up (they start with ``FAKE``);
the host uses the reserved ``.invalid`` domain. No live system is called.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_IMPL_ID,
    SDDC_PRODUCT,
    SDDC_TYPED_OPS,
    SDDC_VERSION,
    SddcManagerConnector,
    register_sddc_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder import AGENT_TRACE_HANDLE_EXTRA_KEY, agent_read, capture
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction.flight_recorder import (
    SECRET_FAMILY_OMITTED_MARKER,
    classify_body_exclusion,
)
from meho_backplane.settings import get_settings

_OP_ID = "sddc.license.list"
_LICENSE_KEYS_PATH = "/v1/license-keys"
_TOKEN_PATH = "/v1/tokens"
_ACCESS_TOKEN = "license-access-token"
_SDDC_HOST = "sddc-license.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a5dc")

#: Made-up keys in the vendor shape: five groups of five letters or digits.
_KEY_ESXI = "FAKE1-AAAAA-BBBBB-CCCCC-AB12C"
_KEY_VSAN = "FAKE2-DDDDD-EEEEE-FFFFF-ZY98X"
_SECRET_KEYS = (_KEY_ESXI, _KEY_VSAN)
_MASKED_ESXI = "*****-*****-*****-*****-AB12C"
_MASKED_VSAN = "*****-*****-*****-*****-ZY98X"

#: One row of every field the vendor ``LicenseKey`` schema has.
_ROW_ESXI: dict[str, Any] = {
    "id": "license-0001",
    "key": _KEY_ESXI,
    "productType": "ESXI",
    "description": "<description>",
    "productVersion": "9",
    "isUnlimited": False,
    "licenseKeyUsage": {"total": 64, "used": 16, "remaining": 48, "licenseUnit": "CORE"},
    "licenseKeyValidity": {"licenseKeyStatus": "ACTIVE", "expiryDate": "2027-01-01T00:00:00Z"},
    "features": ["<feature>"],
    "licenseEdition": "<edition>",
    "licenseUnit": "CORE",
    "minimumCore": 16,
}
_ROW_VSAN: dict[str, Any] = {
    "id": "license-0002",
    "key": _KEY_VSAN,
    "productType": "VSAN",
    "description": "<description>",
    "isUnlimited": True,
}


def _answer(*rows: dict[str, Any]) -> dict[str, Any]:
    """A ``PageOfLicenseKey`` answer holding *rows*."""
    return {
        "elements": list(rows),
        "pageMetadata": {
            "pageNumber": 0,
            "pageSize": len(rows),
            "totalElements": len(rows),
            "totalPages": 1,
        },
    }


def _assert_no_full_key(value: Any) -> None:
    """No made-up key appears anywhere in *value* (rendered as text)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for key in _SECRET_KEYS:
        assert key not in text, f"full license key ending {key[-5:]!r} found"
        assert key.lower() not in text.lower(), f"full license key ending {key[-5:]!r} found"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher and handler caches, the registry and the trace config cache."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
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
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed embedding, so registration does not load the model."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on the per-test database."""
    async with get_sessionmaker()() as s:
        yield s


class _SddcTarget:
    """A target the connector and the resolver both accept."""

    def __init__(self) -> None:
        self.product = SDDC_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SDDC_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "sddc-license"
        self.host = _SDDC_HOST
        self.port = 443
        self.secret_ref = "targets/op-license/sddc-license"
        self.auth_model = "shared_service_account"
        self.sso_realm = "vsphere.local"


def _operator() -> Operator:
    return Operator(
        sub="op-license-sddc",
        name="SDDC License Operator",
        email=None,
        raw_jwt="op.license.sddc.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _credentials(_target: object, _operator: Operator) -> dict[str, str]:
    """Fixed credentials, so no Vault read runs."""
    return {"username": "sddc-license-svc", "password": "sddc-license-pw"}


async def _register() -> SddcManagerConnector:
    """Run the SDDC typed registrar; return the connector with fake credentials."""
    await register_sddc_typed_operations()
    connector = get_or_create_connector_instance(SddcManagerConnector)
    connector._credentials_loader = _credentials  # type: ignore[attr-defined]
    return connector


def _mock_sddc(mock: respx.MockRouter, answer: Any) -> respx.Route:
    mock.post(_TOKEN_PATH).respond(200, json={"accessToken": _ACCESS_TOKEN})
    return mock.get(_LICENSE_KEYS_PATH).respond(200, json=answer)


async def _dispatch_list(answer: Any) -> Any:
    """Dispatch ``sddc.license.list`` against a fake SDDC Manager serving *answer*."""
    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        route = _mock_sddc(mock, answer)
        result = await dispatch(
            operator=_operator(),
            connector_id=SDDC_CONNECTOR_ID,
            op_id=_OP_ID,
            target=_SddcTarget(),
            params={},
        )
    assert route.call_count == 1
    return result


def _license_op() -> Any:
    return next(op for op in SDDC_TYPED_OPS if op.op_id == _OP_ID)


# ---------------------------------------------------------------------------
# The rows: key_masked in place of key, every other field kept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_row_shows_the_key_masked_and_keeps_every_other_field(
    _stub_embedding: None, session: AsyncSession
) -> None:
    await _register()
    result = await _dispatch_list(_answer(_ROW_ESXI, _ROW_VSAN))

    assert result.status == "ok", result.error
    esxi, vsan = result.result["elements"]
    expected_esxi = {name: value for name, value in _ROW_ESXI.items() if name != "key"}
    assert esxi == {**expected_esxi, "key_masked": _MASKED_ESXI}
    assert vsan["key_masked"] == _MASKED_VSAN
    assert "key" not in esxi and "key" not in vsan
    assert result.result["pageMetadata"] == _answer(_ROW_ESXI, _ROW_VSAN)["pageMetadata"]
    _assert_no_full_key(result.result)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # A key of 10 characters or fewer is hidden in full.
        pytest.param(
            {"elements": [{"id": "x", "key": "SHORT-KEY"}]},
            {"elements": [{"id": "x", "key_masked": "*********"}]},
            id="short-key",
        ),
        # A key that is not a non-empty string gives null.
        pytest.param(
            {"elements": [{"id": "x", "key": None}]},
            {"elements": [{"id": "x", "key_masked": None}]},
            id="null-key",
        ),
        pytest.param(
            {"elements": [{"id": "x", "key": ""}]},
            {"elements": [{"id": "x", "key_masked": None}]},
            id="empty-key",
        ),
        # A row without a key, and an empty answer, stay as they are.
        pytest.param({"elements": [{"id": "x"}]}, {"elements": [{"id": "x"}]}, id="no-key"),
        pytest.param({}, {}, id="empty-answer"),
        # An answer of another shape is masked too.
        pytest.param([{"key": _KEY_ESXI}], [{"key_masked": _MASKED_ESXI}], id="bare-list"),
        pytest.param(
            {"page": {"rows": [{"key": _KEY_VSAN}]}},
            {"page": {"rows": [{"key_masked": _MASKED_VSAN}]}},
            id="other-envelope",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_handler_masks_every_key_field(
    monkeypatch: pytest.MonkeyPatch, answer: Any, expected: Any
) -> None:
    connector = SddcManagerConnector()

    async def _fake_get_json(target: Any, path: str, **_: Any) -> Any:
        assert path == _LICENSE_KEYS_PATH
        return answer

    monkeypatch.setattr(connector, "_get_json", _fake_get_json)
    assert await connector.license_list(_operator(), _SddcTarget(), {}) == expected


@pytest.mark.asyncio
async def test_masked_rows_match_the_published_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """``key_masked`` is text, or null when SDDC Manager sends an empty key or no text."""
    connector = SddcManagerConnector()
    answer = _answer(
        _ROW_ESXI,
        {"id": "license-0003", "key": ""},
        {"id": "license-0004", "key": None},
    )

    async def _fake_get_json(target: Any, path: str, **_: Any) -> Any:
        return answer

    monkeypatch.setattr(connector, "_get_json", _fake_get_json)
    result = await connector.license_list(_operator(), _SddcTarget(), {})

    assert [row["key_masked"] for row in result["elements"]] == [_MASKED_ESXI, None, None]
    schema = _license_op().response_schema
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(result)) == []


# ---------------------------------------------------------------------------
# No full key in the audit rows or in a stored result
# ---------------------------------------------------------------------------


async def _audit_rows_text() -> str:
    """Every audit row of the test database, as one text."""
    async with get_sessionmaker()() as s:
        rows = [
            {attr.key: getattr(row, attr.key) for attr in sa_inspect(row).mapper.column_attrs}
            for row in (await s.execute(select(AuditLog))).scalars()
        ]
    assert rows, "the dispatch should have written an audit row"
    return json.dumps(rows, default=str)


@pytest.mark.asyncio
async def test_the_audit_rows_hold_no_full_key(
    _stub_embedding: None, session: AsyncSession
) -> None:
    await _register()
    result = await _dispatch_list(_answer(_ROW_ESXI, _ROW_VSAN))

    assert result.status == "ok", result.error
    _assert_no_full_key(await _audit_rows_text())


class _FakeValkey:
    """In-memory async Valkey stand-in for the result handle store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def _handle_store(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeValkey, ResultHandleStore]:
    """Point result handles and the agent's copy of a trace at one in-memory store."""
    valkey = _FakeValkey()
    store = ResultHandleStore(valkey)  # type: ignore[arg-type]
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(agent_read, "get_result_handle_store", lambda: store)
    return valkey, store


@pytest.mark.asyncio
async def test_a_stored_result_holds_no_full_key(
    _stub_embedding: None,
    session: AsyncSession,
    _handle_store: tuple[_FakeValkey, ResultHandleStore],
) -> None:
    """A long list comes back as a result handle; the stored rows are masked too."""
    valkey, store = _handle_store
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))
    await _register()
    rows = [{**_ROW_ESXI, "id": f"license-{i:04d}"} for i in range(60)]

    result = await _dispatch_list(_answer(*rows))

    assert result.status == "ok", result.error
    assert result.handle is not None, "60 rows should come back as a result handle"
    window = await read_result_window(_operator(), result.handle.handle_id, offset=0, limit=60)
    assert len(window["rows"]) == 60
    assert {row["key_masked"] for row in window["rows"]} == {_MASKED_ESXI}
    assert all("key" not in row for row in window["rows"])
    stored = b"".join(valkey.store.values()).decode()
    assert _MASKED_ESXI in stored, "the stored rows should hold the masked key"
    _assert_no_full_key(stored)
    _assert_no_full_key(result.result)
    _assert_no_full_key(window)


# ---------------------------------------------------------------------------
# The tag, and the flight recorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_op_carries_the_license_key_tag(
    _stub_embedding: None, session: AsyncSession
) -> None:
    op = _license_op()
    assert "license-key" in op.tags
    assert classify_body_exclusion(_OP_ID, tags=op.tags, method="GET").excluded is True
    # Control: without the tag, nothing else keeps the body out of the trace.
    other_tags = tuple(tag for tag in op.tags if tag != "license-key")
    assert classify_body_exclusion(_OP_ID, tags=other_tags, method="GET").excluded is False

    await register_sddc_typed_operations()
    row = (
        await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.impl_id == SDDC_IMPL_ID,
                EndpointDescriptor.op_id == _OP_ID,
            )
        )
    ).scalar_one()
    assert "license-key" in row.tags
    assert row.safety_level == "safe"
    assert row.requires_approval is False


async def _turn_on_the_flight_recorder(session: AsyncSession) -> None:
    session.add(
        Tenant(
            id=_TENANT_ID, slug="sddc-license", name="sddc-license", flight_recorder_enabled=True
        )
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        return list(
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == trace.id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )


def _license_call_spans(spans: list[DispatchTraceSpan]) -> list[DispatchTraceSpan]:
    return [
        span
        for span in spans
        if span.span_kind == "vendor_call" and span.name.endswith(_LICENSE_KEYS_PATH)
    ]


@pytest.mark.asyncio
async def test_the_trace_of_a_dispatch_holds_no_key(
    _stub_embedding: None,
    session: AsyncSession,
    _handle_store: tuple[_FakeValkey, ResultHandleStore],
) -> None:
    """The dispatcher records the vendor call, but not its body.

    The dispatcher also copies the trace into the result handle store, so an
    agent can read it. That copy holds no key either.
    """
    valkey, _ = _handle_store
    await _register()
    await _turn_on_the_flight_recorder(session)

    result = await _dispatch_list(_answer(_ROW_ESXI, _ROW_VSAN))

    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _spans(result.audit_id)
    (span,) = _license_call_spans(spans)
    assert span.attributes["body_recorded"] is False
    assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    _assert_no_full_key([{"name": s.name, "attributes": s.attributes} for s in spans])
    assert AGENT_TRACE_HANDLE_EXTRA_KEY in result.extras
    stored = b"".join(valkey.store.values()).decode()
    assert _LICENSE_KEYS_PATH in stored, "the stored trace should hold the license call span"
    _assert_no_full_key(stored)


async def _trace_of_a_list_call(session: AsyncSession, tags: tuple[str, ...]) -> str:
    """Run the real HTTP path of the read under a trace with *tags*; return the spans as text."""
    connector = await _register()
    await _turn_on_the_flight_recorder(session)
    audit_id = uuid.uuid4()
    handle = await capture.begin_dispatch_capture(
        audit_id=audit_id,
        operator=_operator(),
        target=_SddcTarget(),
        descriptor=SimpleNamespace(op_id=_OP_ID, tags=list(tags), source_kind="typed"),
        connector_id=SDDC_CONNECTOR_ID,
    )
    try:
        async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
            _mock_sddc(mock, _answer(_ROW_ESXI, _ROW_VSAN))
            await connector.license_list(_operator(), _SddcTarget(), {})
    finally:
        await capture.end_dispatch_capture(handle)
    spans = await _spans(audit_id)
    assert len(_license_call_spans(spans)) == 1
    return json.dumps([{"name": s.name, "attributes": s.attributes} for s in spans], default=str)


@pytest.mark.asyncio
async def test_with_the_tag_the_trace_holds_no_key(
    _stub_embedding: None, session: AsyncSession
) -> None:
    _assert_no_full_key(await _trace_of_a_list_call(session, _license_op().tags))


@pytest.mark.asyncio
async def test_without_the_tag_the_trace_would_hold_the_key(
    _stub_embedding: None, session: AsyncSession
) -> None:
    """Control: the tag is what keeps the key out, so the test above is not empty."""
    other_tags = tuple(tag for tag in _license_op().tags if tag != "license-key")
    assert _KEY_ESXI in await _trace_of_a_list_call(session, other_tags)

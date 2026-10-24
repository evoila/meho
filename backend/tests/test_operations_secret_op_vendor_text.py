# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vendor's error text of an operation that sends a secret is not kept (#4854).

An operation sends a secret when ``classify_op`` pins it as
``credential_write`` or when one of its tags has a secret token (the same
list the flight recorder uses). For such an operation the dispatcher replaces
every vendor text in the error result with one fixed text, in one place, so
the caller's answer and the audit row hold the same thing. MEHO's own fields
stay: ``error_code``, ``http_status``, ``cause`` and the fault type.

Each vendor answer here holds a made-up secret. The tests check that it
reaches neither the result nor the audit row, that an operation that sends
no secret keeps the text as today, and that the login retry after HTTP 401
still runs once.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.soap import SoapFault, attach_soap_fault
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import (
    result_connector_auth_failed,
    result_connector_error,
    result_connector_http_403,
    result_connector_http_422,
)
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._vendor_text import (
    VENDOR_TEXT_WITHHELD,
    sends_secret,
    withhold_vendor_text,
)
from meho_backplane.settings import get_settings

# Made up, and not secret-shaped, so the Tier-1 redaction does not catch it:
# only the rule under test can keep it out.
_SECRET = "quokka-" + "7781-zebra-pass"
_VENDOR_TEXT = f"the value {_SECRET} does not meet the rules"

_SECRET_TAG_OP = "POST:/api/v1/accounts/{id}/rotate"
_PINNED_OP = "vault.kv.put"
_PLAIN_OP = "POST:/api/v1/accounts"
_SECRET_TAGS = ["accounts", "secret-write"]

_PRODUCT = "secretdemo"
_VERSION = "1"
_TENANT = UUID("00000000-0000-0000-0000-00000000a854")


def _http_error(status: int, body: Any) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://appliance.example.test/api/v1/accounts")
    if isinstance(body, str):
        response = httpx.Response(status, text=body, request=request)
    else:
        response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _json_body() -> dict[str, Any]:
    return {
        "_typeName": "InvalidArgument",
        "faultstring": _VENDOR_TEXT,
        "message": _VENDOR_TEXT,
        "errors": [{"field": "value", "message": _VENDOR_TEXT}],
    }


def _soap_body() -> str:
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<soapenv:Body><soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{_VENDOR_TEXT}</faultstring>"
        '<detail><InvalidArgumentFault xmlns="urn:vim25" xsi:type="InvalidArgument"/></detail>'
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )


def _attached_soap_fault() -> RuntimeError:
    """A typed connector's error with the parsed SOAP fault attached (the ESXi path)."""
    error = RuntimeError(f"vim call failed: {_VENDOR_TEXT}")
    attach_soap_fault(
        error,
        SoapFault(
            faultcode="ServerFaultCode", faultstring=_VENDOR_TEXT, fault_type="InvalidArgument"
        ),
    )
    return error


def _establish_auth_error() -> ConnectorAuthError:
    error = ConnectorAuthError(
        f"login rejected: {_VENDOR_TEXT}",
        status_code=401,
        cause="session_establish_401",
        target_name="appliance-a",
    )
    error.__cause__ = _http_error(401, _json_body())
    return error


class _Target:
    name = "appliance-a"
    host = "appliance.example.test"
    secret_ref = None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


# ---------------------------------------------------------------------------
# The check: does this operation send a secret?
# ---------------------------------------------------------------------------


def test_sends_secret_for_a_pinned_op_and_for_a_secret_tag() -> None:
    assert sends_secret(_PINNED_OP, [])
    assert sends_secret("vmware.composite.license.assign", ["composite", "license-key"])
    assert sends_secret(_SECRET_TAG_OP, _SECRET_TAGS)
    assert not sends_secret(_PLAIN_OP, ["accounts", "write"])
    assert not sends_secret(_PLAIN_OP, None)


# ---------------------------------------------------------------------------
# The rule on each built error result
# ---------------------------------------------------------------------------


_Build = Callable[[], Any]

_RESULTS: list[tuple[str, _Build, dict[str, Any]]] = [
    (
        "401-auth-failed",
        lambda: result_connector_auth_failed(
            "op", _http_error(401, _json_body()), _Target(), 1.0, relogin="reestablished"
        ),
        {
            "error_code": "connector_auth_failed",
            "http_status": 401,
            "cause": "session_dispatch_401_after_relogin",
        },
    ),
    (
        "establish-auth-failed",
        lambda: result_connector_auth_failed("op", _establish_auth_error(), _Target(), 1.0),
        {"error_code": "connector_auth_failed", "cause": "session_establish_401"},
    ),
    (
        "403",
        lambda: result_connector_http_403("op", _http_error(403, _json_body()), 1.0),
        {"error_code": "connector_http_403", "http_status": 403},
    ),
    (
        "422",
        lambda: result_connector_http_422("op", _http_error(422, _json_body()), 1.0),
        {"error_code": "connector_http_422", "http_status": 422},
    ),
    (
        "500",
        lambda: result_connector_error("op", _http_error(500, _json_body()), 1.0),
        {"error_code": "connector_error", "http_status": 500, "exception_class": "HTTPStatusError"},
    ),
    (
        "soap-fault-body",
        lambda: result_connector_error("op", _http_error(500, _soap_body()), 1.0),
        {"error_code": "connector_error", "http_status": 500},
    ),
    (
        "soap-fault-attached",
        lambda: result_connector_error("op", _attached_soap_fault(), 1.0),
        {"error_code": "connector_error", "exception_class": "RuntimeError"},
    ),
    (
        "upstream-error",
        lambda: result_connector_error(
            "op",
            ConnectorUpstreamError("rejected", http_status=400, upstream_message=_VENDOR_TEXT),
            1.0,
        ),
        {"error_code": "connector_error", "http_status": 400},
    ),
]
_RESULT_IDS = [name for name, _, _ in _RESULTS]


@pytest.mark.parametrize(("_name", "build", "kept"), _RESULTS, ids=_RESULT_IDS)
def test_vendor_text_is_withheld_and_meho_fields_stay(
    _name: str, build: _Build, kept: dict[str, Any]
) -> None:
    built = build()
    # Control: the builder copies the secret into the result.
    assert _SECRET in built.model_dump_json()

    out = withhold_vendor_text(built, op_id=_SECRET_TAG_OP, tags=_SECRET_TAGS)

    assert _SECRET not in out.model_dump_json()
    for key, value in kept.items():
        assert out.extras[key] == value, key
    # Same keys, so the shape of the result does not change.
    assert set(out.extras) == set(built.extras)
    assert VENDOR_TEXT_WITHHELD in out.model_dump_json()
    fault = built.extras.get("upstream_fault")
    if fault is not None:
        assert out.extras["upstream_fault"] == {
            "type": fault["type"],
            "faultstring": VENDOR_TEXT_WITHHELD,
        }


def test_the_summary_keeps_meho_text_and_drops_what_upstream_said() -> None:
    built = result_connector_http_403("op", _http_error(403, _json_body()), 1.0)
    out = withhold_vendor_text(built, op_id=_PINNED_OP, tags=None)
    assert built.error is not None
    assert out.error is not None
    meho_part = built.error.split(" Upstream said: ")[0]
    assert out.error == f"{meho_part} Upstream said: {VENDOR_TEXT_WITHHELD}"


def test_the_422_validation_errors_are_withheld_but_stay_a_list() -> None:
    built = result_connector_http_422("op", _http_error(422, _json_body()), 1.0)
    out = withhold_vendor_text(built, op_id=_PINNED_OP, tags=None)
    assert out.extras["validation_errors"] == [VENDOR_TEXT_WITHHELD]


def test_an_op_that_sends_no_secret_keeps_the_result_as_it_is() -> None:
    built = result_connector_http_422("op", _http_error(422, _json_body()), 1.0)
    assert withhold_vendor_text(built, op_id=_PLAIN_OP, tags=["accounts"]) is built


# ---------------------------------------------------------------------------
# Through the dispatcher: the result and the audit row, one rule for both
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
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _FailingConnector(HttpConnector):
    """Every call fails with the error :attr:`fail` builds. Has the 401 re-login hook."""

    product = _PRODUCT
    version = _VERSION
    impl_id = "secretdemo-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    #: Set per test with ``monkeypatch`` (as a ``staticmethod``).
    fail: ClassVar[Callable[[], BaseException]]

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.invalidations = 0

    def _raise(self) -> dict[str, Any]:
        self.attempts += 1
        raise type(self).fail()

    async def _request_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return self._raise()

    async def _post_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return self._raise()

    async def invalidate_session(self, target: Any) -> None:
        self.invalidations += 1

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


class _Fingerprint:
    version = _VERSION


class _DispatchTarget:
    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = _Fingerprint()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "appliance-a"
        self.host = "appliance.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.secret_ref: str | None = None


def _operator() -> Operator:
    return Operator(
        sub="op-secret-text",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed(session: AsyncSession, op_id: str, tags: list[str]) -> None:
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_FailingConnector.impl_id,
            op_id=op_id,
            source_kind="ingested",
            method="POST",
            path="/api/v1/accounts",
            handler_ref=None,
            summary="Made-up op.",
            description="Made-up op with no masking code of its own.",
            tags=tags,
            parameter_schema={"type": "object", "properties": {}},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=[0.1] * 384,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _dispatch(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    op_id: str,
    tags: list[str],
    fail: Callable[[], BaseException],
) -> tuple[Any, dict[str, Any], _FailingConnector]:
    monkeypatch.setattr(_FailingConnector, "fail", staticmethod(fail), raising=False)
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_FailingConnector.impl_id, cls=_FailingConnector
    )
    await _seed(session, op_id, tags)
    result = await dispatch(
        operator=_operator(),
        connector_id=f"{_FailingConnector.impl_id}-{_VERSION}",
        op_id=op_id,
        target=_DispatchTarget(),
        params={},
    )
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1
    connector = get_or_create_connector_instance(_FailingConnector)
    assert isinstance(connector, _FailingConnector)
    return result, rows[0].payload, connector


_CASES: list[tuple[str, Callable[[], BaseException], dict[str, Any]]] = [
    (
        "401-after-retry",
        lambda: _http_error(401, _json_body()),
        {"error_code": "connector_auth_failed", "http_status": 401},
    ),
    (
        "403",
        lambda: _http_error(403, _json_body()),
        {"error_code": "connector_http_403", "http_status": 403},
    ),
    (
        "422",
        lambda: _http_error(422, _json_body()),
        {"error_code": "connector_http_422", "http_status": 422},
    ),
    (
        "500",
        lambda: _http_error(500, _json_body()),
        {"error_code": "connector_error", "http_status": 500},
    ),
    (
        "soap-fault",
        lambda: _http_error(500, _soap_body()),
        {"error_code": "connector_error", "http_status": 500},
    ),
]
_CASE_IDS = [name for name, _, _ in _CASES]


@pytest.mark.parametrize(("op_id", "tags"), [(_SECRET_TAG_OP, _SECRET_TAGS), (_PINNED_OP, [])])
@pytest.mark.parametrize(("_name", "fail", "kept"), _CASES, ids=_CASE_IDS)
async def test_dispatch_keeps_the_secret_out_of_the_result_and_the_audit_row(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    tags: list[str],
    _name: str,
    fail: Callable[[], BaseException],
    kept: dict[str, Any],
) -> None:
    result, payload, connector = await _dispatch(
        session, monkeypatch, op_id=op_id, tags=tags, fail=fail
    )

    assert result.status == "error"
    assert _SECRET not in result.model_dump_json()
    assert _SECRET not in _text(payload)
    for key, value in kept.items():
        assert result.extras[key] == value, key
        assert payload["error"][key] == value, key
    assert result.extras["upstream_message"] == VENDOR_TEXT_WITHHELD
    assert payload["error"]["upstream_message"] == VENDOR_TEXT_WITHHELD
    assert result.extras["upstream_fault"]["type"] == "InvalidArgument"
    assert payload["error"]["upstream_fault"]["type"] == "InvalidArgument"
    if kept["http_status"] == 401:
        # The login retry still runs once: the first call, one re-login, one retry.
        assert connector.attempts == 2
        assert connector.invalidations == 1
        assert result.extras["cause"] == "session_dispatch_401_after_relogin"
    else:
        assert connector.attempts == 1
        assert connector.invalidations == 0


@pytest.mark.parametrize(("_name", "fail", "kept"), _CASES, ids=_CASE_IDS)
async def test_dispatch_of_an_op_that_sends_no_secret_keeps_the_vendor_text(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _name: str,
    fail: Callable[[], BaseException],
    kept: dict[str, Any],
) -> None:
    result, payload, _ = await _dispatch(
        session, monkeypatch, op_id=_PLAIN_OP, tags=["accounts", "write"], fail=fail
    )

    assert result.status == "error"
    assert _SECRET in result.extras["upstream_message"]
    assert _SECRET in payload["error"]["upstream_message"]
    assert result.extras["upstream_fault"]["faultstring"] == _VENDOR_TEXT
    assert VENDOR_TEXT_WITHHELD not in result.model_dump_json()
    for key, value in kept.items():
        assert result.extras[key] == value, key

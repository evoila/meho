# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Planted secrets stay out of results, errors, audit rows, traces and logs (#5031).

One shared test for every connector. Each connector's operations run through
the same dispatcher, so the test registers one typed operation under each
registered connector and runs it with a handler that returns or raises a
vendor answer with planted secrets:

* a normal answer;
* error answers: HTTP 500, 422, 403 and 401 with a JSON body, a session login
  the vendor refused, a plain raised error whose text holds the vendor's
  JSON or a Python dict, an error result the handler returns with the
  vendor's answer in ``result``, and a multi-step operation's error envelope
  (an ``issues[]`` entry with ``severity == "error"``) with the vendor's
  answer inside.

The planted values are a token, a password field and a vendor's encrypted
password field. For each run the test checks that no planted value is in:

* the result the caller gets;
* the committed audit row of the run;
* any log line written during the run.

A trace test hands the same answers (and a request with an
``Authorization`` header and a password in the body) to the flight-recorder
redaction that every vendor call goes through, and checks the recorded span.

What the normal answer holds: secrets with a label in the text
(``Bearer ...``, ``password=...``). The normal result path removes a
secret-named field such as ``{"password": ...}`` only for a credential read,
and the audit row's ``raw_payload`` keeps the raw answer on purpose (see
``docs/codebase/redaction.md``), so neither is checked for the normal answer.
The row of a multi-step error envelope keeps the raw answer too, so its
``raw_payload`` is not checked either; its result, its ``payload`` and the
stored copy behind a result handle are cleaned. The trace removes
secret-named fields for every answer.

Every planted value starts with :data:`PLANTED_MARKER`. The capture check
(``test_capture_secret_scan.py``) fails when that marker is in a saved test
capture, so a value from here cannot end up in one unseen.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    clear_registry,
    list_connector_impls,
    product_impl_id_round_trips,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    DispatchChild,
    PassThroughReducer,
    dispatch,
    register_composite_operation,
    register_typed_operation,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.redaction.flight_recorder import redact_span
from meho_backplane.settings import get_settings

#: The start of every planted value. The capture check looks for it.
PLANTED_MARKER = "planted-secret-"

PLANTED_TOKEN = f"{PLANTED_MARKER}token-5031-made-up"
PLANTED_PASSWORD = f"{PLANTED_MARKER}password-5031-made-up"
PLANTED_ENCRYPTED = f"{PLANTED_MARKER}encrypted-5031-made-up"
PLANTED_VALUES = (PLANTED_TOKEN, PLANTED_PASSWORD, PLANTED_ENCRYPTED)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005031")
_VENDOR_URL = "https://vendor.example.invalid/api/v1/items"

#: A vendor's JSON error body: a session token, a password field and an
#: encrypted password field, with no top-level ``message``.
_VENDOR_ERROR_BODY: dict[str, Any] = {
    "error": {
        "code": "LOGIN_FAILED",
        "sessionToken": PLANTED_TOKEN,
        "password": PLANTED_PASSWORD,
        "encryptedPassword": PLANTED_ENCRYPTED,
    }
}

#: The same secrets as a vendor's normal answer holds them as structured data.
_VENDOR_STRUCTURED_ANSWER: dict[str, Any] = {
    "items": [{"name": "item-1", "token": PLANTED_TOKEN}],
    "account": {"user": "svc-made-up", "password": PLANTED_PASSWORD},
    "encryptedPassword": PLANTED_ENCRYPTED,
}

#: A normal answer with secrets the normal result path removes: text with a label.
_VENDOR_NORMAL_ANSWER: dict[str, Any] = {
    "items": [{"name": "item-1", "state": "ready"}],
    "debug": f"Authorization: Bearer {PLANTED_TOKEN}",
    "note": f"connected with password={PLANTED_PASSWORD} and secret: {PLANTED_ENCRYPTED}",
}


def _connectors() -> list[tuple[str, str, str]]:
    """Every registered connector, as ``(product, version, impl_id)``."""
    _eager_import_connectors()
    impls = [
        (product, version, impl_id)
        for product, version, impl_id in list_connector_impls()
        if version
        and impl_id
        and product_impl_id_round_trips(product=product, version=version, impl_id=impl_id)
    ]
    return impls


_CONNECTORS = _connectors()


# ---------------------------------------------------------------------------
# The planted answers
# ---------------------------------------------------------------------------


def _http_error(
    status: int, *, json_body: Any, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _VENDOR_URL)
    response = httpx.Response(status, json=json_body, headers=headers, request=request)
    return httpx.HTTPStatusError(f"{status} from vendor", request=request, response=response)


def _raise_http_500() -> Any:
    raise _http_error(500, json_body=_VENDOR_ERROR_BODY)


def _raise_http_422() -> Any:
    raise _http_error(
        422,
        json_body={
            "message": f"Validation failed for token={PLANTED_TOKEN}",
            "errors": [
                {"field": "account", "rejected": {"password": PLANTED_PASSWORD}},
                {
                    "field": "key",
                    "message": f'bad value {{"encryptedPassword": "{PLANTED_ENCRYPTED}"}}',
                },
            ],
        },
    )


def _raise_http_403() -> Any:
    raise _http_error(
        403,
        json_body={"message": f"denied: password={PLANTED_PASSWORD}", "detail": _VENDOR_ERROR_BODY},
        headers={"x-oauth-scopes": f"Bearer {PLANTED_TOKEN}"},
    )


def _raise_http_401() -> Any:
    raise _http_error(401, json_body=_VENDOR_ERROR_BODY)


def _raise_login_refused() -> Any:
    raise ConnectorAuthError(
        f"login refused: {json.dumps(_VENDOR_ERROR_BODY)}",
        status_code=401,
        cause="invalid_credentials",
        target_name="made-up-target",
    )


def _raise_json_text() -> Any:
    raise RuntimeError(f"vendor said {json.dumps(_VENDOR_ERROR_BODY)}")


def _raise_python_dict_text() -> Any:
    raise RuntimeError(f"vendor said {_VENDOR_ERROR_BODY!r}")


def _return_normal_answer() -> Any:
    return _VENDOR_NORMAL_ANSWER


def _return_error_result() -> Any:
    """An error result a handler returns (#4274), with the vendor's answer in ``result``."""
    return OperationResult(
        status="error",
        op_id="set-by-the-dispatcher",
        result=_VENDOR_ERROR_BODY,
        error=f"vendor refused: {_VENDOR_ERROR_BODY!r}",
        duration_ms=0,
        extras={"error_code": "vendor_refused", "detail": _VENDOR_ERROR_BODY},
    )


#: A multi-step operation's error envelope (#3809) with the vendor's answer inside.
_COMPOSITE_ERROR_ENVELOPE: dict[str, Any] = {
    "status": "create_error",
    "issues": [
        {
            "severity": "error",
            "message": f"vendor said {json.dumps(_VENDOR_ERROR_BODY)}",
            "rejected": _VENDOR_ERROR_BODY,
        }
    ],
    "account": _VENDOR_STRUCTURED_ANSWER["account"],
}


_ANSWERS: dict[str, Callable[[], Any]] = {
    "normal": _return_normal_answer,
    "http_500": _raise_http_500,
    "http_422": _raise_http_422,
    "http_403": _raise_http_403,
    "http_401": _raise_http_401,
    "login_refused": _raise_login_refused,
    "error_json_text": _raise_json_text,
    "error_python_dict_text": _raise_python_dict_text,
    "error_result_returned": _return_error_result,
}

#: The answer of the multi-step operation; it is registered on its own.
_COMPOSITE_ANSWER = "error_composite_envelope"

_current_answer: list[Callable[[], Any]] = [_return_normal_answer]


async def _planted_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Module-level typed handler: returns or raises the current planted answer."""
    return _current_answer[0]()


async def _planted_composite_handler(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> dict[str, Any]:
    """Module-level multi-step handler: returns the error envelope with planted secrets."""
    return _COMPOSITE_ERROR_ENVELOPE


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    set_default_reducer(PassThroughReducer())
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _StandInConnector(Connector):
    """Stands in for the connector class; the planted handler does not use it."""

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


def _operator() -> Operator:
    return Operator(
        sub="op-planted",
        name="Planted Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _leaks(text: str) -> list[str]:
    return [value for value in PLANTED_VALUES if value in text]


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars().all())


def _row_text(row: AuditLog, *, with_raw_payload: bool) -> str:
    fields: dict[str, Any] = {
        "payload": row.payload,
        "redaction_manifest": row.redaction_manifest,
        "path": row.path,
    }
    if with_raw_payload:
        fields["raw_payload"] = row.raw_payload
    return json.dumps(fields, default=str)


# ---------------------------------------------------------------------------
# The shared test: result, audit row and log, for every connector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "version", "impl_id"),
    _CONNECTORS,
    ids=[f"{impl_id}-{version}" for _, version, impl_id in _CONNECTORS],
)
async def test_planted_secrets_stay_out_of_results_audit_rows_and_logs(
    product: str,
    version: str,
    impl_id: str,
    embedding: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No planted value reaches the caller, the audit row or a log line."""
    register_connector_v2(product=product, version=version, impl_id=impl_id, cls=_StandInConnector)
    op_ids: dict[str, str] = {}
    for answer in _ANSWERS:
        op_id = f"{product}.planted_secret_{answer}.read"
        op_ids[answer] = op_id
        await register_typed_operation(
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            handler=_planted_handler,
            summary="Made-up read for the planted-secret test.",
            description="Returns or raises a vendor answer with planted secrets.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=embedding,
            safety_level="safe",
        )
    composite_op_id = f"{product}.composite.planted_secret_{_COMPOSITE_ANSWER}.read"
    op_ids[_COMPOSITE_ANSWER] = composite_op_id
    await register_composite_operation(
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=composite_op_id,
        handler=_planted_composite_handler,
        summary="Made-up multi-step read for the planted-secret test.",
        description="Returns an error envelope with planted secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
        requires_approval=False,
    )

    # MEHO's own loggers at every level; other libraries at INFO and up (the
    # SQLite driver's DEBUG lines echo every statement, raw_payload included).
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="meho_backplane")
    leaks: dict[str, list[str]] = {}
    answers: dict[str, Callable[[], Any]] = {**_ANSWERS, _COMPOSITE_ANSWER: _return_normal_answer}
    for answer, make_answer in answers.items():
        _current_answer[0] = make_answer
        caplog.clear()
        with structlog.testing.capture_logs() as structured_logs:
            result = await dispatch(
                operator=_operator(),
                connector_id=f"{impl_id}-{version}",
                op_id=op_ids[answer],
                target=None,
                params={},
            )
        if answer == "normal":
            assert result.status == "ok", result.error
        else:
            assert result.status == "error", answer

        rows = await _audit_rows(op_ids[answer])
        assert len(rows) == 1, answer
        # An error row keeps no raw answer. A normal row keeps it on purpose,
        # and so does the row of a multi-step error envelope: the handler
        # returned it like a normal answer (#4223).
        keeps_raw = answer in ("normal", _COMPOSITE_ANSWER)
        row_text = _row_text(rows[0], with_raw_payload=not keeps_raw)

        for place, text in (
            ("result", result.model_dump_json()),
            ("audit row", row_text),
            ("log", json.dumps(structured_logs, default=str) + caplog.text),
        ):
            if found := _leaks(text):
                leaks[f"{answer} -> {place}"] = found

    assert leaks == {}


# ---------------------------------------------------------------------------
# The trace: the redaction every recorded vendor call goes through
# ---------------------------------------------------------------------------


_TRACE_ANSWERS: dict[str, tuple[int, Any]] = {
    "normal_structured": (200, _VENDOR_STRUCTURED_ANSWER),
    "normal_labelled_text": (200, _VENDOR_NORMAL_ANSWER),
    "error_json": (500, _VENDOR_ERROR_BODY),
    "error_json_text": (400, json.dumps(_VENDOR_ERROR_BODY)),
}


@pytest.mark.parametrize(
    ("product", "version", "impl_id"),
    _CONNECTORS,
    ids=[f"{impl_id}-{version}" for _, version, impl_id in _CONNECTORS],
)
@pytest.mark.parametrize("answer", list(_TRACE_ANSWERS))
def test_planted_secrets_stay_out_of_the_trace(
    product: str, version: str, impl_id: str, answer: str
) -> None:
    """The recorded span of a vendor call holds no planted value."""
    status, body = _TRACE_ANSWERS[answer]
    span = redact_span(
        op_id=f"{product}.items.list",
        connector_id=f"{impl_id}-{version}",
        method="POST",
        request_headers={
            "Authorization": f"Bearer {PLANTED_TOKEN}",
            "x-api-key": PLANTED_TOKEN,
            "content-type": "application/json",
            "user-agent": f"made-up-client token={PLANTED_TOKEN}",
        },
        response_headers={"content-type": "application/json", "x-request-id": "req-made-up"},
        request_body={
            "user": "svc-made-up",
            "password": PLANTED_PASSWORD,
            "encryptedPassword": PLANTED_ENCRYPTED,
        },
        response_body=body,
        request_content_type="application/json",
        response_content_type="application/json" if status < 400 else None,
    )
    assert _leaks(json.dumps(span.model_dump(mode="json"), default=str)) == []

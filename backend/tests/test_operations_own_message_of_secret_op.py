# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation that sends a secret still shows the text MEHO wrote itself.

The dispatcher hides every vendor text of such an operation. A sentence that
MEHO or a connector wrote itself holds no vendor text, so an error type that
has the ``ConnectorOwnMessage`` mark keeps its ``exception_message``. A
plain error type and every vendor field stay hidden as before.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import meho_connector_kit as kit
import pytest
from sqlalchemy import select

import meho_backplane
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector, ConnectorOwnMessage
from meho_backplane.connectors.linux.ops_write import (
    LinuxWriteError,
    LinuxWriteOwnMessageError,
    _resolve_sudo_password,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._errors import MESSAGE_SOURCE_KEY, result_connector_error
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD, withhold_vendor_text
from meho_backplane.settings import get_settings

_SECRET_TAGS = ["license", "license-key"]
_OP = "vmware.license.list"
_OWN_TEXT = "the MEHO account may not read licences; check its rights on the target"
# Made up, and not secret-shaped, so only the rule under test can keep it out.
_VENDOR_TEXT = "the value quokka-" + "7781-zebra-pass is not valid"


class _OwnMessageError(ConnectorOwnMessage, RuntimeError):
    pass


def test_own_message_of_a_secret_op_is_shown() -> None:
    built = result_connector_error(_OP, _OwnMessageError(_OWN_TEXT), 1.0)

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == _OWN_TEXT
    assert out.extras["exception_class"] == "_OwnMessageError"
    assert VENDOR_TEXT_WITHHELD not in out.model_dump_json()


def test_kit_mark_keeps_own_message_and_hides_vendor_fields() -> None:
    class _PackageError(kit.ConnectorOwnMessage, RuntimeError):
        pass

    first = result_connector_error(_OP, _PackageError(_OWN_TEXT), 1.0)
    built = first.model_copy(update={"extras": {**first.extras, "upstream_message": _VENDOR_TEXT}})

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert kit.ConnectorOwnMessage is ConnectorOwnMessage
    assert out.extras["exception_message"] == _OWN_TEXT
    assert out.extras["upstream_message"] == VENDOR_TEXT_WITHHELD
    assert "quokka" not in out.model_dump_json()


def test_vendor_message_of_a_secret_op_stays_hidden() -> None:
    built = result_connector_error(_OP, RuntimeError(_VENDOR_TEXT), 1.0)
    assert "quokka" in built.model_dump_json()  # control: the builder copies it

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    assert "quokka" not in out.model_dump_json()


def test_vendor_fields_stay_hidden_next_to_an_own_message() -> None:
    first = result_connector_error(_OP, _OwnMessageError(_OWN_TEXT), 1.0)
    built = first.model_copy(update={"extras": {**first.extras, "upstream_message": _VENDOR_TEXT}})

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == _OWN_TEXT
    assert out.extras["upstream_message"] == VENDOR_TEXT_WITHHELD


def test_own_message_of_a_plain_op_is_unchanged() -> None:
    built = result_connector_error("op", _OwnMessageError(_OWN_TEXT), 1.0)
    assert withhold_vendor_text(built, op_id="op", tags=["accounts"]) is built


@pytest.mark.asyncio
async def test_linux_sudo_refusal_shows_meho_reason_on_a_secret_op() -> None:
    class _Connector:
        async def _resolve_secret(self, target: object, operator: object) -> dict[str, str]:
            return {"private_key": "x"}  # no sudo_password and no password

    with pytest.raises(LinuxWriteOwnMessageError) as raised:
        await _resolve_sudo_password(_Connector(), object(), None)  # type: ignore[arg-type]

    built = result_connector_error("linux.script.run", raised.value, 1.0)
    out = withhold_vendor_text(built, op_id="linux.script.run", tags=None)

    assert out.extras["exception_class"] == "LinuxWriteOwnMessageError"
    assert "sudo_password" in out.extras["exception_message"]
    assert VENDOR_TEXT_WITHHELD not in out.model_dump_json()


def test_other_linux_write_errors_stay_hidden() -> None:
    built = result_connector_error("linux.script.run", LinuxWriteError(_VENDOR_TEXT), 1.0)
    out = withhold_vendor_text(built, op_id="linux.script.run", tags=None)
    assert out.extras["exception_message"] == VENDOR_TEXT_WITHHELD


def test_a_secret_shaped_value_in_an_own_message_is_still_cleaned() -> None:
    secret = "hunter2-" + "quokka-pass"
    built = result_connector_error(_OP, _OwnMessageError(f"login failed: password={secret}"), 1.0)

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"].startswith("login failed: ")
    assert secret not in out.model_dump_json()


class _MarkedHttpError(ConnectorOwnMessage, httpx.HTTPStatusError):
    pass


def test_a_marked_http_error_stays_hidden() -> None:
    request = httpx.Request("POST", "https://appliance.example.test/api/licences")
    response = httpx.Response(500, text=_VENDOR_TEXT, request=request)
    built = result_connector_error(
        _OP, _MarkedHttpError(_VENDOR_TEXT, request=request, response=response), 1.0
    )
    assert MESSAGE_SOURCE_KEY not in built.extras

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    assert "quokka" not in out.model_dump_json()


# ---------------------------------------------------------------------------
# Every error type in the core that carries the mark
# ---------------------------------------------------------------------------

#: Each error type in ``meho_backplane`` whose message is shown on an operation
#: that sends a secret. A new one fails the test below until it is added here,
#: so each new one is seen in review.
_MARKED_TYPES = {"LinuxWriteOwnMessageError"}


def _base_names(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def test_every_marked_error_type_in_the_core_is_listed() -> None:
    classes: list[tuple[str, set[str]]] = []
    for path in Path(meho_backplane.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes += [(n.name, _base_names(n)) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    marked = {"ConnectorOwnMessage"}
    while True:
        more = {name for name, bases in classes if bases & marked} - marked
        if not more:
            break
        marked |= more

    assert marked - {"ConnectorOwnMessage"} == _MARKED_TYPES


# ---------------------------------------------------------------------------
# Through the dispatcher: the answer and the audit row
# ---------------------------------------------------------------------------

_TENANT = UUID("00000000-0000-0000-0000-00000000a6b1")
_DISPATCH_OP = "demo.licence.assign"
_DISPATCH_TAGS = ["license", "license-key"]


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


class _DemoConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


class _Fingerprint:
    version = None


class _Target:
    def __init__(self) -> None:
        self.product = "demo"
        self.fingerprint = _Fingerprint()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "demo-target"
        self.host = "demo.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _raising_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    raise _OwnMessageError(_OWN_TEXT)


async def _returning_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    return OperationResult(
        status="error",
        op_id=_DISPATCH_OP,
        error="connector_error: RuntimeError",
        duration_ms=0.0,
        extras={
            "error_code": "connector_error",
            "exception_message": _VENDOR_TEXT,
            MESSAGE_SOURCE_KEY: "meho",
        },
    )


async def _dispatch(handler: Any) -> tuple[Any, dict[str, Any]]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="demo", version="", impl_id="", cls=_DemoConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=_DISPATCH_OP,
        handler=handler,
        summary="Demo op that sends a licence key.",
        description="Demo op for the own-message rule.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
        tags=_DISPATCH_TAGS,
    )
    operator = Operator(
        sub="op-own-message",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    result = await dispatch(
        operator=operator,
        connector_id="demo-1.x",
        op_id=_DISPATCH_OP,
        target=_Target(),
        params={},
    )
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == _DISPATCH_OP)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    return result, rows[0].payload


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_env")
async def test_dispatch_shows_a_raised_own_message_in_the_answer_and_the_audit_row() -> None:
    result, payload = await _dispatch(_raising_handler)

    assert result.status == "error"
    assert result.extras["exception_message"] == _OWN_TEXT
    assert payload["error"]["exception_message"] == _OWN_TEXT
    assert result.extras[MESSAGE_SOURCE_KEY] == "meho"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_env")
async def test_dispatch_hides_vendor_text_of_a_returned_result_that_claims_the_mark() -> None:
    result, payload = await _dispatch(_returning_handler)

    assert result.status == "error"
    assert result.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    assert payload["error"]["exception_message"] == VENDOR_TEXT_WITHHELD
    assert MESSAGE_SOURCE_KEY not in result.extras
    assert "quokka" not in result.model_dump_json()
    assert "quokka" not in str(payload)

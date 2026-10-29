# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the FreeIPA connector: login, session, errors, fingerprint, registration.

What is covered, with a respx-mocked IPA server and no ingested catalog:

* the login (form fields ``user`` and ``password``, ``Referer`` and
  ``Accept: text/plain``) and, on every JSON-RPC call, the session cookie,
  the ``Referer`` and the API version;
* the ``Referer`` names ``tls_server_name`` when the target sets it;
* one login per credential; a ``401`` logs in again for that credential only;
* a refused login names FreeIPA's reason; a ``400`` names the ``Referer`` rule;
  a command error names the command, FreeIPA's error and its text;
* a call as MEHO's system identity never uses a cached session;
* the fingerprint and the probe; the registration shape;
* no password and no session cookie in a result or a stored trace;
* the wire helpers: request body, answer unwrapping, value decoding, ``ping``.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.freeipa import (
    FREEIPA_CONNECTOR_ID,
    FREEIPA_IMPL_ID,
    FREEIPA_PRODUCT,
    FREEIPA_TYPED_OPS,
    FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP,
    FreeipaConnector,
    register_freeipa_typed_operations,
)
from meho_backplane.connectors.freeipa.rpc import (
    API_VERSION,
    FreeipaCommandError,
    build_request,
    decode_values,
    parse_ping_summary,
    unwrap_answer,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, EndpointDescriptor, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings
from tests._freeipa_fixtures import (
    ALL_ANSWERS,
    BASE_URL,
    FREEIPA_VERSION,
    HOST,
    JSON,
    LOGIN,
    PASSWORD,
    PRINCIPAL,
    SESSION_COOKIE,
    USERNAME,
    Target,
    credentials_loader,
    dispatch_op,
    error_answer,
    fake_ipa,
    login_response,
    make_operator,
    ok_answer,
    register,
    run,
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
        product=FREEIPA_PRODUCT,
        version=FREEIPA_VERSION,
        impl_id=FREEIPA_IMPL_ID,
        cls=FreeipaConnector,
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


def _rendered(result: Any) -> str:
    return (result.error or "") + json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# Login and session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_logs_in_and_sends_cookie_referer_and_api_version(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, calls, login = await run("freeipa.about", {})

    assert result.status == "ok", result.error
    assert login.call_count == 1
    login_request = login.calls[0].request
    assert login_request.headers["referer"] == f"https://{HOST}/ipa"
    assert login_request.headers["accept"] == "text/plain"
    assert login_request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert login_request.content.decode() == f"user={USERNAME}&password={PASSWORD}"

    assert len(calls) == 1
    assert calls[0]["body"] == {
        "method": "ping",
        "params": [[], {"version": API_VERSION}],
        "id": 0,
    }
    headers = calls[0]["headers"]
    assert headers["cookie"] == f"ipa_session={SESSION_COOKIE}"
    assert headers["referer"] == f"https://{HOST}/ipa"
    assert result.result == {
        "server_version": "4.13.4",
        "api_version": "2.257",
        "connector_api_version": API_VERSION,
        "principal": PRINCIPAL,
    }


@pytest.mark.asyncio
async def test_referer_names_the_tls_server_name(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    target = Target(tls_server_name="ipa01.example.test")
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(return_value=login_response())
        mock.post(JSON).mock(side_effect=fake_ipa(ALL_ANSWERS, calls))
        result = await dispatch_op("freeipa.about", {}, target)

    assert result.status == "ok", result.error
    assert login.calls[0].request.headers["referer"] == "https://ipa01.example.test/ipa"
    assert calls[0]["headers"]["referer"] == "https://ipa01.example.test/ipa"


@pytest.mark.asyncio
async def test_second_call_reuses_the_session(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    target = Target()
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(return_value=login_response())
        mock.post(JSON).mock(side_effect=fake_ipa(ALL_ANSWERS, calls))
        first = await dispatch_op("freeipa.user.list", {}, target)
        second = await dispatch_op("freeipa.group.list", {}, target)
    assert first.status == second.status == "ok"
    assert login.call_count == 1


@pytest.mark.asyncio
async def test_401_on_a_call_logs_in_again_for_that_credential_only(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    first, second = Target(), Target(secret_ref="targets/op-reads/freeipa-second")
    calls: list[dict[str, Any]] = []
    rejected = {"done": False}
    ok = fake_ipa(ALL_ANSWERS, calls)

    def _json(request: httpx.Request) -> httpx.Response:
        if "MagBearerToken=old" in request.headers.get("cookie", "") and not rejected["done"]:
            rejected["done"] = True
            return httpx.Response(401)
        return ok(request)

    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(
            side_effect=[
                login_response("MagBearerToken=other"),
                login_response("MagBearerToken=old"),
                login_response("MagBearerToken=new"),
            ]
        )
        mock.post(JSON).mock(side_effect=_json)
        assert (await dispatch_op("freeipa.about", {}, second)).status == "ok"
        result = await dispatch_op("freeipa.about", {}, first)
        again = await dispatch_op("freeipa.about", {}, second)

    assert result.status == "ok", result.error
    assert again.status == "ok"
    assert login.call_count == 3
    cookies = [c["headers"]["cookie"] for c in calls]
    assert any("MagBearerToken=new" in c for c in cookies)
    assert "MagBearerToken=other" in cookies[-1]


@pytest.mark.asyncio
async def test_system_operator_never_uses_a_cached_session() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    target = Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(return_value=login_response())
        await connector.auth_headers(target, make_operator())
        await connector.auth_headers(target, make_operator())
        await connector.auth_headers(target, synthesise_system_operator())
    assert login.call_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_login_names_freeipa_reason(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post(LOGIN).respond(401, headers={"X-IPA-Rejection-Reason": "password-expired"})
        json_route = mock.post(JSON).mock(return_value=ok_answer({}))
        result = await dispatch_op("freeipa.about", {})

    assert result.status == "error"
    assert json_route.call_count == 0
    rendered = _rendered(result)
    assert "401" in rendered
    assert "password-expired" in rendered
    assert PASSWORD not in rendered


@pytest.mark.asyncio
async def test_400_names_the_referer_rule(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, _, _ = await run("freeipa.about", {}, {"ping": httpx.Response(400, text="denied")})
    assert result.status == "error"
    rendered = _rendered(result)
    assert "400" in rendered
    assert "tls_server_name" in rendered


@pytest.mark.asyncio
async def test_400_on_login_names_the_referer_rule() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post(LOGIN).respond(400, text="denied")
        with pytest.raises(RuntimeError, match="tls_server_name"):
            await connector.auth_headers(Target(), make_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_command_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    missing = error_answer("NotFound", 4001, "web99.example.test: host not found")
    params = {"fqdn": "web99.example.test"}
    result, _, _ = await run("freeipa.host.show", params, {"host_show": missing})
    assert result.status == "error"
    rendered = _rendered(result)
    assert "host_show" in rendered
    assert "NotFound" in rendered
    assert "host not found" in rendered


@pytest.mark.asyncio
async def test_http_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, _, _ = await run("freeipa.user.list", {}, {"user_find": httpx.Response(503)})
    assert result.status == "error"
    assert "503" in _rendered(result)


@pytest.mark.asyncio
async def test_login_without_a_cookie_fails() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post(LOGIN).respond(200, text="")
        with pytest.raises(RuntimeError, match="without a session cookie"):
            await connector.auth_headers(Target(), make_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_rejects_other_auth_models() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    target = Target()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, make_operator())


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


async def _enable_trace_capture(session: AsyncSession) -> None:
    session.add(
        Tenant(
            id=make_operator().tenant_id,
            slug="freeipa-reads",
            name="freeipa-reads",
            flight_recorder_enabled=True,
        )
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _vendor_call_spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = (
            await s.execute(
                select(DispatchTraceSpan)
                .where(DispatchTraceSpan.trace_id == trace.id)
                .order_by(DispatchTraceSpan.seq.asc())
            )
        ).scalars()
        return [span for span in spans if span.span_kind == "vendor_call"]


@pytest.mark.asyncio
async def test_result_and_trace_hold_no_password_or_session_cookie(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    await _enable_trace_capture(session)
    result, calls, _ = await run("freeipa.user.list", {"text": "jdoe"})

    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == len(calls) == 1
    rendered = json.dumps([f"{span.name}|{span.attributes}" for span in spans], default=str)
    rendered += json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert PASSWORD not in rendered
    assert "made-up-session" not in rendered
    assert "login_password" not in rendered


# ---------------------------------------------------------------------------
# Fingerprint and probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reports_version_api_version_and_principal() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post(LOGIN).mock(return_value=login_response())
        mock.post(JSON).mock(side_effect=fake_ipa(ALL_ANSWERS, calls))
        fp = await connector.fingerprint(Target(), make_operator())
    assert (fp.vendor, fp.product, fp.version, fp.reachable) == (
        "freeipa",
        "freeipa",
        "4.13.4",
        True,
    )
    assert fp.extras == {"api_version": "2.257", "principal": PRINCIPAL}
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_and_probe_report_an_unreachable_target() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post(LOGIN).respond(401, headers={"X-IPA-Rejection-Reason": "invalid-password"})
        fp = await connector.fingerprint(Target(), make_operator())
        probe = await connector.probe(Target())
    assert fp.reachable is False
    assert "invalid-password" in str(fp.extras["error"])
    assert probe.ok is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_401_in_the_fingerprint_drops_the_session() -> None:
    connector = FreeipaConnector(credentials_loader=credentials_loader)
    target = Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(return_value=login_response())
        mock.post(JSON).respond(401)
        first = await connector.fingerprint(target, make_operator())
        second = await connector.fingerprint(target, make_operator())
    assert first.reachable is False and second.reachable is False
    assert login.call_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_freeipa_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == FREEIPA_PRODUCT,
                    EndpointDescriptor.impl_id == FREEIPA_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in FREEIPA_TYPED_OPS}
    assert len(rows) == 17
    for row in rows:
        assert row.source_kind == "typed"
        assert row.handler_ref is not None
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.parameter_schema["additionalProperties"] is False
        assert row.parameter_schema["examples"]
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)


def test_every_op_has_a_group_text_and_names_only_its_own_ops() -> None:
    op_ids = {op.op_id for op in FREEIPA_TYPED_OPS}
    assert {op.group_key for op in FREEIPA_TYPED_OPS} == set(FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP)
    texts = [*FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP.values()]
    for op in FREEIPA_TYPED_OPS:
        texts.extend(str(value) for value in (op.llm_instructions or {}).values())
    named = {word.strip(".,;:()'") for text in texts for word in text.split()}
    assert {word for word in named if word.startswith("freeipa.")} <= op_ids


def test_package_registers_versioned_connector_and_wildcard() -> None:
    clear_registry()
    register_connector_v2(
        product=FREEIPA_PRODUCT,
        version=FREEIPA_VERSION,
        impl_id=FREEIPA_IMPL_ID,
        cls=FreeipaConnector,
    )
    register_connector_v2(product=FREEIPA_PRODUCT, version="", impl_id="", cls=FreeipaConnector)
    registry = all_connectors_v2()
    assert registry[(FREEIPA_PRODUCT, FREEIPA_VERSION, FREEIPA_IMPL_ID)] is FreeipaConnector
    assert registry[(FREEIPA_PRODUCT, "", "")] is FreeipaConnector
    assert FREEIPA_CONNECTOR_ID == "freeipa-jsonrpc-4.x"


@pytest.mark.parametrize("version", ["4.9.8", "4.10.2", "4.12.2", "4.13.4"])
def test_each_covered_version_resolves_to_the_connector(version: str) -> None:
    assert resolve_connector(Target(version=version)) is FreeipaConnector


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def test_build_request_adds_the_api_version_and_drops_empty_options() -> None:
    assert build_request("host_find", ["web"], {"sizelimit": 5, "in_hostgroup": None}) == {
        "method": "host_find",
        "params": [["web"], {"sizelimit": 5, "version": API_VERSION}],
        "id": 0,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "not a JSON object"),
        ({"error": None}, "has no result"),
        ({"error": "plain text"}, "Error: plain text"),
        (
            {"result": None, "error": {"code": 4001, "name": "NotFound", "message": "gone"}},
            "NotFound 4001: gone",
        ),
    ],
)
def test_unwrap_answer_errors(payload: Any, message: str) -> None:
    with pytest.raises(FreeipaCommandError, match=message):
        unwrap_answer(payload, "user_show")


def test_unwrap_answer_returns_result_principal_and_version() -> None:
    answer = unwrap_answer(
        {"result": {"value": "x"}, "error": None, "principal": PRINCIPAL, "version": "4.13.4"},
        "user_show",
    )
    assert (answer.result, answer.principal, answer.server_version) == (
        {"value": "x"},
        PRINCIPAL,
        "4.13.4",
    )


def test_decode_values_turns_tagged_values_into_text() -> None:
    assert decode_values(
        {
            "when": [{"__datetime__": "20261029120000Z"}],
            "zone": {"__dns_name__": "example.test."},
            "blob": {"__base64__": "AAE="},
            "bad": {"__datetime__": "not-a-date"},
            "plain": ["a", 1, True, None],
        }
    ) == {
        "when": ["2026-10-29T12:00:00Z"],
        "zone": "example.test.",
        "blob": {"__base64__": "AAE="},
        "bad": "not-a-date",
        "plain": ["a", 1, True, None],
    }


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("IPA server version 4.13.4. API version 2.257", ("4.13.4", "2.257")),
        ("IPA server version 4.12.2-1.el9. API version 2.254", ("4.12.2-1.el9", "2.254")),
        ("something else", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_ping_summary(summary: Any, expected: tuple[str | None, str | None]) -> None:
    assert parse_ping_summary(summary) == expected

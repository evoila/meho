# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Entra ID read operations, through the real dispatcher.

The dispatch tests run the real dispatcher (policy, audit, result reduction)
with only the Entra ID connector registered. The lists over 50 rows go
through the real result reducer (``JsonFluxReducer``) and ``result_query``,
to prove that the rows go to a stored result while the fields next to them
stay inline. Microsoft's login service and Microsoft Graph are mocked with
respx. Every id, name and secret here is made up.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.entra import (
    ENTRA_CONNECTOR_ID,
    ENTRA_IMPL_ID,
    ENTRA_PRODUCT,
    ENTRA_TYPED_OPS,
    ENTRA_TYPED_WHEN_TO_USE_BY_GROUP,
    ENTRA_VERSION,
    EntraConnector,
    register_entra_typed_operations,
    typed_reads,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    CLIENT_A,
    OBJECT_A,
    SECRET_A,
    TENANT_A,
    operator,
    token_answer,
    token_url,
)
from tests._entra_fixtures import (
    ALL_ROLES,
    APPLICATIONS_URL,
    CERTIFICATE_DATA,
    GROUPS_URL,
    ORGANIZATION_URL,
    ROLE_ASSIGNMENTS_URL,
    ROLE_DEFINITIONS_URL,
    SECRET_HINT,
    SERVICE_PRINCIPALS_URL,
    USERS_URL,
    EntraTargetStub,
    app_item,
    entra_loader,
    graph_token,
    group_item,
    guid,
    next_link,
    organization_answer,
    page,
    role_assignment_item,
    role_definition_item,
    service_principal_item,
    user_item,
)
from tests._result_store_helpers import MemoryResultStore

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

ENTRA_OP_IDS = [
    "entra.about",
    "entra.user.list",
    "entra.group.list",
    "entra.role.assignment.list",
    "entra.app.list",
    "entra.service_principal.list",
]

#: Made-up role definition ids.
GLOBAL_ADMIN = guid("e", 1)
READER = guid("e", 2)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _entra_only_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=ENTRA_PRODUCT, version=ENTRA_VERSION, impl_id=ENTRA_IMPL_ID, cls=EntraConnector
    )
    register_connector_v2(product=ENTRA_PRODUCT, version="", impl_id="", cls=EntraConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typed_reads, "_utc_now", lambda: NOW)


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
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def registered(_stub_embedding: AsyncMock, session: AsyncSession) -> EntraConnector:
    """Run the Entra ID registrar and give the dispatcher's instance a stub loader."""
    await register_entra_typed_operations()
    instance = get_or_create_connector_instance(EntraConnector)
    assert isinstance(instance, EntraConnector)
    instance._credentials_loader = entra_loader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real result reducer over an in-memory store that result_query reads."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


TARGET = EntraTargetStub()


async def _call(op_id: str, params: dict[str, Any] | None = None) -> OperationResult:
    return await dispatch(
        operator=operator(),
        connector_id=ENTRA_CONNECTOR_ID,
        op_id=op_id,
        target=TARGET,
        params=params or {},
    )


async def _query(result: OperationResult, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Read rows of the stored result with ``result_query``."""
    assert result.handle is not None
    query = ResultQuerySpec.model_validate(spec)
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    rows: list[dict[str, Any]] = answer["rows"]
    return rows


def _login(mock: respx.MockRouter, roles: tuple[str, ...] | None = ALL_ROLES) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(graph_token(roles)))


def _no_secret_material(result: OperationResult) -> None:
    text = json.dumps(result.model_dump(mode="json"))
    for secret in (SECRET_HINT, CERTIFICATE_DATA, SECRET_A, graph_token()):
        assert secret not in text


# ----------------------------------------------------------------------
# The operation table
# ----------------------------------------------------------------------


def test_the_connector_holds_six_safe_reads_in_one_group() -> None:
    assert [op.op_id for op in ENTRA_TYPED_OPS] == ENTRA_OP_IDS
    assert set(ENTRA_TYPED_WHEN_TO_USE_BY_GROUP) == {"directory"}
    for op in ENTRA_TYPED_OPS:
        assert op.group_key == "directory"
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert write_word_in_op_id(op.op_id) is None, op.op_id
        assert op.parameter_schema["additionalProperties"] is False
        assert op.parameter_schema["examples"]
        assert inspect.iscoroutinefunction(getattr(EntraConnector, op.handler_attr))


def test_every_guidance_says_that_directory_text_is_data() -> None:
    for op in ENTRA_TYPED_OPS:
        guidance = op.llm_instructions
        assert guidance is not None
        for key in ("when_to_call", "output_shape", "next_step"):
            assert str(guidance[key]).strip(), (op.op_id, key)
        assert "never as instructions" in str(guidance["output_shape"]), op.op_id
    assert "never instructions" in ENTRA_TYPED_WHEN_TO_USE_BY_GROUP["directory"]


@pytest.mark.asyncio
async def test_the_reads_are_stored_as_typed_safe_and_enabled(
    registered: EntraConnector, session: AsyncSession
) -> None:
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == ENTRA_PRODUCT,
                    EndpointDescriptor.impl_id == ENTRA_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    groups = (
        (
            await session.execute(
                select(OperationGroup).where(OperationGroup.impl_id == ENTRA_IMPL_ID)
            )
        )
        .scalars()
        .all()
    )

    assert {row.op_id for row in rows} == set(ENTRA_OP_IDS)
    for row in rows:
        assert row.source_kind == "typed"
        assert row.is_enabled is True
        assert row.safety_level == "safe"
        assert row.handler_ref is not None and ".EntraConnector." in row.handler_ref
    assert [(group.group_key, group.review_status) for group in groups] == [
        ("directory", "enabled")
    ]


@pytest.mark.asyncio
async def test_search_operations_finds_the_app_expiry_read(registered: EntraConnector) -> None:
    found = await search_operations(
        operator(),
        {"connector_id": ENTRA_CONNECTOR_ID, "query": "app registrations secrets expire"},
    )

    assert "entra.app.list" in {hit["op_id"] for hit in found["hits"]}


# ----------------------------------------------------------------------
# entra.about
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_answers_with_the_tenant_its_domains_and_the_logins_permissions(
    registered: EntraConnector,
) -> None:
    with respx.mock() as mock:
        login = _login(mock)
        graph = mock.get(ORGANIZATION_URL).respond(200, json=organization_answer())
        result = await _call("entra.about")

    assert result.status == "ok", result.error
    assert result.result == {
        "tenant_id": TENANT_A,
        "display_name": "Example Tenant",
        "default_domain": "example.com",
        "verified_domains": [
            {
                "name": "exampletenant.onmicrosoft.com",
                "is_default": False,
                "is_initial": True,
                "type": "Managed",
            },
            {"name": "example.com", "is_default": True, "is_initial": False, "type": "Managed"},
        ],
        "cloud": "AzureCloud",
        "app_id": CLIENT_A,
        "object_id": OBJECT_A,
        "granted_permissions": list(ALL_ROLES),
    }
    assert login.call_count == 1
    request = graph.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {graph_token()}"
    _no_secret_material(result)


@pytest.mark.asyncio
async def test_about_without_the_organization_permission_names_it(
    registered: EntraConnector,
) -> None:
    denied = {
        "error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges."}
    }

    with respx.mock() as mock:
        _login(mock, roles=("User.Read.All",))
        mock.get(ORGANIZATION_URL).respond(403, json=denied)
        result = await _call("entra.about")

    assert result.status == "error"
    assert result.error == "connector_error: EntraPermissionError"
    extras = result.extras or {}
    assert extras["http_status"] == 403
    assert "Organization.Read.All with admin consent" in extras["exception_message"]
    assert "carries: User.Read.All" in extras["remediation"]
    assert "Grant admin consent" in extras["remediation"]
    assert extras["upstream_message"] == "Authorization_RequestDenied: Insufficient privileges."


@pytest.mark.asyncio
async def test_a_refused_login_fails_clearly_and_the_secret_is_read_again(
    registered: EntraConnector,
) -> None:
    loader = entra_loader()
    registered._credentials_loader = loader
    refusal = {"error": "invalid_client", "error_codes": [7000215]}

    with respx.mock() as mock:
        mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(401, json=refusal),
                httpx.Response(200, json=token_answer(graph_token())),
            ]
        )
        mock.get(ORGANIZATION_URL).respond(200, json=organization_answer())
        refused = await _call("entra.about")
        fixed = await _call("entra.about")

    assert refused.status == "error"
    assert refused.error is not None and refused.error.startswith("connector_auth_failed")
    assert "AADSTS7000215" in json.dumps(refused.model_dump(mode="json"))
    assert SECRET_A not in json.dumps(refused.model_dump(mode="json"))
    assert fixed.status == "ok", fixed.error
    assert loader.calls == 2


# ----------------------------------------------------------------------
# entra.user.list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_list_selects_the_fields_and_builds_the_filter(
    registered: EntraConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).respond(200, json=page([user_item(1, guest=True)]))
        plain = await _call("entra.user.list")
        guests = await _call("entra.user.list", {"user_type": "Guest"})
        both = await _call("entra.user.list", {"user_type": "Member", "enabled": False})

    assert plain.status == guests.status == both.status == "ok"
    params = [call.request.url.params for call in graph.calls]
    assert params[0]["$select"] == (
        "id,displayName,userPrincipalName,accountEnabled,userType,createdDateTime"
    )
    assert params[0]["$top"] == "999"
    assert "$filter" not in params[0]
    assert params[1]["$filter"] == "userType eq 'Guest'"
    assert params[2]["$filter"] == "userType eq 'Member' and accountEnabled eq false"
    assert plain.result["value"] == [
        {
            "id": guid("1", 1),
            "display_name": "Example Person 1",
            "user_principal_name": "person1@example.com",
            "account_enabled": True,
            "user_type": "Guest",
            "created_at": "2026-01-02T03:04:05Z",
        }
    ]


@pytest.mark.asyncio
async def test_user_list_refuses_an_unknown_user_type_before_any_call(
    registered: EntraConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("entra.user.list", {"user_type": "Admin' or true or '"})

    assert result.status == "error"
    assert not login.called


@pytest.mark.asyncio
async def test_user_list_over_50_rows_goes_to_a_stored_result(
    registered: EntraConnector, store: MemoryResultStore
) -> None:
    users = [user_item(n, guest=n % 4 == 0) for n in range(70)]

    with respx.mock() as mock:
        _login(mock)
        mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page(users[:40], next_link(USERS_URL))),
                httpx.Response(200, json=page(users[40:])),
            ]
        )
        result = await _call("entra.user.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 70
    assert (summary["truncated"], summary["pages_read"], summary["omission"]) == (False, 2, None)
    guests = await _query(result, {"filter": [{"field": "user_type", "op": "=", "value": "Guest"}]})
    assert len(guests) == 18


# ----------------------------------------------------------------------
# entra.group.list
# ----------------------------------------------------------------------


def _count_url(group_id: str) -> str:
    return f"{GROUPS_URL}/{group_id}/members/$count"


@pytest.mark.asyncio
async def test_group_list_names_the_type_and_membership_of_each_group(
    registered: EntraConnector,
) -> None:
    kinds = ["microsoft_365", "security", "mail_enabled_security", "distribution", "other"]
    groups = [group_item(n, kind) for n, kind in enumerate(kinds)]
    groups.append(group_item(9, "security", dynamic=True))

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(GROUPS_URL).respond(200, json=page(groups))
        result = await _call("entra.group.list")

    assert result.status == "ok", result.error
    rows = result.result["value"]
    assert [row["group_type"] for row in rows] == [*kinds, "security"]
    assert [row["membership"] for row in rows] == ["assigned"] * 5 + ["dynamic"]
    assert all(row["member_count"] is None for row in rows)
    assert result.result["member_counts"] == "not_requested"
    assert graph.calls[0].request.url.params["$select"] == (
        "id,displayName,groupTypes,mailEnabled,securityEnabled"
    )


@pytest.mark.asyncio
async def test_group_list_counts_members_on_request(registered: EntraConnector) -> None:
    groups = [group_item(1), group_item(2), group_item(3)]

    with respx.mock() as mock:
        _login(mock)
        mock.get(GROUPS_URL).respond(200, json=page(groups))
        first = mock.get(_count_url(guid("2", 1))).respond(200, text="﻿12")
        mock.get(_count_url(guid("2", 2))).respond(200, text="0")
        mock.get(_count_url(guid("2", 3))).respond(404, json={"error": {"code": "NotFound"}})
        result = await _call("entra.group.list", {"include_member_count": True})

    assert result.status == "ok", result.error
    assert [row["member_count"] for row in result.result["value"]] == [12, 0, None]
    assert result.result["member_counts"] == "counted"
    assert first.calls[0].request.headers["ConsistencyLevel"] == "eventual"


@pytest.mark.asyncio
async def test_group_list_counts_no_members_for_more_than_100_groups(
    registered: EntraConnector, store: MemoryResultStore
) -> None:
    groups = [group_item(n, "microsoft_365" if n < 30 else "security") for n in range(101)]

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(GROUPS_URL).respond(200, json=page(groups))
        result = await _call("entra.group.list", {"include_member_count": True})

    assert result.status == "ok", result.error
    assert graph.call_count == 1
    assert result.handle is not None
    assert result.result["row_count"] == 101
    assert result.result["member_counts"] == "too_many_groups"
    assert result.result["truncated"] is False
    m365 = await _query(
        result, {"filter": [{"field": "group_type", "op": "=", "value": "microsoft_365"}]}
    )
    assert len(m365) == 30


# ----------------------------------------------------------------------
# entra.role.assignment.list
# ----------------------------------------------------------------------


def _definitions() -> dict[str, Any]:
    return page(
        [
            role_definition_item(GLOBAL_ADMIN, "Global Administrator"),
            role_definition_item(READER, "Directory Readers"),
        ]
    )


@pytest.mark.asyncio
async def test_role_assignment_list_names_roles_and_principals(
    registered: EntraConnector,
) -> None:
    assignments = [
        role_assignment_item(1, GLOBAL_ADMIN, "user"),
        role_assignment_item(2, READER, "servicePrincipal"),
        role_assignment_item(3, guid("9", 9), "group"),
    ]
    assignments[2]["principal"] = {"@odata.type": "#microsoft.graph.group", "id": guid("3", 3)}

    with respx.mock() as mock:
        _login(mock)
        mock.get(ROLE_DEFINITIONS_URL).respond(200, json=_definitions())
        graph = mock.get(ROLE_ASSIGNMENTS_URL).respond(200, json=page(assignments))
        result = await _call("entra.role.assignment.list")

    assert result.status == "ok", result.error
    assert graph.calls[0].request.url.params["$expand"] == "principal"
    assert result.result["value"] == [
        {
            "id": "example-assignment-1",
            "role_name": "Global Administrator",
            "role_definition_id": GLOBAL_ADMIN,
            "principal_id": guid("3", 1),
            "principal_type": "user",
            "principal_name": "Example Principal 1",
            "scope": "/",
        },
        {
            "id": "example-assignment-2",
            "role_name": "Directory Readers",
            "role_definition_id": READER,
            "principal_id": guid("3", 2),
            "principal_type": "servicePrincipal",
            "principal_name": "Example Principal 2",
            "scope": "/",
        },
        {
            "id": "example-assignment-3",
            "role_name": None,
            "role_definition_id": guid("9", 9),
            "principal_id": guid("3", 3),
            "principal_type": "group",
            "principal_name": None,
            "scope": "/",
        },
    ]


@pytest.mark.asyncio
async def test_role_assignment_list_over_50_rows_goes_to_a_stored_result(
    registered: EntraConnector, store: MemoryResultStore
) -> None:
    assignments = [
        role_assignment_item(n, GLOBAL_ADMIN if n < 3 else READER, "user") for n in range(55)
    ]

    with respx.mock() as mock:
        _login(mock)
        mock.get(ROLE_DEFINITIONS_URL).respond(200, json=_definitions())
        mock.get(ROLE_ASSIGNMENTS_URL).respond(200, json=page(assignments))
        result = await _call("entra.role.assignment.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 55
    assert (result.result["truncated"], result.result["pages_read"]) == (False, 1)
    admins = await _query(
        result, {"filter": [{"field": "role_name", "op": "=", "value": "Global Administrator"}]}
    )
    assert sorted(row["principal_id"] for row in admins) == [guid("3", n) for n in range(3)]


@pytest.mark.asyncio
async def test_role_assignment_list_without_the_permission_names_it(
    registered: EntraConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock, roles=())
        mock.get(ROLE_DEFINITIONS_URL).respond(
            403, json={"error": {"code": "Authorization_RequestDenied"}}
        )
        assignments = mock.get(ROLE_ASSIGNMENTS_URL).respond(200, json=page([]))
        result = await _call("entra.role.assignment.list")

    assert result.error == "connector_error: EntraPermissionError"
    assert "RoleManagement.Read.Directory" in (result.extras or {})["exception_message"]
    assert not assignments.called


# ----------------------------------------------------------------------
# entra.app.list and entra.service_principal.list
# ----------------------------------------------------------------------

SOON = "2026-11-10T00:00:00Z"  # 12 days after NOW
LATER = "2027-06-01T00:00:00Z"
GONE = "2026-09-01T00:00:00Z"  # already run out


@pytest.mark.asyncio
async def test_app_list_shows_counts_and_next_end_dates_and_never_a_secret(
    registered: EntraConnector,
) -> None:
    apps = [
        app_item(1, secret_ends=(GONE, LATER, SOON), certificate_ends=(LATER,)),
        app_item(2, secret_ends=(GONE,)),
        app_item(3),
    ]

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(APPLICATIONS_URL).respond(200, json=page(apps))
        result = await _call("entra.app.list")

    assert result.status == "ok", result.error
    params = graph.calls[0].request.url.params
    assert params["$select"] == "id,appId,displayName,passwordCredentials,keyCredentials"
    assert params["$top"] == "999"
    assert result.result["value"][0] == {
        "id": guid("6", 1),
        "app_id": guid("7", 1),
        "display_name": "Example App 1",
        "secret_count": 3,
        "expired_secret_count": 1,
        "next_secret_expiry": SOON,
        "certificate_count": 1,
        "expired_certificate_count": 0,
        "next_certificate_expiry": LATER,
    }
    second = result.result["value"][1]
    assert (second["expired_secret_count"], second["next_secret_expiry"]) == (1, None)
    assert result.result["read_count"] == 3
    assert result.result["as_of"] == "2026-10-28T12:00:00Z"
    assert result.result["expiring_within_days"] is None
    _no_secret_material(result)


@pytest.mark.asyncio
async def test_app_list_keeps_only_apps_that_run_out_within_the_days_given(
    registered: EntraConnector,
) -> None:
    apps = [
        app_item(1, secret_ends=(SOON,)),
        app_item(2, secret_ends=(LATER,)),
        app_item(3, secret_ends=(GONE,)),
        app_item(4, certificate_ends=(SOON,)),
        app_item(5),
    ]

    with respx.mock() as mock:
        _login(mock)
        mock.get(APPLICATIONS_URL).respond(200, json=page(apps))
        result = await _call("entra.app.list", {"expiring_within_days": 30})

    assert result.status == "ok", result.error
    assert [row["display_name"] for row in result.result["value"]] == [
        "Example App 1",
        "Example App 4",
    ]
    assert result.result["read_count"] == 5
    assert result.result["expiring_within_days"] == 30


@pytest.mark.asyncio
async def test_app_list_over_50_rows_keeps_its_fields_next_to_the_stored_result(
    registered: EntraConnector, store: MemoryResultStore
) -> None:
    apps = [app_item(n, secret_ends=(SOON if n < 5 else LATER, GONE)) for n in range(80)]

    with respx.mock() as mock:
        _login(mock)
        mock.get(APPLICATIONS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page(apps[:60], next_link(APPLICATIONS_URL))),
                httpx.Response(200, json=page(apps[60:])),
            ]
        )
        result = await _call("entra.app.list", {"expiring_within_days": 365})

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 80
    assert summary["read_count"] == 80
    assert summary["as_of"] == "2026-10-28T12:00:00Z"
    assert summary["expiring_within_days"] == 365
    assert (summary["truncated"], summary["pages_read"]) == (False, 2)
    soon = await _query(
        result, {"filter": [{"field": "next_secret_expiry", "op": "=", "value": SOON}]}
    )
    assert len(soon) == 5
    _no_secret_material(result)
    stored = json.dumps(await _query(result, {"limit": 100}))
    assert SECRET_HINT not in stored and CERTIFICATE_DATA not in stored


@pytest.mark.asyncio
async def test_service_principal_list_shows_type_enabled_and_credentials(
    registered: EntraConnector,
) -> None:
    items = [
        service_principal_item(1, kind="Application", certificate_ends=(SOON,)),
        service_principal_item(2, kind="ManagedIdentity", certificate_ends=(LATER,)),
    ]

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(SERVICE_PRINCIPALS_URL).respond(200, json=page(items))
        result = await _call("entra.service_principal.list", {"expiring_within_days": 30})

    assert result.status == "ok", result.error
    assert graph.calls[0].request.url.params["$top"] == "100"
    assert graph.calls[0].request.url.params["$select"] == (
        "id,appId,displayName,servicePrincipalType,accountEnabled,"
        "passwordCredentials,keyCredentials"
    )
    assert result.result["value"] == [
        {
            "id": guid("8", 1),
            "app_id": guid("7", 1),
            "display_name": "Example App 1",
            "service_principal_type": "Application",
            "account_enabled": True,
            "secret_count": 0,
            "expired_secret_count": 0,
            "next_secret_expiry": None,
            "certificate_count": 1,
            "expired_certificate_count": 0,
            "next_certificate_expiry": SOON,
        }
    ]
    assert result.result["read_count"] == 2
    _no_secret_material(result)


@pytest.mark.asyncio
async def test_service_principal_list_over_50_rows_goes_to_a_stored_result(
    registered: EntraConnector, store: MemoryResultStore
) -> None:
    items = [
        service_principal_item(n, kind="ManagedIdentity" if n < 10 else "Application")
        for n in range(150)
    ]
    link = f"{SERVICE_PRINCIPALS_URL}?$top=100&$skiptoken=page2"

    with respx.mock() as mock:
        _login(mock)
        mock.get(SERVICE_PRINCIPALS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page(items[:100], link)),
                httpx.Response(200, json=page(items[100:])),
            ]
        )
        result = await _call("entra.service_principal.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 150
    assert result.result["read_count"] == 150
    assert result.result["expiring_within_days"] is None
    assert result.result["pages_read"] == 2
    managed = await _query(
        result,
        {"filter": [{"field": "service_principal_type", "op": "=", "value": "ManagedIdentity"}]},
    )
    assert len(managed) == 10

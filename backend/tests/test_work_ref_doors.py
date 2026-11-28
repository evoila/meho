# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A new door for a ticket id (work ref) cannot be added without the shared rule.

The test walks every REST and console request model field, every query,
path, header and form parameter, and every MCP tool argument named
``work_ref``. It fails
when one of them neither uses the shared type or function nor is on a short
list with a reason. The search filters get a request test of their own: a bad
filter value is refused with the rule and without the refused text.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import jsonschema
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, BeforeValidator
from starlette.testclient import TestClient

from meho_backplane.api.request_validation import request_validation_exception_handler
from meho_backplane.api.v1 import (
    agent_runs,
    approvals,
    audit,
    audit_counts,
    audit_models,
    broadcast,
    runbook_runs,
    scheduler,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import broadcast as mcp_broadcast
from meho_backplane.mcp.tools._work_ref_argument import work_ref_argument
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.work_ref import check_work_ref_filter_input, normalize_work_ref_input

_ZWSP = chr(0x200B)

#: The validators of the shared types: ``WorkRef`` cleans a stored ticket id,
#: ``WorkRefFilter`` checks a search filter and keeps its text as typed.
_SHARED_VALIDATORS = (normalize_work_ref_input, check_work_ref_filter_input)

# ---------------------------------------------------------------- REST doors

_CONSOLE_FORM = "console form: the handler cleans it with form_work_ref (the shared rule)"
_CONSOLE_FILTER = (
    "console search box: the handler checks it with filter_work_ref, the rule of the "
    "REST filters; a blank box means no filter"
)

#: Parameters that read a ticket id without the shared type. Each has a
#: reason, and the test fails when an entry matches no route.
_REST_OTHER_DOORS: dict[tuple[str, str], str] = {
    ("/api/v1/agents/{name}/run", "meho_work_ref"): (
        "header: the audit middleware refuses a bad value first, and "
        "_header_work_ref decodes it with decode_work_ref_header"
    ),
    ("/api/v1/agents/{name}/run/events", "meho_work_ref"): (
        "header: the audit middleware refuses a bad value first, and "
        "_header_work_ref decodes it with decode_work_ref_header"
    ),
    # Console forms that store a ticket id: the handler cleans the field with
    # form_work_ref (meho_backplane.ui.routes.work_ref_form), which uses the
    # shared rule. A blank field means "no ticket", so the type stays str.
    ("/ui/agents/{name}/run", "work_ref"): _CONSOLE_FORM,
    ("/ui/operations/call", "work_ref"): _CONSOLE_FORM,
    ("/ui/runbooks/runs", "work_ref"): _CONSOLE_FORM,
    ("/ui/scheduler/create", "work_ref"): _CONSOLE_FORM,
    # Console search boxes that check the value with the rule of the REST
    # filters (filter_work_ref): a bad value is a 422 page that names the rule.
    ("/ui/agents/runs", "work_ref"): _CONSOLE_FILTER,
    ("/ui/audit", "work_ref"): _CONSOLE_FILTER,
    ("/ui/audit/results", "work_ref"): _CONSOLE_FILTER,
    ("/ui/scheduler", "work_ref"): _CONSOLE_FILTER,
    ("/ui/approvals", "work_ref"): _CONSOLE_FILTER,
    ("/ui/approvals/list", "work_ref"): _CONSOLE_FILTER,
    ("/ui/approvals/in-order", "work_ref"): _CONSOLE_FILTER,
}

#: The search filters that must use the shared type: (router path, parameter).
_REST_FILTERS: list[tuple[str, str]] = [
    ("/api/v1/agents/runs", "work_ref"),
    ("/api/v1/approvals", "work_ref"),
    ("/api/v1/broadcast/recent", "work_ref"),
    ("/api/v1/runbooks/runs", "work_ref"),
    ("/api/v1/scheduler/triggers", "work_ref"),
]


def _uses_shared_type(annotation: Any, metadata: tuple[Any, ...] = ()) -> bool:
    """Whether a field type carries the shared ticket id validator."""
    for item in metadata:
        if isinstance(item, BeforeValidator) and item.func in _SHARED_VALIDATORS:
            return True
    if typing.get_origin(annotation) is typing.Annotated:
        first, *rest = typing.get_args(annotation)
        return _uses_shared_type(first, tuple(rest))
    return any(_uses_shared_type(arg) for arg in typing.get_args(annotation))


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def _flat_routes(routes: list[Any]) -> Iterator[Any]:
    """Yield every route, also those of an included router.

    FastAPI keeps an included router as one entry that holds the router
    (``original_router``), not as a copy of its routes.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if isinstance(included, APIRouter):
            yield from _flat_routes(included.routes)
        else:
            yield route


def _console_routers() -> list[APIRouter]:
    """The console routers. They are built by a function, not kept in a module."""
    from meho_backplane.ui.auth import build_router as build_ui_auth_router
    from meho_backplane.ui.routes import build_router as build_ui_router

    return [build_ui_auth_router(), build_ui_router()]


def _api_routers() -> list[APIRouter]:
    routers: dict[int, APIRouter] = {}
    for package_name in ("meho_backplane.api", "meho_backplane.ui"):
        package = importlib.import_module(package_name)
        for info in pkgutil.walk_packages(package.__path__, f"{package_name}."):
            module = importlib.import_module(info.name)
            for value in vars(module).values():
                if isinstance(value, APIRouter):
                    routers[id(value)] = value
    return list(routers.values())


def _route_doors(
    routes: list[Any],
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, bool]]]:
    """Return the parameters and the request model fields named ``work_ref``.

    Parameters are ``(route path, name, uses the shared type)``; model fields
    are ``("<Module>.<Model>.<field>", uses the shared type)``. The walk also
    goes into every dependency (``Depends(...)``) of a route, at any depth.
    """
    params: list[tuple[str, str, bool]] = []
    fields: dict[str, bool] = {}
    seen_models: set[type[BaseModel]] = set()

    def walk(model: type[BaseModel]) -> None:
        if model in seen_models:
            return
        seen_models.add(model)
        for name, info in model.model_fields.items():
            if "work_ref" in name:
                key = f"{model.__module__}.{model.__name__}.{name}"
                fields[key] = _uses_shared_type(info.annotation, tuple(info.metadata))
            for nested in _models_in(info.annotation):
                walk(nested)

    def walk_dependant(path: str, dependant: Dependant) -> None:
        groups = (
            dependant.query_params,
            dependant.header_params,
            dependant.path_params,
            dependant.cookie_params,
            dependant.body_params,
        )
        for group in groups:
            for param in group:
                info = param.field_info
                if "work_ref" in param.name or "work-ref" in str(param.alias):
                    params.append(
                        (
                            path,
                            param.name,
                            _uses_shared_type(info.annotation, tuple(info.metadata)),
                        )
                    )
                for model in _models_in(info.annotation):
                    walk(model)
        for sub in dependant.dependencies:
            walk_dependant(path, sub)

    for route in routes:
        if isinstance(route, APIRoute):
            walk_dependant(route.path, route.dependant)
    return params, sorted(fields.items())


def _rest_doors() -> tuple[list[tuple[str, str, bool]], list[tuple[str, bool]]]:
    """Return the ``work_ref`` parameters and model fields of every REST and console route."""
    routers = _api_routers() + _console_routers()
    return _route_doors(
        list(_flat_routes([route for router in routers for route in router.routes]))
    )


def test_every_rest_work_ref_parameter_uses_the_shared_type() -> None:
    params, _ = _rest_doors()
    assert params, "the walk found no work_ref parameter: the test is broken"
    found_other: set[tuple[str, str]] = set()
    missing: list[str] = []
    for path, name, shared in params:
        if shared:
            continue
        if (path, name) in _REST_OTHER_DOORS:
            found_other.add((path, name))
        else:
            missing.append(f"{path} {name}")
    assert not missing, (
        "These parameters take a ticket id without the shared type WorkRef "
        f"(meho_backplane.work_ref): {missing}. Use the type, or add the parameter "
        "to _REST_OTHER_DOORS with a reason."
    )
    assert found_other == set(_REST_OTHER_DOORS), "an entry of _REST_OTHER_DOORS matches no route"


def test_every_rest_work_ref_model_field_uses_the_shared_type() -> None:
    _, fields = _rest_doors()
    assert fields, "the walk found no work_ref model field: the test is broken"
    missing = [key for key, shared in fields if not shared]
    assert not missing, (
        "These request model fields take a ticket id without the shared type WorkRef "
        f"(meho_backplane.work_ref): {missing}"
    )


def test_the_search_filters_are_found_and_use_the_shared_type() -> None:
    params, fields = _rest_doors()
    shared_params = {(path, name) for path, name, shared in params if shared}
    for door in _REST_FILTERS:
        assert door in shared_params, door
    filter_fields = dict(fields)
    assert filter_fields[f"{audit_models.__name__}.AuditQueryRequest.work_ref"]
    assert filter_fields[f"{audit_models.__name__}.AuditClassCountsRequest.work_ref"]


def test_the_audit_path_filter_uses_the_shared_type() -> None:
    route = next(
        r
        for r in audit.router.routes
        if isinstance(r, APIRoute) and r.path == "/api/v1/audit/by-work-ref/{ref:path}"
    )
    (ref,) = [p for p in route.dependant.path_params if p.name == "ref"]
    assert _uses_shared_type(ref.field_info.annotation, tuple(ref.field_info.metadata))


class _DependencyRequest(BaseModel):
    work_ref: str


class _DependencyBody(BaseModel):
    work_ref: str


def _dependency_body(body: _DependencyBody) -> _DependencyBody:
    return body


def _nested_dependency(body: _DependencyBody = Depends(_dependency_body)) -> _DependencyBody:
    return body


def test_the_walk_finds_a_work_ref_behind_a_dependency() -> None:
    router = APIRouter()

    @router.get("/query-model")
    async def _query_model(request: _DependencyRequest = Depends()) -> None:
        del request

    @router.post("/nested-body")
    async def _nested_body(body: _DependencyBody = Depends(_nested_dependency)) -> None:
        del body

    params, fields = _route_doors(list(router.routes))
    assert ("/query-model", "work_ref", False) in params
    assert (f"{__name__}._DependencyBody.work_ref", False) in fields


# ---------------------------------------------------------------- MCP doors


#: Every MCP tool with a ``work_ref`` argument: the function that reads it,
#: and the words its source must hold (the shared function or a model that
#: uses the shared type).
def _mcp_doors() -> dict[str, tuple[Callable[..., Any], tuple[str, ...]]]:
    from meho_backplane.mcp.tools import agent_runs as agent_runs_tools
    from meho_backplane.mcp.tools import approvals as approvals_tools
    from meho_backplane.mcp.tools import operations as operations_tools
    from meho_backplane.mcp.tools import runbook_runs as runbook_tools
    from meho_backplane.mcp.tools import scheduler as scheduler_tools

    shared = ("normalize_work_ref", "work_ref_argument")
    return {
        "call_operation": (
            operations_tools._clean_work_ref_argument,
            ("normalize_work_ref_input",),
        ),
        "meho_agents_list_runs": (agent_runs_tools._list_runs_handler, shared),
        "meho_agents_run": (agent_runs_tools._run_handler, shared),
        "meho_approvals_list": (approvals_tools._list_handler, shared),
        "meho_broadcast_announce": (mcp_broadcast._parse_announce_claims, shared),
        "meho_broadcast_recent": (mcp_broadcast._extract_filter, shared),
        "meho_broadcast_watch": (mcp_broadcast._extract_filter, shared),
        "meho_runbook_list_runs": (runbook_tools._list_runs_handler, ("ListRunsFilter",)),
        "meho_runbook_start": (runbook_tools._start_handler, ("StartRunRequest",)),
        "meho_scheduler_create": (scheduler_tools._create_handler, ("ScheduledTriggerCreate",)),
        "meho_scheduler_list": (scheduler_tools._list_handler, shared),
    }


def _schema_has_work_ref(schema: Any) -> bool:
    if isinstance(schema, dict):
        return any(
            key == "work_ref" or _schema_has_work_ref(value) for key, value in schema.items()
        )
    if isinstance(schema, list):
        return any(_schema_has_work_ref(item) for item in schema)
    return False


def test_every_mcp_work_ref_argument_is_read_with_the_shared_rule() -> None:
    mcp_registry.eager_import_mcp_modules()
    tools_with_work_ref = set()
    for name in mcp_registry.registered_tool_names():
        entry = mcp_registry.get_tool(name)
        assert entry is not None
        if _schema_has_work_ref(entry[0].inputSchema):
            tools_with_work_ref.add(name)
    doors = _mcp_doors()
    assert tools_with_work_ref == set(doors), (
        "A tool with a work_ref argument is not on the list (or a listed tool lost it). "
        "Read the argument with work_ref_argument or normalize_work_ref, then update "
        f"_mcp_doors. Not listed: {sorted(tools_with_work_ref - set(doors))}. "
        f"Listed without the argument: {sorted(set(doors) - tools_with_work_ref)}."
    )
    for name, (reader, words) in doors.items():
        source = inspect.getsource(reader)
        assert any(word in source for word in words), (
            f"{name}: {reader.__name__} does not use the shared rule ({' or '.join(words)})"
        )


# ---------------------------------------------------------------- filters: REST


def _filter_app() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    for module in (agent_runs, approvals, audit, audit_counts, broadcast, runbook_runs, scheduler):
        app.include_router(module.router)

    async def _operator() -> Operator:
        return Operator(
            sub="op-rest",
            name="Rest Caller",
            email=None,
            raw_jwt="<test-jwt>",
            tenant_id=uuid.uuid4(),
            tenant_role=TenantRole.TENANT_ADMIN,
        )

    app.dependency_overrides[verify_jwt_and_bind] = _operator
    return TestClient(app, raise_server_exceptions=False)


_BAD_FILTERS = [
    ("blank", "%20%20", "must not be empty"),
    ("invisible", quote("CHG-hidden" + _ZWSP + "7"), "control or invisible characters"),
    ("too long", "hidden" + "a" * 260, "at most 256 characters"),
]


@pytest.mark.parametrize(("path", "name"), _REST_FILTERS)
@pytest.mark.parametrize(("label", "raw", "words"), _BAD_FILTERS)
def test_a_bad_rest_filter_is_refused_with_the_rule(
    path: str, name: str, label: str, raw: str, words: str
) -> None:
    response = _filter_app().get(f"{path}?{name}={raw}")
    assert response.status_code == 422, (label, response.text)
    (error,) = response.json()["detail"]
    assert error["loc"] == ["query", name]
    assert words in error["msg"]
    assert "input" not in error
    assert "hidden" not in response.text


#: Values that break the length limit only before cleaning: they must pass.
_LONG_ONLY_BEFORE_CLEANING = [
    ("spaces around 256 characters", " " + "a" * 256 + " "),
    ("130 decomposed letters", unicodedata.normalize("NFD", "\u00c4" * 130)),
]


def test_the_openapi_file_has_no_length_limit_before_cleaning() -> None:
    schema = _filter_app().app.openapi()  # type: ignore[attr-defined]
    for path, name in _REST_FILTERS:
        (param,) = [p for p in schema["paths"][path]["get"]["parameters"] if p["name"] == name]
        (string,) = [s for s in param["schema"]["anyOf"] if s.get("type") == "string"]
        assert "maxLength" not in string, path
    for model in ("AuditQueryRequest", "AuditClassCountsRequest"):
        field = schema["components"]["schemas"][model]["properties"]["work_ref"]
        (string,) = [s for s in field["anyOf"] if s.get("type") == "string"]
        assert "maxLength" not in string, model


@pytest.mark.parametrize(("path", "name"), _REST_FILTERS)
@pytest.mark.parametrize(("label", "raw"), _LONG_ONLY_BEFORE_CLEANING)
def test_a_rest_filter_checks_the_length_after_cleaning(
    path: str, name: str, label: str, raw: str
) -> None:
    response = _filter_app().get(path, params={name: raw})
    assert response.status_code != 422, (label, response.text)


@pytest.mark.parametrize(("label", "raw"), _LONG_ONLY_BEFORE_CLEANING)
def test_a_model_filter_checks_the_length_after_cleaning_and_keeps_the_text(
    label: str, raw: str
) -> None:
    from meho_backplane.runbooks.runs_schemas import ListRunsFilter

    assert ListRunsFilter(work_ref=raw).work_ref == raw, label
    assert audit_models.AuditQueryRequest(work_ref=raw).to_filters().work_ref == raw, label
    assert work_ref_argument(raw) == raw, label


@pytest.mark.parametrize(("label", "raw"), _LONG_ONLY_BEFORE_CLEANING)
def test_an_audit_path_filter_checks_the_length_after_cleaning(label: str, raw: str) -> None:
    response = _filter_app().get(f"/api/v1/audit/by-work-ref/{quote(raw)}")
    assert response.status_code != 422, (label, response.text)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("meho_scheduler_list", lambda ref: {"work_ref": ref}),
        ("meho_broadcast_recent", lambda ref: {"filter": {"work_ref": ref}}),
        ("meho_broadcast_watch", lambda ref: {"cursor": "c1", "filter": {"work_ref": ref}}),
    ],
)
@pytest.mark.parametrize(("label", "raw"), _LONG_ONLY_BEFORE_CLEANING)
def test_an_mcp_filter_schema_checks_the_length_after_cleaning(
    tool: str, arguments: Callable[[str], dict[str, Any]], label: str, raw: str
) -> None:
    mcp_registry.eager_import_mcp_modules()
    entry = mcp_registry.get_tool(tool)
    assert entry is not None
    jsonschema.validate(arguments(raw), entry[0].inputSchema)


@pytest.mark.parametrize(("label", "raw", "words"), _BAD_FILTERS)
def test_a_bad_audit_path_filter_is_refused_with_the_rule(label: str, raw: str, words: str) -> None:
    response = _filter_app().get(f"/api/v1/audit/by-work-ref/{raw}")
    assert response.status_code == 422, (label, response.text)
    (error,) = response.json()["detail"]
    assert words in error["msg"]
    assert "hidden" not in response.text


@pytest.mark.parametrize("path", ["/api/v1/audit/query", "/api/v1/audit/class-counts"])
def test_a_bad_audit_body_filter_is_refused_with_the_rule(path: str) -> None:
    response = _filter_app().post(path, json={"work_ref": "CHG-hidden" + _ZWSP + "7"})
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "work_ref"]
    assert "control or invisible characters" in error["msg"]
    assert "hidden" not in response.text


def test_a_422_with_a_second_error_does_not_show_the_refused_ticket_id() -> None:
    # A missing field carries the whole body as its input, with the ticket id.
    response = _filter_app().post(
        "/api/v1/runbooks/runs",
        json={"template_slug": "tpl", "work_ref": "CHG-hidden" + _ZWSP + "7"},
    )
    assert response.status_code == 422, response.text
    errors = {tuple(error["loc"]): error for error in response.json()["detail"]}
    assert errors[("body", "target")]["type"] == "missing"
    assert "input" not in errors[("body", "target")]
    assert "control or invisible characters" in errors[("body", "work_ref")]["msg"]
    assert "hidden" not in response.text


def test_a_422_without_a_refused_ticket_id_has_no_input() -> None:
    response = _filter_app().post(
        "/api/v1/runbooks/runs", json={"template_slug": "tpl", "work_ref": "CHG-7"}
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert "input" not in error
    assert "CHG-7" not in response.text


def test_the_audit_body_filter_keeps_its_text_for_the_search() -> None:
    body = audit_models.AuditQueryRequest(work_ref="  CHG1 ")
    assert body.to_filters().work_ref == "  CHG1 "


def test_the_runbook_runs_filter_keeps_its_text_for_the_search() -> None:
    from meho_backplane.runbooks.runs_schemas import ListRunsFilter

    assert ListRunsFilter(work_ref=" CHG1 ").work_ref == " CHG1 "


# ---------------------------------------------------------------- filters: MCP


def test_the_mcp_filter_argument_is_checked_or_refused() -> None:
    assert work_ref_argument(None) is None
    assert work_ref_argument("  CHG1 ") == "  CHG1 "
    for bad in ("", "  ", "CHG" + _ZWSP + "1", "a" * 257):
        with pytest.raises(McpInvalidParamsError):
            work_ref_argument(bad)
    with pytest.raises(McpInvalidParamsError, match="must be a string"):
        work_ref_argument(7)


def test_the_mcp_filter_error_names_the_argument_and_not_the_value() -> None:
    with pytest.raises(McpInvalidParamsError) as caught:
        work_ref_argument("hidden" + _ZWSP, name="filter.work_ref")
    message = str(caught.value)
    assert message.startswith("filter.work_ref must not contain")
    assert "U+200B" in message
    assert "hidden" not in message


def _admin_operator() -> Operator:
    return Operator(
        sub="op-mcp",
        name="Mcp Caller",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _model_doors() -> list[tuple[str, Callable[..., Any], dict[str, Any]]]:
    """The MCP tools that read ``work_ref`` through a pydantic model."""
    from meho_backplane.mcp.tools import runbook_runs as runbook_tools
    from meho_backplane.mcp.tools import scheduler as scheduler_tools

    return [
        ("meho_runbook_list_runs", runbook_tools._list_runs_handler, {}),
        (
            "meho_runbook_start",
            runbook_tools._start_handler,
            {"template_slug": "tpl", "target": "t1"},
        ),
        (
            "meho_scheduler_create",
            scheduler_tools._create_handler,
            {"name": "n", "kind": "cron", "cron_expr": "0 * * * *", "agent_name": "a"},
        ),
    ]


@pytest.mark.parametrize(("tool", "handler", "arguments"), _model_doors())
@pytest.mark.parametrize(
    "bad", ["CHG-hidden" + _ZWSP + "7", "CHG-hidden\x007", "hidden" + "a" * 260]
)
async def test_an_mcp_model_error_does_not_show_the_refused_ticket_id(
    tool: str, handler: Callable[..., Any], arguments: dict[str, Any], bad: str
) -> None:
    with pytest.raises(McpInvalidParamsError) as caught:
        await handler(_admin_operator(), {**arguments, "work_ref": bad})
    message = str(caught.value)
    assert "work_ref" in message, (tool, message)
    assert "hidden" not in message, (tool, message)
    assert "input_value" not in message, (tool, message)


def test_the_mcp_model_error_text_names_the_field_and_the_rule() -> None:
    from pydantic import ValidationError

    from meho_backplane.input_refusal import refusal_text
    from meho_backplane.runbooks.runs_schemas import ListRunsFilter

    with pytest.raises(ValidationError) as caught:
        ListRunsFilter.model_validate({"work_ref": "CHG-hidden" + _ZWSP})
    text = refusal_text(caught.value)
    assert text.startswith("work_ref: ")
    assert "control or invisible characters (found U+200B)" in text
    assert text.endswith("(value_error)")
    assert "hidden" not in text


async def test_an_mcp_tool_error_without_a_ticket_id_error_names_no_input() -> None:
    from meho_backplane.mcp.tools import runbook_runs as runbook_tools
    from meho_backplane.mcp.tools import scheduler as scheduler_tools

    create = {"name": "n", "kind": "zz-hidden", "agent_name": "a", "work_ref": "CHG-7"}
    with pytest.raises(McpInvalidParamsError) as caught:
        await scheduler_tools._create_handler(_admin_operator(), create)
    assert str(caught.value).startswith("invalid arguments: kind: Input should be")
    assert "(enum)" in str(caught.value)
    assert "zz-hidden" not in str(caught.value)

    listing = {"status": "zz-hidden", "work_ref": "CHG-7"}
    with pytest.raises(McpInvalidParamsError) as caught:
        await runbook_tools._list_runs_handler(_admin_operator(), listing)
    assert "status: " in str(caught.value)
    assert "zz-hidden" not in str(caught.value)
    assert "input_value" not in str(caught.value)


def test_the_broadcast_history_filter_is_checked_or_refused() -> None:
    assert mcp_broadcast._extract_filter({"filter": {"work_ref": " CHG1 "}}).work_ref == " CHG1 "
    assert mcp_broadcast._extract_filter({}).work_ref is None
    with pytest.raises(McpInvalidParamsError, match=r"filter\.work_ref"):
        mcp_broadcast._extract_filter({"filter": {"work_ref": "CHG\n1"}})

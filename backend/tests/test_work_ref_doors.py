# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A new door for a ticket id (work ref) cannot be added without the shared rule.

The test walks every REST request model field, every REST query, path and
header parameter, and every MCP tool argument named ``work_ref``. It fails
when one of them neither uses the shared type or function nor is on a short
list with a reason. The search filters get a request test of their own: a bad
filter value is refused with the rule and without the refused text.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import APIRouter, FastAPI
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
from meho_backplane.work_ref import normalize_work_ref_input

_ZWSP = chr(0x200B)

# ---------------------------------------------------------------- REST doors

#: Parameters that read a ticket id without the shared type. Each has a
#: reason, and the test fails when an entry no longer matches a route.
_REST_OTHER_DOORS: dict[tuple[str, str], str] = {
    ("/api/v1/agents/{name}/run", "meho_work_ref"): (
        "header: the audit middleware refuses a bad value first, and "
        "_header_work_ref decodes it with decode_work_ref_header"
    ),
    ("/api/v1/agents/{name}/run/events", "meho_work_ref"): (
        "header: the audit middleware refuses a bad value first, and "
        "_header_work_ref decodes it with decode_work_ref_header"
    ),
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
        if isinstance(item, BeforeValidator) and item.func is normalize_work_ref_input:
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


def _rest_doors() -> tuple[list[tuple[str, str, bool]], list[tuple[str, bool]]]:
    """Return the parameters and the request model fields named ``work_ref``.

    Parameters are ``(route path, name, uses the shared type)``; model fields
    are ``("<Module>.<Model>.<field>", uses the shared type)``.
    """
    params: list[tuple[str, str, bool]] = []
    fields: dict[str, bool] = {}

    def walk(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            if "work_ref" in name:
                key = f"{model.__module__}.{model.__name__}.{name}"
                fields[key] = _uses_shared_type(info.annotation, tuple(info.metadata))
            for nested in _models_in(info.annotation):
                walk(nested, seen)

    seen_models: set[type[BaseModel]] = set()
    for router in _api_routers():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            dependant = route.dependant
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
                                route.path,
                                param.name,
                                _uses_shared_type(info.annotation, tuple(info.metadata)),
                            )
                        )
                    for model in _models_in(info.annotation):
                        walk(model, seen_models)
    return params, sorted(fields.items())


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


def test_the_audit_body_filter_is_cleaned() -> None:
    body = audit_models.AuditQueryRequest(work_ref="  CHG1 ")
    assert body.work_ref == "CHG1"
    assert body.to_filters().work_ref == "CHG1"


def test_the_runbook_runs_filter_is_cleaned() -> None:
    from meho_backplane.runbooks.runs_schemas import ListRunsFilter

    assert ListRunsFilter(work_ref=" CHG1 ").work_ref == "CHG1"


# ---------------------------------------------------------------- filters: MCP


def test_the_mcp_filter_argument_is_cleaned_or_refused() -> None:
    assert work_ref_argument(None) is None
    assert work_ref_argument("  CHG1 ") == "CHG1"
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


def test_the_broadcast_history_filter_is_cleaned_or_refused() -> None:
    assert mcp_broadcast._extract_filter({"filter": {"work_ref": " CHG1 "}}).work_ref == "CHG1"
    assert mcp_broadcast._extract_filter({}).work_ref is None
    with pytest.raises(McpInvalidParamsError, match=r"filter\.work_ref"):
        mcp_broadcast._extract_filter({"filter": {"work_ref": "CHG\n1"}})

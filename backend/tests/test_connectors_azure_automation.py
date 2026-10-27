# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure Automation reads (group ``automation``).

Azure Resource Manager and Microsoft's login service are mocked with respx.
The answers are shaped like the examples in Azure's pinned API description
file (Microsoft.Automation 2024-10-23), with made-up names and ids. The tests
run the real paging helper, the token cache, the real dispatcher and the real
result reduction with the real result store on a fake Valkey.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_VERSION,
    AzureConnector,
    _paging,
    _paths,
    automation_reads,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.automation import OPS, WHEN_TO_USE, AutomationOps
from meho_backplane.connectors.azure.automation_reads import (
    AutomationParameterError,
    account_names,
    job_filter,
    runbooks_by_schedule,
    schedule_row,
)
from meho_backplane.connectors.azure.automation_schemas import (
    ACCOUNT_LIST_RESPONSE,
    JOB_LIST_RESPONSE,
    RUNBOOK_LIST_RESPONSE,
    SCHEDULE_LIST_RESPONSE,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import run_result_query
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    SUBSCRIPTION_A,
    TENANT_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

SUB = SUBSCRIPTION_A
RG = "rg-automation-example"
ACCOUNT = "example-automation"
WHEN = "2026-10-26T08:00:00.123+00:00"

#: The clock of the job reads. Its fraction of a second is cut off.
NOW = datetime(2026, 10, 27, 9, 30, 15, 500000, tzinfo=UTC)

ACCOUNTS_PATH = f"/subscriptions/{SUB}/providers/Microsoft.Automation/automationAccounts"
ACCOUNT_PATH = (
    f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Automation/"
    f"automationAccounts/{ACCOUNT}"
)
ACCOUNT_PARAMS = {"resource_group": RG, "account": ACCOUNT}

#: Values that must never leave MEHO. Azure sends them in the answers below.
CONTENT_LINK = "https://example.invalid/content-link-must-not-leave.ps1"
DEFAULT_VALUE = "runbook-default-value-must-not-leave"
SCHEDULE_PARAMETER = "job-schedule-parameter-must-not-leave"
HYBRID_URL = "https://hybrid-url-must-not-leave.example.invalid/"


# ----------------------------------------------------------------------
# Answers shaped like the spec examples, with made-up values
# ----------------------------------------------------------------------


def _guid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _account(name: str = ACCOUNT, rg: str = RG) -> dict[str, Any]:
    """One AutomationAccount, as AutomationAccount_List returns it."""
    return {
        "name": name,
        "type": "Microsoft.Automation/AutomationAccounts",
        "id": f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Automation/"
        + f"automationAccounts/{name}",
        "location": "westeurope",
        "identity": {
            "type": "SystemAssigned",
            "principalId": "cccccccc-0000-0000-0000-00000000f001",
            "tenantId": TENANT_A,
        },
        "properties": {
            "sku": {"name": "Basic"},
            "state": "Ok",
            "creationTime": WHEN,
            "lastModifiedTime": WHEN,
            "lastModifiedBy": "example-user",
            "publicNetworkAccess": True,
            "disableLocalAuth": False,
            "automationHybridServiceUrl": HYBRID_URL,
        },
    }


def _runbook(name: str, state: str = "Published") -> dict[str, Any]:
    """One Runbook, as Runbook_ListByAutomationAccount returns it, with its content links."""
    return {
        "name": name,
        "type": "Microsoft.Automation/AutomationAccounts/Runbooks",
        "id": f"{ACCOUNT_PATH}/runbooks/{name}",
        "location": "westeurope",
        "properties": {
            "runbookType": "PowerShell72",
            "state": state,
            "logVerbose": False,
            "logProgress": False,
            "logActivityTrace": 0,
            "jobCount": 0,
            "creationTime": WHEN,
            "lastModifiedTime": WHEN,
            "lastModifiedBy": "example-user",
            "description": "Restarts the example service.",
            "publishContentLink": {"uri": CONTENT_LINK, "contentHash": {"value": "00"}},
            "parameters": {"Token": {"type": "String", "defaultValue": DEFAULT_VALUE}},
            "draft": {"inEdit": False, "draftContentLink": {"uri": CONTENT_LINK}},
        },
    }


def _job(n: int, status: str = "Completed", runbook: str = "rb-restart") -> dict[str, Any]:
    """One JobCollectionItem, as Job_ListByAutomationAccount returns it."""
    return {
        "name": _guid(n),
        "type": "Microsoft.Automation/AutomationAccounts/Jobs",
        "id": f"{ACCOUNT_PATH}/jobs/{_guid(n)}",
        "properties": {
            "jobId": _guid(n),
            "runbook": {"name": runbook},
            "status": status,
            "creationTime": WHEN,
            "startTime": WHEN,
            "endTime": None if status == "Running" else WHEN,
            "lastModifiedTime": WHEN,
            "provisioningState": "Succeeded",
            "startedBy": "example-user",
            "runOn": None,
            "jobRuntimeEnvironment": {"runtimeEnvironmentName": "example-environment"},
        },
    }


def _schedule(
    name: str,
    *,
    frequency: str = "Hour",
    enabled: bool = True,
    advanced: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One Schedule, as Schedule_ListByAutomationAccount returns it."""
    properties: dict[str, Any] = {
        "description": "Starts the example runbook.",
        "creationTime": WHEN,
        "expiryTime": "9999-12-31T23:59:00+00:00",
        "expiryTimeOffsetMinutes": 0,
        "frequency": frequency,
        "interval": 1,
        "isEnabled": enabled,
        "lastModifiedTime": WHEN,
        "nextRun": "2026-10-27T10:00:00+00:00",
        "nextRunOffsetMinutes": 0,
        "startTime": "2026-10-01T10:00:00+00:00",
        "startTimeOffsetMinutes": 0,
        "timeZone": "UTC",
    }
    if advanced is not None:
        properties["advancedSchedule"] = advanced
    return {"name": name, "id": f"{ACCOUNT_PATH}/schedules/{name}", "properties": properties}


def _job_schedule(n: int, schedule: str, runbook: str) -> dict[str, Any]:
    """One JobSchedule, as JobSchedule_ListByAutomationAccount returns it, with a parameter."""
    return {
        "id": f"{ACCOUNT_PATH}/jobSchedules/{_guid(n)}",
        "properties": {
            "jobScheduleId": _guid(n),
            "parameters": {"Token": SCHEDULE_PARAMETER},
            "runOn": None,
            "runbook": {"name": runbook},
            "schedule": {"name": schedule},
        },
    }


def _ok(values: list[dict[str, Any]], next_link: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"value": values}
    if next_link is not None:
        body["nextLink"] = next_link
    return httpx.Response(200, json=body)


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "Example message."}})


def _route(mock: respx.MockRouter, path: str) -> respx.Route:
    return mock.get(host=ARM_HOST, path=path)


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _row_fields(response: dict[str, Any]) -> set[str]:
    return set(response["properties"]["value"]["items"]["properties"])


# ----------------------------------------------------------------------
# No path that returns secrets
# ----------------------------------------------------------------------

#: The ends of the Azure Automation paths that return runbook code, job
#: output or streams, credentials or variables. A job's runbook code is at
#: ``.../jobs/{job}/runbookContent``, which ends in "content" too.
_SECRET_ENDINGS = ("content", "output", "streams", "credentials", "variables")


def _secret_parts(path: str) -> list[str]:
    """The fixed parts of *path* (not the ``{...}`` ones) that end in a secret ending."""
    parts = path.partition("?")[0].split("/")
    return [
        part
        for part in parts
        if part and not part.startswith("{") and part.lower().endswith(_SECRET_ENDINGS)
    ]


@pytest.mark.parametrize(
    "path",
    [
        f"{ACCOUNT_PATH}/runbooks/{{runbook}}/content",
        f"{ACCOUNT_PATH}/runbooks/{{runbook}}/draft/content",
        f"{ACCOUNT_PATH}/jobs/{{job}}/output",
        f"{ACCOUNT_PATH}/jobs/{{job}}/streams",
        f"{ACCOUNT_PATH}/jobs/{{job}}/streams/{{stream}}",
        f"{ACCOUNT_PATH}/jobs/{{job}}/runbookContent",
        f"{ACCOUNT_PATH}/credentials",
        f"{ACCOUNT_PATH}/credentials/{{credential}}",
        f"{ACCOUNT_PATH}/variables/{{variable}}",
    ],
)
def test_the_secret_path_check_catches_each_secret_read(path: str) -> None:
    assert _secret_parts(path)


def test_no_azure_path_reads_code_output_streams_credentials_or_variables() -> None:
    """Every path in ``_paths.py``, of every family, stays away from secret reads.

    Stricter than "the path ends in /content": a fixed part anywhere in the
    path counts, so ``.../streams/{id}`` and ``.../runbookContent`` fail too.
    """
    paths = {
        name: value
        for name, value in vars(_paths).items()
        if isinstance(value, str) and value.startswith("/")
    }
    assert "AUTOMATION_JOBS_PATH" in paths
    assert {name: _secret_parts(value) for name, value in paths.items()} == {
        name: [] for name in paths
    }


# ----------------------------------------------------------------------
# Parameters, filter and rows (no calls)
# ----------------------------------------------------------------------


def test_the_job_filter_has_the_form_microsoft_sends() -> None:
    since = datetime(2026, 10, 20, 9, 30, 15, tzinfo=UTC)
    in_berlin = datetime(2026, 10, 20, 11, 30, 15, tzinfo=timezone(timedelta(hours=2)))

    assert job_filter(since, None) == "properties/startTime ge 2026-10-20T09:30:15.0000000Z"
    assert job_filter(in_berlin, None) == "properties/startTime ge 2026-10-20T09:30:15.0000000Z"
    assert job_filter(since, "Failed") == (
        "properties/startTime ge 2026-10-20T09:30:15.0000000Z and properties/status eq 'Failed'"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resource_group", f"{RG}\n"),
        ("resource_group", "rg/other"),
        ("resource_group", "rg-example."),
        ("resource_group", ""),
        ("resource_group", None),
        ("account", f"{ACCOUNT}\n"),
        ("account", "../other"),
        ("account", "short"),
        ("account", "1-example"),
        ("account", "example-"),
        ("account", "example_automation"),
        ("account", "a" * 51),
        ("account", 12345678),
    ],
)
def test_a_name_must_match_the_azure_rule_in_full(field: str, value: object) -> None:
    with pytest.raises(AutomationParameterError) as caught:
        account_names({**ACCOUNT_PARAMS, field: value}, target_name="azure-sub-a")

    assert f"'{field}'" in str(caught.value)
    assert caught.value.remediation
    assert "../other" not in str(caught.value)


def test_good_names_pass_unchanged() -> None:
    names = {"resource_group": "RG_Example.(1)", "account": "Example-Automation-01"}

    assert account_names(names, target_name="azure-sub-a") == names


def test_the_links_hold_only_names_and_match_schedules_without_case() -> None:
    links = runbooks_by_schedule(
        [
            _job_schedule(1, "Nightly", "rb-b"),
            _job_schedule(2, "NIGHTLY", "rb-a"),
            _job_schedule(3, "Weekly", "rb-a"),
            {"properties": {"schedule": {"name": "No-runbook"}}},
            {"properties": "not-an-object"},
        ]
    )

    assert links == {"nightly": ["rb-a", "rb-b"], "weekly": ["rb-a"]}
    assert SCHEDULE_PARAMETER not in json.dumps(links)


def test_a_schedule_row_reads_the_days_it_runs_on() -> None:
    weekly = _schedule("Weekly", frequency="Week", advanced={"weekDays": ["Monday", 3, "Friday"]})
    monthly = _schedule(
        "Monthly",
        frequency="Month",
        advanced={
            "monthDays": [1, 15, "x", True],
            "monthlyOccurrences": [
                {"occurrence": 1, "day": "Monday"},
                {"occurrence": -1, "day": "Friday"},
                {"day": "Sunday"},
                "not-an-object",
            ],
        },
    )

    week_row = schedule_row(weekly, {"weekly": ["rb-a"]})
    month_row = schedule_row(monthly, None)

    assert week_row["week_days"] == ["Monday", "Friday"]
    assert week_row["runbooks"] == ["rb-a"]
    assert month_row["month_days"] == [1, 15]
    assert month_row["monthly_occurrences"] == ["1 Monday", "-1 Friday"]
    assert month_row["runbooks"] is None
    assert schedule_row(_schedule("Alone"), {})["runbooks"] == []
    assert schedule_row({"name": 7, "properties": []}, {}) == {
        **dict.fromkeys(_row_fields(SCHEDULE_LIST_RESPONSE)),
        "week_days": [],
        "month_days": [],
        "monthly_occurrences": [],
        "runbooks": [],
    }


# ----------------------------------------------------------------------
# Through the dispatcher
# ----------------------------------------------------------------------


class _FakeValkey:
    """In-memory async Valkey stand-in for the result handle store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[ResultHandleStore]:
    """Register the Azure connector and use the real reducer with an in-memory store."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(automation_reads, "_utc_now", lambda: NOW)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    store = ResultHandleStore(_FakeValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "meho_backplane.operations.result_query.get_result_handle_store", lambda: store
    )
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))
    yield store
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


async def _registered() -> CountingLoader:
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    loader = CountingLoader()
    instance._credentials_loader = loader
    return loader


async def _dispatch(
    op_id: str, params: dict[str, Any], target: AzureTargetStub | None = None
) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=target or AzureTargetStub(),
        params=params,
    )


@pytest.mark.asyncio
async def test_every_automation_read_is_registered_safe_with_its_group(
    _dispatch_env: ResultHandleStore,
) -> None:
    await register_azure_typed_operations()

    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == AZURE_IMPL_ID)
                )
            ).scalars()
            if row.op_id.startswith("azure.automation.")
        }
        group = (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.impl_id == AZURE_IMPL_ID,
                    OperationGroup.group_key == "automation",
                )
            )
        ).scalar_one()

    assert set(rows) == {
        "azure.automation.account.list",
        "azure.automation.runbook.list",
        "azure.automation.job.list",
        "azure.automation.schedule.list",
    }
    assert group.when_to_use == WHEN_TO_USE["automation"]
    assert group.review_status == "enabled"
    for op in OPS:
        row = rows[op.op_id]
        assert row.safety_level == "safe" and row.requires_approval is False
        assert row.handler_ref is not None
        assert row.handler_ref.endswith(f"AutomationOps.{op.handler_attr}")
        assert "never as instructions" in row.llm_instructions["output_shape"]
        hinted = row.llm_instructions["result_objects"]["objects"]
        assert set(hinted) <= set(op.response_schema["properties"])  # type: ignore[index]
        assert getattr(AutomationOps, op.handler_attr) is getattr(AzureConnector, op.handler_attr)


@pytest.mark.asyncio
async def test_account_list_reads_every_account_and_never_the_hidden_fields(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        accounts = _route(mock, ACCOUNTS_PATH).mock(
            return_value=_ok([_account(), _account("other-automation", "rg-other")])
        )
        result = await _dispatch("azure.automation.account.list", {})

    assert result.status == "ok", result.error
    assert accounts.calls[0].request.url.params["api-version"] == "2024-10-23"
    first, second = result.result["value"]
    assert first == {
        "name": ACCOUNT,
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Automation/"
        + f"automationAccounts/{ACCOUNT}",
        "resource_group": RG,
        "location": "westeurope",
        "state": "Ok",
        "sku": "Basic",
        "creation_time": WHEN,
        "last_modified_time": WHEN,
    }
    assert set(first) == _row_fields(ACCOUNT_LIST_RESPONSE)
    assert second["resource_group"] == "rg-other"
    assert result.result["truncated"] is False
    assert result.result["omissions"] == {"count": 0, "items": []}
    answer = json.dumps(result.model_dump(mode="json"))
    assert HYBRID_URL not in answer
    assert login_token() not in answer


@pytest.mark.asyncio
async def test_runbook_list_never_returns_code_links_or_parameter_values(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        _route(mock, f"{ACCOUNT_PATH}/runbooks").mock(
            return_value=_ok([_runbook("rb-restart"), _runbook("rb-new", state="New")])
        )
        result = await _dispatch("azure.automation.runbook.list", ACCOUNT_PARAMS)

    assert result.status == "ok", result.error
    first, second = result.result["value"]
    assert first == {
        "name": "rb-restart",
        "runbook_type": "PowerShell72",
        "state": "Published",
        "description": "Restarts the example service.",
        "creation_time": WHEN,
        "last_modified_time": WHEN,
        "last_modified_by": "example-user",
    }
    assert set(first) == _row_fields(RUNBOOK_LIST_RESPONSE)
    assert second["state"] == "New"
    answer = json.dumps(result.model_dump(mode="json"))
    assert "content-link-must-not-leave" not in answer
    assert DEFAULT_VALUE not in answer


@pytest.mark.asyncio
async def test_job_list_asks_azure_for_the_window_and_the_status(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        jobs = _route(mock, f"{ACCOUNT_PATH}/jobs").mock(
            return_value=_ok([_job(1, "Failed"), _job(2, "Failed", "rb-backup")])
        )
        result = await _dispatch(
            "azure.automation.job.list", {**ACCOUNT_PARAMS, "days": 3, "status": "Failed"}
        )

    assert result.status == "ok", result.error
    sent = jobs.calls[0].request.url.params
    assert sent["api-version"] == "2024-10-23"
    assert sent["$filter"] == (
        "properties/startTime ge 2026-10-24T09:30:15.0000000Z and properties/status eq 'Failed'"
    )
    assert result.result["window"] == {
        "days": 3,
        "from": "2026-10-24T09:30:15Z",
        "to": "2026-10-27T09:30:15Z",
    }
    assert result.result["status_filter"] == "Failed"
    first, second = result.result["value"]
    assert first == {
        "name": _guid(1),
        "job_id": _guid(1),
        "runbook": "rb-restart",
        "status": "Failed",
        "creation_time": WHEN,
        "start_time": WHEN,
        "end_time": WHEN,
        "started_by": "example-user",
        "run_on": None,
    }
    assert set(first) == _row_fields(JOB_LIST_RESPONSE)
    assert second["runbook"] == "rb-backup"


@pytest.mark.asyncio
async def test_job_list_reads_seven_days_of_every_state_by_default(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        jobs = _route(mock, f"{ACCOUNT_PATH}/jobs").mock(return_value=_ok([_job(1, "Running")]))
        result = await _dispatch("azure.automation.job.list", ACCOUNT_PARAMS)

    assert result.status == "ok", result.error
    assert jobs.calls[0].request.url.params["$filter"] == (
        "properties/startTime ge 2026-10-20T09:30:15.0000000Z"
    )
    assert result.result["window"]["days"] == 7
    assert result.result["status_filter"] is None
    assert result.result["value"][0]["end_time"] is None


@pytest.mark.parametrize(
    "extra",
    [{"days": 31}, {"days": 0}, {"days": "7"}, {"status": "failed"}, {"status": "Unknown"}],
)
@pytest.mark.asyncio
async def test_job_list_refuses_a_bad_window_or_status_before_any_call(
    _dispatch_env: ResultHandleStore, extra: dict[str, Any]
) -> None:
    await _registered()

    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _dispatch("azure.automation.job.list", {**ACCOUNT_PARAMS, **extra})

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not login.called


@pytest.mark.asyncio
async def test_a_name_that_breaks_the_rule_is_refused_before_anything_is_sent(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        by_schema = await _dispatch(
            "azure.automation.schedule.list", {"resource_group": RG, "account": "../other"}
        )
        # A schema pattern lets a trailing line end through; the full match does not.
        by_body = await _dispatch(
            "azure.automation.runbook.list", {"resource_group": f"{RG}\n", "account": ACCOUNT}
        )

    assert by_schema.status == "error"
    assert by_schema.extras["error_code"] == "invalid_params"
    assert by_body.status == "error"
    assert by_body.extras["exception_class"] == "AutomationParameterError"
    assert "'resource_group'" in by_body.extras["exception_message"]
    assert by_body.extras["remediation"]
    assert not login.called


@pytest.mark.asyncio
async def test_a_missing_account_comes_back_as_not_found(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        _route(mock, f"{ACCOUNT_PATH}/runbooks").mock(return_value=_error(404, "NotFound"))
        result = await _dispatch("azure.automation.runbook.list", ACCOUNT_PARAMS)

    assert result.status == "not_found"
    assert result.extras["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_schedule_list_names_the_runbooks_each_schedule_starts(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()
    # Azure's own next-page link of this list names the port, like the spec example.
    next_link = f"https://{ARM_HOST}:443{ACCOUNT_PATH}/schedules?api-version=2024-10-23&$skip=100"

    with respx.mock() as mock:
        _login(mock)
        schedules = _route(mock, f"{ACCOUNT_PATH}/schedules").mock(
            side_effect=[
                _ok(
                    [
                        _schedule("Nightly"),
                        _schedule("Weekly", frequency="Week", advanced={"weekDays": ["Monday"]}),
                    ],
                    next_link,
                ),
                _ok([_schedule("Unused", enabled=False)]),
            ]
        )
        links = _route(mock, f"{ACCOUNT_PATH}/jobSchedules").mock(
            return_value=_ok(
                [
                    _job_schedule(1, "Nightly", "rb-restart"),
                    _job_schedule(2, "NIGHTLY", "rb-backup"),
                    _job_schedule(3, "Weekly", "rb-restart"),
                ]
            )
        )
        result = await _dispatch("azure.automation.schedule.list", ACCOUNT_PARAMS)

    assert result.status == "ok", result.error
    assert schedules.call_count == 2
    assert schedules.calls[1].request.url.params["$skip"] == "100"
    assert links.calls[0].request.url.params["api-version"] == "2024-10-23"
    rows = {row["name"]: row for row in result.result["value"]}
    assert rows["Nightly"] == {
        "name": "Nightly",
        "enabled": True,
        "frequency": "Hour",
        "interval": 1,
        "next_run": "2026-10-27T10:00:00+00:00",
        "start_time": "2026-10-01T10:00:00+00:00",
        "expiry_time": "9999-12-31T23:59:00+00:00",
        "time_zone": "UTC",
        "week_days": [],
        "month_days": [],
        "monthly_occurrences": [],
        "description": "Starts the example runbook.",
        "runbooks": ["rb-backup", "rb-restart"],
    }
    assert set(rows["Nightly"]) == _row_fields(SCHEDULE_LIST_RESPONSE)
    assert rows["Weekly"]["week_days"] == ["Monday"]
    assert rows["Weekly"]["runbooks"] == ["rb-restart"]
    assert rows["Unused"]["enabled"] is False
    assert rows["Unused"]["runbooks"] == []
    assert result.result["omissions"]["count"] == 0
    assert SCHEDULE_PARAMETER not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_schedule_list_still_answers_when_the_links_cannot_be_read(
    _dispatch_env: ResultHandleStore,
) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        _route(mock, f"{ACCOUNT_PATH}/schedules").mock(return_value=_ok([_schedule("Nightly")]))
        _route(mock, f"{ACCOUNT_PATH}/jobSchedules").mock(
            return_value=_error(403, "AuthorizationFailed")
        )
        result = await _dispatch("azure.automation.schedule.list", ACCOUNT_PARAMS)

    assert result.status == "ok", result.error
    assert [row["runbooks"] for row in result.result["value"]] == [None]
    assert result.result["truncated"] is True
    (item,) = result.result["omissions"]["items"]
    assert item["what"] == "runbook links"
    assert item["reason"] == "no_permission"
    assert "AuthorizationFailed" in item["detail"]
    assert "Example message." not in item["detail"]


@pytest.mark.asyncio
async def test_one_login_serves_several_reads(_dispatch_env: ResultHandleStore) -> None:
    loader = await _registered()
    target = AzureTargetStub()

    with respx.mock() as mock:
        login = _login(mock)
        _route(mock, ACCOUNTS_PATH).mock(return_value=_ok([_account()]))
        _route(mock, f"{ACCOUNT_PATH}/runbooks").mock(return_value=_ok([_runbook("rb-restart")]))
        first = await _dispatch("azure.automation.account.list", {}, target)
        second = await _dispatch("azure.automation.runbook.list", ACCOUNT_PARAMS, target)

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert login.call_count == 1
    assert loader.calls == 1


def _sixty_rows(mock: respx.MockRouter, op_id: str) -> dict[str, Any]:
    """Mock answers with 60 rows for *op_id*; return the params of the call."""
    many = range(60)
    if op_id == "azure.automation.account.list":
        _route(mock, ACCOUNTS_PATH).mock(
            return_value=_ok([_account(f"example-automation-{n:02d}") for n in many])
        )
        return {}
    if op_id == "azure.automation.runbook.list":
        _route(mock, f"{ACCOUNT_PATH}/runbooks").mock(
            return_value=_ok([_runbook(f"rb-{n:02d}") for n in many])
        )
        return ACCOUNT_PARAMS
    if op_id == "azure.automation.job.list":
        _route(mock, f"{ACCOUNT_PATH}/jobs").mock(
            return_value=_ok([_job(n, "Failed" if n % 4 == 0 else "Completed") for n in many])
        )
        return ACCOUNT_PARAMS
    _route(mock, f"{ACCOUNT_PATH}/schedules").mock(
        return_value=_ok([_schedule(f"schedule-{n:02d}") for n in many])
    )
    _route(mock, f"{ACCOUNT_PATH}/jobSchedules").mock(
        return_value=_error(403, "AuthorizationFailed")
    )
    return ACCOUNT_PARAMS


@pytest.mark.parametrize("op_id", [op.op_id for op in OPS])
@pytest.mark.asyncio
async def test_a_long_list_keeps_its_blocks_next_to_the_stored_result(
    _dispatch_env: ResultHandleStore, op_id: str
) -> None:
    """The real reducer stores the 60 rows and keeps every block inline and stored."""
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        params = _sixty_rows(mock, op_id)
        result = await _dispatch(op_id, params)

    assert result.status == "ok", result.error
    assert result.handle is not None, "60 rows should come back as a stored result"
    summary = result.result
    assert summary["row_count"] == 60
    expected_omissions = 1 if op_id == "azure.automation.schedule.list" else 0
    assert summary["truncated"] is (expected_omissions > 0)
    assert summary["omissions"]["count"] == expected_omissions
    handle_id = result.handle.handle_id
    stored = await run_result_query(operator(), handle_id, ResultQuerySpec(path="/omissions"))
    assert stored["value"]["count"] == expected_omissions
    if expected_omissions:
        assert summary["omissions"]["items"][0]["reason"] == "no_permission"
        assert stored["value"]["items"][0]["what"] == "runbook links"
    if op_id == "azure.automation.job.list":
        window = {"days": 7, "from": "2026-10-20T09:30:15Z", "to": "2026-10-27T09:30:15Z"}
        assert summary["window"] == window
        assert summary["status_filter"] is None
        stored_window = await run_result_query(
            operator(), handle_id, ResultQuerySpec(path="/window")
        )
        assert stored_window["value"] == window
        failed = await run_result_query(
            operator(),
            handle_id,
            ResultQuerySpec.model_validate(
                {"filter": [{"field": "status", "op": "=", "value": "Failed"}]}
            ),
        )
        assert len(failed["rows"]) == 15


@pytest.mark.asyncio
async def test_a_paging_limit_on_the_job_list_stays_next_to_the_stored_result(
    _dispatch_env: ResultHandleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paging limit cuts the jobs; the reason survives at the stored result."""
    ticks = iter(range(0, 10_000, 200))
    monkeypatch.setattr(_paging, "_monotonic", lambda: float(next(ticks)))
    await _registered()
    next_link = f"{ARM_BASE}{ACCOUNT_PATH}/jobs?api-version=2024-10-23&$skiptoken=2"

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _route(mock, f"{ACCOUNT_PATH}/jobs").mock(
            side_effect=[_ok([_job(n) for n in range(55)], next_link), _ok([_job(99)])]
        )
        result = await _dispatch("azure.automation.job.list", ACCOUNT_PARAMS)

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 55
    assert summary["truncated"] is True
    (item,) = summary["omissions"]["items"]
    assert item["what"] == "jobs"
    assert item["reason"] == "time_limit"
    assert summary["window"]["days"] == 7

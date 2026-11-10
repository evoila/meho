# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure Migrate writes: CSV import, groups and VM assessments.

Azure Resource Manager, the upload link and Microsoft's login service are
mocked with respx. All names, ids and links are made up. The tests run the
real connector, the token cache and the real retry rules of ``send``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog

from meho_backplane.connectors.azure import AzureConnector, _http, _polling, migrate_writes
from meho_backplane.connectors.azure._errors import (
    AzureForbiddenError,
    AzureRequestError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure.migrate import OPS, WHEN_TO_USE
from meho_backplane.connectors.azure.migrate_csv import MAX_CSV_ROWS, prepare_csv
from meho_backplane.connectors.azure.migrate_jobs import default_site_name, site_param
from meho_backplane.connectors.azure.migrate_write_ops import WRITE_OPS
from meho_backplane.connectors.azure.migrate_writes import MAX_MACHINES_PER_CALL
from meho_backplane.operations._validate import validate_params
from tests._azure_fixtures import (
    ARM_HOST,
    SUBSCRIPTION_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

SUB = SUBSCRIPTION_A
RG = "rg-migrate-example"
PROJECT = "example-assessment-project"
GROUP = "example-group"
ASSESSMENT = "example-assessment"
SITE = default_site_name(PROJECT)
JOB = "example-job-01"
BLOB_HOST = "example-import.blob.core.windows.net"
#: The made-up upload link. Its signature must never leave MEHO.
SIGNATURE = "MADEUPSIGNATUREVALUE"
LINK = f"https://{BLOB_HOST}/import-container/servers.csv?sv=2024-01-01&sig={SIGNATURE}"

RG_PATH = f"/subscriptions/{SUB}/resourceGroups/{RG}"
PROJECT_PATH = f"{RG_PATH}/providers/Microsoft.Migrate/assessmentProjects/{PROJECT}"
SITE_PATH = f"{RG_PATH}/providers/Microsoft.OffAzure/importSites/{SITE}"
JOBS_PATH = f"{SITE_PATH}/importJobs"
COLLECTOR_PATH = f"{PROJECT_PATH}/importcollectors/{SITE}-collector"
GROUP_PATH = f"{PROJECT_PATH}/groups/{GROUP}"
ASSESSMENT_PATH = f"{GROUP_PATH}/assessments/{ASSESSMENT}"

GOLDEN = Path(__file__).parent / "data" / "migration_azure_migrate_golden.csv"
CSV = GOLDEN.read_text(encoding="utf-8")
CSV_ROWS = 2

IMPORT_PARAMS = {"resource_group": RG, "project": PROJECT, "csv": CSV}
GROUP_PARAMS = {"resource_group": RG, "project": PROJECT, "group": GROUP}
ASSESSMENT_PARAMS = {
    **GROUP_PARAMS,
    "assessment": ASSESSMENT,
    "region": "westeurope",
    "sizing": "as_on_premises",
}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Polls do not sleep, and the clock moves 5 s per read.

    A poll that never finishes then runs out of wait time after a few
    rounds instead of looping on the real clock. Tests that need a
    different clock set their own.
    """
    clock = iter(range(0, 1_000_000, 5))
    monkeypatch.setattr(migrate_writes, "_monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(migrate_writes, "_sleep", AsyncMock())
    monkeypatch.setattr(_polling, "_sleep", AsyncMock())
    monkeypatch.setattr(_http, "_sleep", AsyncMock())
    yield


def _connector() -> AzureConnector:
    return AzureConnector(credentials_loader=CountingLoader())


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _arm(mock: respx.MockRouter, method: str, path: str) -> respx.Route:
    return mock.route(method=method, host=ARM_HOST, path=path)


def _error(status: int, code: str = "Example") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "Example message."}})


def _job(state: str, **props: Any) -> dict[str, Any]:
    return {
        "name": JOB,
        "id": f"{JOBS_PATH}/{JOB}",
        "status": "Running",
        "startTime": "2026-09-26T09:14:05Z",
        "properties": {"jobResult": state, "blobSasUri": LINK, **props},
    }


def _finished_job() -> dict[str, Any]:
    return _job(
        "CompletedWithErrors",
        numberOfMachinesImported=1,
        errorSummary={
            "errors": ["Row 3: the server name is empty."],
            "errorCount": 1,
            "warningCount": 0,
        },
    )


class _Import:
    """The routes of one import, so a test changes only what it needs."""

    def __init__(self, mock: respx.MockRouter, *, tags: dict[str, str] | None = None) -> None:
        self.login = _login(mock)
        self.project = _arm(mock, "GET", PROJECT_PATH).respond(200, json={"location": "westeurope"})
        if tags is None:
            self.site_get = _arm(mock, "GET", SITE_PATH).respond(404, json=_error(404).json())
        else:
            self.site_get = _arm(mock, "GET", SITE_PATH).respond(200, json={"tags": tags})
        self.site_put = _arm(mock, "PUT", SITE_PATH).respond(201, json={"name": SITE})
        self.site_patch = _arm(mock, "PATCH", SITE_PATH).respond(200, json={})
        self.collector = _arm(mock, "PUT", COLLECTOR_PATH).respond(200, json={})
        self.import_uri = _arm(mock, "POST", f"{SITE_PATH}/importUri").respond(
            200, json={"uri": LINK, "jobArmId": f"{JOBS_PATH}/{JOB}"}
        )
        self.upload = mock.put(host=BLOB_HOST, path="/import-container/servers.csv").respond(201)
        self.job = _arm(mock, "GET", f"{JOBS_PATH}/{JOB}").mock(
            side_effect=[
                httpx.Response(200, json=_job("InProgress")),
                httpx.Response(200, json=_finished_job()),
            ]
        )


def _tag(csv_text: str = CSV) -> str:
    return prepare_csv(csv_text).tag_key


# ----------------------------------------------------------------------
# The file
# ----------------------------------------------------------------------


def test_the_golden_file_is_a_good_import_file() -> None:
    prepared = prepare_csv(CSV)

    assert prepared.rows == CSV_ROWS
    assert prepared.content == CSV.encode("utf-8")
    assert len(prepared.sha256) == 64
    assert prepared.describe() == {
        "sha256": prepared.sha256,
        "bytes": len(prepared.content),
        "rows": CSV_ROWS,
    }


def test_a_row_count_does_not_count_blank_lines_or_the_header() -> None:
    text = "Server name,Cores,Memory (In MB),OS name\n\na,1,1,x\n\n\nb,2,2,y\n"

    assert prepare_csv(text).rows == 2


@pytest.mark.parametrize(
    "bad",
    [
        None,
        7,
        "",
        "   \n",
        "Server name,Cores,Memory (In MB),OS name\n",
        "Server name,Cores,OS name\na,1,x\n",
        "a,b,c\n1,2,3\n",
        "Server name,Cores,Memory (In MB),OS name\na,1,1,x\x00\n",
    ],
)
def test_a_bad_file_is_refused_before_anything_is_sent(bad: object) -> None:
    with pytest.raises(ValueError, match="csv"):
        prepare_csv(bad)


def test_a_file_over_the_row_limit_is_refused() -> None:
    header = "Server name,Cores,Memory (In MB),OS name\n"
    text = header + "a,1,1,x\n" * (MAX_CSV_ROWS + 1)

    with pytest.raises(ValueError, match="20,000"):
        prepare_csv(text)
    assert prepare_csv(header + "a,1,1,x\n" * MAX_CSV_ROWS).rows == MAX_CSV_ROWS


def test_a_file_over_the_size_limit_is_refused() -> None:
    text = "Server name,Cores,Memory (In MB),OS name\n" + "a" * (8 * 1024 * 1024)

    with pytest.raises(ValueError, match="bytes"):
        prepare_csv(text)


def test_the_default_site_name_follows_azures_rule_and_is_stable() -> None:
    for project in ("p", "Example Project (Prod)", "x" * 200, "ünï-çödé", "---"):
        name = default_site_name(project)
        assert (
            validate_params(
                {"type": "object", "properties": {"site": {"pattern": r"^[a-zA-Z0-9-]{3,24}$"}}},
                {"site": name},
            )
            == []
        )
        assert name == default_site_name(project)
    assert default_site_name("a") != default_site_name("b")


@pytest.mark.parametrize("bad", ["ab", "x" * 25, "a/b", "a b", "name\n", 7])
def test_a_bad_site_name_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="site must be"):
        site_param({"site": bad}, PROJECT)


# ----------------------------------------------------------------------
# The import
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_full_import_path_reports_the_job_and_the_bad_row() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.login.call_count == 1
    assert routes.site_put.call_count == 1
    assert json.loads(routes.site_put.calls.last.request.content) == {
        "location": "westeurope",
        "properties": {},
    }
    assert json.loads(routes.collector.calls.last.request.content) == {
        "properties": {"discoverySiteId": f"{SITE_PATH}"}
    }
    patch_body = json.loads(routes.site_patch.calls.last.request.content)
    assert patch_body == {"tags": {_tag(): JOB}}
    assert routes.upload.call_count == 1
    assert routes.upload.calls.last.request.content == CSV.encode("utf-8")
    assert result["outcome"] == "uploaded"
    assert result["job_name"] == JOB
    assert result["state"] == "CompletedWithErrors"
    assert result["done"] is True
    assert result["rows_read"] == CSV_ROWS
    assert result["rows_imported"] == 1
    assert result["error_count"] == 1
    assert result["bad_rows"] == [{"reason": "Row 3: the server name is empty."}]
    assert result["bad_rows_truncated"] is False
    assert result["csv"]["rows"] == CSV_ROWS
    assert result["poll_error"] is None


@pytest.mark.asyncio
async def test_the_upload_gets_no_azure_token() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    upload = routes.upload.calls.last.request
    assert "authorization" not in upload.headers
    assert upload.headers["x-ms-blob-type"] == "BlockBlob"
    # ...while the Resource Manager calls do carry it.
    assert routes.import_uri.calls.last.request.headers["authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_the_link_is_in_no_result_log_or_error() -> None:
    with structlog.testing.capture_logs() as logs, respx.mock(assert_all_called=False) as mock:
        _Import(mock)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert SIGNATURE not in json.dumps(result)
    assert BLOB_HOST not in json.dumps(result)
    assert SIGNATURE not in json.dumps(logs, default=str)


@pytest.mark.asyncio
async def test_an_existing_site_is_not_written_again() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={"owner": "example-team"})
        await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.site_put.call_count == 0
    patch_body = json.loads(routes.site_patch.calls.last.request.content)
    assert patch_body == {"tags": {"owner": "example-team", _tag(): JOB}}


@pytest.mark.asyncio
async def test_a_retry_after_a_lost_answer_reads_the_job_and_uploads_nothing() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): JOB})
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0
    assert routes.site_patch.call_count == 0
    assert result["outcome"] == "already_sent"
    assert result["job_name"] == JOB
    assert result["state"] == "CompletedWithErrors"


@pytest.mark.asyncio
async def test_a_lost_answer_after_the_upload_is_not_an_error_and_not_sent_twice() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.upload.mock(side_effect=httpx.ReadTimeout("lost"))
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "upload_unconfirmed"
    assert result["state"] == "CompletedWithErrors"
    assert SIGNATURE not in json.dumps(result)


@pytest.mark.asyncio
async def test_an_upload_answer_of_5xx_is_unconfirmed_and_is_not_sent_again() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.upload.respond(503)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "upload_unconfirmed"


@pytest.mark.asyncio
async def test_a_refused_upload_fails_with_a_text_that_has_no_link() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.upload.respond(403)
        with pytest.raises(AzureRequestError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    text = f"{caught.value} {caught.value.remediation}"
    assert SIGNATURE not in text
    assert BLOB_HOST not in text
    assert "403" in text


@pytest.mark.asyncio
async def test_when_the_record_cannot_be_written_nothing_is_uploaded() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_patch.respond(403, json=_error(403, "AuthorizationFailed").json())
        with pytest.raises(AzureForbiddenError):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.upload.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example.invalid/c/servers.csv?sig=x",
        f"http://{BLOB_HOST}/c/servers.csv?sig=x",
        f"https://user:pass@{BLOB_HOST}/c/servers.csv?sig=x",
        "https://blob.core.windows.net/c/servers.csv?sig=x",
        f"https://{BLOB_HOST}.evil.example.invalid/c/servers.csv?sig=x",
        None,
        "not a link",
    ],
)
async def test_a_link_that_is_not_a_blob_link_is_refused_before_anything_is_written(
    link: object,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.import_uri.respond(200, json={"uri": link, "jobArmId": f"{JOBS_PATH}/{JOB}"})
        with pytest.raises(AzureUrlRefusedError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert "sig=x" not in str(caught.value)
    assert routes.site_patch.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_job_without_a_usable_name_uploads_nothing() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.import_uri.respond(200, json={"uri": LINK, "jobArmId": "/x/y/bad name!"})
        with pytest.raises(AzureUrlRefusedError):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_allow_repeat_sends_a_file_again() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): JOB})
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "allow_repeat": True}
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"


@pytest.mark.asyncio
async def test_a_job_that_never_got_its_file_gets_a_new_link() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): JOB})
        routes.job.mock(
            side_effect=[
                httpx.Response(200, json=_job("WaitingForBlobUpload")),
                httpx.Response(200, json=_finished_job()),
            ]
        )
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"


@pytest.mark.asyncio
async def test_a_tag_that_names_a_job_that_is_gone_gets_a_new_link() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): "gone-job"})
        _arm(mock, "GET", f"{JOBS_PATH}/gone-job").respond(404, json=_error(404).json())
        await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.upload.call_count == 1


@pytest.mark.asyncio
async def test_a_different_file_is_not_taken_for_the_one_sent_before() -> None:
    other = CSV + "extra-server,2,4096,Ubuntu Linux\n"
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): JOB})
        await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": other}
        )

    assert routes.upload.call_count == 1


@pytest.mark.asyncio
async def test_an_interrupted_poll_still_reports_the_upload() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.job.mock(
            side_effect=[httpx.Response(200, json=_job("InProgress"))]
            + [_error(500, "InternalError")] * 4
        )
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"
    assert result["done"] is False
    assert "azure.migrate.job.list" in result["poll_error"]
    assert SIGNATURE not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_job_still_running_when_the_wait_is_over_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(migrate_writes, "_monotonic", lambda: float(next(clock)))
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.job.mock(return_value=httpx.Response(200, json=_job("InProgress")))
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["done"] is False
    assert result["state"] == "InProgress"
    assert result["rows_imported"] is None


@pytest.mark.asyncio
async def test_a_bad_row_reason_has_its_secrets_filtered_and_is_cut() -> None:
    errors = [f"Row {n}: password=MADEUPPASSWORD123 is not allowed" for n in range(150)]
    job = _job("CompletedWithErrors", errorSummary={"errors": errors, "errorCount": 150})
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.job.mock(return_value=httpx.Response(200, json=job))
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert len(result["bad_rows"]) == 100
    assert result["error_count"] == 150
    assert result["bad_rows_truncated"] is True
    assert "MADEUPPASSWORD123" not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_project_that_does_not_exist_fails_before_any_write() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.project.respond(404, json=_error(404).json())
        with pytest.raises(Exception, match="no such subscription"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.site_put.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_project_with_no_region_imports_nothing() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.project.respond(200, json={})
        with pytest.raises(ValueError, match="no region"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_bad_file_sends_nothing_to_azure() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        with pytest.raises(ValueError, match="csv"):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": "a,b\n"}
            )

    assert routes.project.call_count == 0
    assert routes.login.call_count == 0


# ----------------------------------------------------------------------
# The job list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_list_follows_every_page_and_never_returns_a_link() -> None:
    second = f"https://{ARM_HOST}{JOBS_PATH}?api-version=2023-06-06&page=2"
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        route = _arm(mock, "GET", JOBS_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"value": [_finished_job()], "nextLink": second}),
                httpx.Response(200, json={"value": [_job("Completed")]}),
            ]
        )
        result = await _connector().migrate_job_list(
            operator(), AzureTargetStub(), {"resource_group": RG, "project": PROJECT}
        )

    assert route.call_count == 2
    assert [row["state"] for row in result["value"]] == ["CompletedWithErrors", "Completed"]
    assert result["state_counts"] == {"CompletedWithErrors": 1, "Completed": 1}
    assert result["site"] == SITE
    assert result["truncated"] is False
    assert SIGNATURE not in json.dumps(result)
    assert "blobSasUri" not in json.dumps(result)


@pytest.mark.asyncio
async def test_job_list_with_no_jobs_and_a_failure_are_not_the_same() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", JOBS_PATH).respond(200, json={"value": []})
        result = await _connector().migrate_job_list(
            operator(), AzureTargetStub(), {"resource_group": RG, "project": PROJECT}
        )
    assert result["value"] == []
    assert result["state_counts"] == {}

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", JOBS_PATH).respond(404, json=_error(404).json())
        with pytest.raises(Exception):  # noqa: B017 -- the not-found error of the dispatcher
            await _connector().migrate_job_list(
                operator(), AzureTargetStub(), {"resource_group": RG, "project": PROJECT}
            )


@pytest.mark.asyncio
async def test_job_list_takes_an_explicit_site() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        route = _arm(
            mock, "GET", f"{RG_PATH}/providers/Microsoft.OffAzure/importSites/my-site/importJobs"
        )
        route.respond(200, json={"value": []})
        result = await _connector().migrate_job_list(
            operator(),
            AzureTargetStub(),
            {"resource_group": RG, "project": PROJECT, "site": "my-site"},
        )

    assert result["site"] == "my-site"
    assert route.call_count == 1


# ----------------------------------------------------------------------
# Groups
# ----------------------------------------------------------------------


def _group_answer(count: int = 0) -> dict[str, Any]:
    return {
        "name": GROUP,
        "id": f"{GROUP_PATH}",
        "properties": {"groupStatus": "Created", "groupType": "Default", "machineCount": count},
    }


class _Group:
    def __init__(self, mock: respx.MockRouter) -> None:
        _login(mock)
        self.put = _arm(mock, "PUT", GROUP_PATH).respond(201, json=_group_answer())
        self.get = _arm(mock, "GET", GROUP_PATH).respond(200, json=_group_answer(2))
        self.update = _arm(mock, "POST", f"{GROUP_PATH}/updateMachines").respond(
            200, json=_group_answer(2)
        )

    def bodies(self, route: respx.Route) -> list[dict[str, Any]]:
        return [json.loads(call.request.content) for call in route.calls]


@pytest.mark.asyncio
async def test_group_create_puts_the_name_then_adds_the_machines() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        result = await _connector().migrate_group_create(
            operator(),
            AzureTargetStub(),
            {**GROUP_PARAMS, "machines": ["m-1", "m-2", "m-1"], "group_type": "Import"},
        )

    assert routes.bodies(routes.put) == [{"properties": {"groupType": "Import"}}]
    assert routes.bodies(routes.update) == [
        {"properties": {"operationType": "Add", "machines": ["m-1", "m-2"]}}
    ]
    assert [step.get("step") or step["operation_type"] for step in result["steps"]] == [
        "group_put",
        "Add",
    ]
    assert result["group"]["machine_count"] == 2
    assert result["poll_error"] is None


@pytest.mark.asyncio
async def test_group_create_twice_sends_the_same_put() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        for _ in range(2):
            await _connector().migrate_group_create(operator(), AzureTargetStub(), GROUP_PARAMS)

    first, second = (call.request for call in routes.put.calls)
    assert first.url == second.url
    assert first.content == second.content
    assert routes.update.call_count == 0


@pytest.mark.asyncio
async def test_group_create_says_what_ran_when_adding_the_machines_fails() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.respond(409, json=_error(409, "Conflict").json())
        with pytest.raises(AzureRequestError) as caught:
            await _connector().migrate_group_create(
                operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
            )

    assert "The group was made" in str(caught.value)
    assert "no second group" in str(caught.value)
    assert routes.put.call_count == 1


@pytest.mark.asyncio
async def test_a_group_that_cannot_be_read_back_is_named_not_hidden() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.get.respond(403, json=_error(403, "AuthorizationFailed").json())
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), GROUP_PARAMS
        )

    assert result["group"] is None
    assert "could not be read back" in result["poll_error"]
    assert result["steps"][0]["step"] == "group_put"


@pytest.mark.asyncio
async def test_group_update_adds_first_then_removes() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        result = await _connector().migrate_group_update(
            operator(),
            AzureTargetStub(),
            {**GROUP_PARAMS, "add": ["m-1"], "remove": ["m-2", "m-3"]},
        )

    assert routes.bodies(routes.update) == [
        {"properties": {"operationType": "Add", "machines": ["m-1"]}},
        {"properties": {"operationType": "Remove", "machines": ["m-2", "m-3"]}},
    ]
    assert routes.put.call_count == 0
    assert [step["machines"] for step in result["steps"]] == [1, 2]


@pytest.mark.asyncio
async def test_group_update_says_what_ran_when_the_second_step_fails() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.mock(
            side_effect=[httpx.Response(200, json=_group_answer()), _error(409, "Conflict")]
        )
        with pytest.raises(AzureRequestError, match="The machines were added"):
            await _connector().migrate_group_update(
                operator(),
                AzureTargetStub(),
                {**GROUP_PARAMS, "add": ["m-1"], "remove": ["m-2"]},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({}, "at least one machine"),
        ({"add": ["m-1"], "remove": ["m-1"]}, "both add and remove"),
        ({"add": ["../x"]}, "add must hold machine names"),
        ({"add": "m-1"}, "list of machine names"),
        ({"add": [f"m-{n}" for n in range(MAX_MACHINES_PER_CALL + 1)]}, "Send at most"),
    ],
)
async def test_group_update_refuses_bad_machine_lists_before_any_call(
    extra: dict[str, Any], message: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        with pytest.raises(ValueError, match=message):
            await _connector().migrate_group_update(
                operator(), AzureTargetStub(), {**GROUP_PARAMS, **extra}
            )

    assert routes.update.call_count == 0


@pytest.mark.asyncio
async def test_a_group_write_that_azure_runs_as_a_long_operation_is_followed() -> None:
    poll = (
        f"https://{ARM_HOST}/subscriptions/{SUB}/providers/Microsoft.Migrate/"
        "locations/westeurope/operations/op-1?api-version=2024-01-15"
    )
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.respond(
            202, headers={"Azure-AsyncOperation": poll, "Retry-After": "1"}, json={}
        )
        mock.get(poll).respond(200, json={"status": "Succeeded"})
        result = await _connector().migrate_group_update(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "add": ["m-1"]}
        )

    step = result["steps"][0]
    assert step["done"] is True
    assert step["operation"]["poll_url"] == poll


# ----------------------------------------------------------------------
# Assessments
# ----------------------------------------------------------------------


def _assessment(status: str = "Completed", **over: Any) -> dict[str, Any]:
    props = {
        "status": status,
        "azureLocation": "westeurope",
        "currency": "EUR",
        "sizingCriterion": "AsOnPremises",
        "reservedInstance": "None",
        "azureHybridUseBenefit": "No",
        "scalingFactor": 1.0,
        "numberOfMachines": 2,
        "suitabilitySummary": {"Suitable": 1, "ConditionallySuitable": 1},
        "monthlyComputeCost": 61.5,
        **over,
    }
    return {"name": ASSESSMENT, "id": ASSESSMENT_PATH, "properties": props}


@pytest.mark.asyncio
async def test_assessment_create_puts_the_settings_and_follows_the_computation() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        get = _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_assessment("Running")),
                httpx.Response(200, json=_assessment("Completed")),
            ]
        )
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(201, json=_assessment("Created"))
        result = await _connector().migrate_assessment_create(
            operator(),
            AzureTargetStub(),
            {
                **ASSESSMENT_PARAMS,
                "pricing": "reserved_3_year",
                "hybrid_benefit": True,
                "comfort_factor": 1.3,
                "currency": "GBP",
                "sizing": "performance",
            },
        )

    assert json.loads(put.calls.last.request.content)["properties"] == {
        "azureLocation": "westeurope",
        "currency": "GBP",
        "sizingCriterion": "PerformanceBased",
        "reservedInstance": "RI3Year",
        "azureHybridUseBenefit": "Yes",
        "scalingFactor": 1.3,
        "azurePricingTier": "Standard",
        "azureOfferCode": "MSAZR0003P",
        "percentile": "Percentile95",
        "timeRange": "Day",
    }
    assert get.call_count == 3
    assert result["state"] == "created"
    assert result["done"] is True
    assert result["status"] == "Completed"
    assert result["machine_count"] == 2
    assert result["readiness_summary"] == {"Suitable": 1, "ConditionallySuitable": 1}
    assert result["assessment"]["monthly_compute_cost"] == 61.5


@pytest.mark.asyncio
async def test_assessment_create_defaults_to_euro_pay_as_you_go() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_assessment()),
            ]
        )
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(201, json={})
        await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    props = json.loads(put.calls.last.request.content)["properties"]
    assert props["currency"] == "EUR"
    assert props["reservedInstance"] == "None"
    assert props["azureHybridUseBenefit"] == "No"
    assert props["scalingFactor"] == 1.0


@pytest.mark.asyncio
async def test_a_retry_of_an_existing_assessment_sends_no_second_put() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).respond(200, json=_assessment("Running"))
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(200, json={})
        result = await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert put.call_count == 0
    assert result["state"] == "existing"
    assert result["done"] is False


@pytest.mark.asyncio
async def test_an_existing_assessment_with_other_settings_is_not_changed() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).respond(
            200, json=_assessment(azureLocation="northeurope", currency="USD")
        )
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(200, json={})
        with pytest.raises(ValueError, match=r"region, currency"):
            await _connector().migrate_assessment_create(
                operator(), AzureTargetStub(), ASSESSMENT_PARAMS
            )

    assert put.call_count == 0


@pytest.mark.asyncio
async def test_an_assessment_that_cannot_be_read_after_the_put_is_named_not_hidden() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[httpx.Response(404, json=_error(404).json())]
            + [_error(500, "InternalError")] * 4
        )
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(201, json={})
        result = await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert put.call_count == 1
    assert result["state"] == "created"
    assert result["done"] is False
    assert result["assessment"] is None
    assert "azure.migrate.assessment.list" in result["poll_error"]


@pytest.mark.asyncio
async def test_an_assessment_still_running_when_the_wait_is_over_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(migrate_writes, "_monotonic", lambda: float(next(clock)))
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[httpx.Response(404, json=_error(404).json())]
            + [httpx.Response(200, json=_assessment("Running"))] * 10
        )
        _arm(mock, "PUT", ASSESSMENT_PATH).respond(201, json={})
        result = await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert result["done"] is False
    assert result["status"] == "Running"


@pytest.mark.asyncio
async def test_a_failed_long_operation_of_the_put_raises_with_azures_reason() -> None:
    poll = (
        f"https://{ARM_HOST}/subscriptions/{SUB}/providers/Microsoft.Migrate/"
        "locations/westeurope/operations/op-2?api-version=2024-01-15"
    )
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).respond(404, json=_error(404).json())
        _arm(mock, "PUT", ASSESSMENT_PATH).respond(
            201, headers={"Azure-AsyncOperation": poll}, json={}
        )
        mock.get(poll).respond(
            200, json={"status": "Failed", "error": {"code": "Bad", "message": "No machines."}}
        )
        with pytest.raises(AzureRequestError) as caught:
            await _connector().migrate_assessment_create(
                operator(), AzureTargetStub(), ASSESSMENT_PARAMS
            )

    assert caught.value.azure_error_code == "Bad"


@pytest.mark.parametrize(
    "bad",
    [
        {"region": "West Europe"},
        {"region": "../x"},
        {"sizing": "fast"},
        {"currency": "XYZ"},
        {"pricing": "spot"},
        {"comfort_factor": 3},
        {"comfort_factor": 0.5},
        {"assessment": "../x"},
    ],
)
def test_the_assessment_schema_refuses_bad_settings(bad: dict[str, Any]) -> None:
    schema = _op("azure.migrate.assessment.create").parameter_schema

    assert validate_params(schema, {**ASSESSMENT_PARAMS, **bad})
    assert not validate_params(schema, ASSESSMENT_PARAMS)


# ----------------------------------------------------------------------
# The operation table
# ----------------------------------------------------------------------


def _op(op_id: str) -> Any:
    return next(op for op in OPS if op.op_id == op_id)


def test_the_writes_wait_for_approval_and_the_job_list_is_a_safe_read() -> None:
    levels = {op.op_id: (op.safety_level, op.requires_approval) for op in WRITE_OPS}

    assert levels == {
        "azure.migrate.import.start": ("caution", True),
        "azure.migrate.job.list": ("safe", False),
        "azure.migrate.group.create": ("caution", True),
        "azure.migrate.group.update": ("caution", True),
        "azure.migrate.assessment.create": ("caution", True),
    }


def test_the_writes_are_in_the_migrate_group_and_its_text_names_them() -> None:
    assert {op.group_key for op in WRITE_OPS} == {"migrate"}
    assert {op.op_id for op in WRITE_OPS} <= {op.op_id for op in OPS}
    text = WHEN_TO_USE["migrate"]
    for op in WRITE_OPS:
        assert op.op_id in text
    assert "Decide and Plan Expert" in text


def test_every_example_passes_its_own_schema() -> None:
    for op in WRITE_OPS:
        for example in op.parameter_schema["examples"]:
            assert validate_params(op.parameter_schema, example) == [], op.op_id


@pytest.mark.parametrize("bad", ["../x", "a/b", "a?b", "a%2Fb"])
def test_the_write_schemas_refuse_names_that_could_change_the_path(bad: str) -> None:
    for op_id in (
        "azure.migrate.import.start",
        "azure.migrate.job.list",
        "azure.migrate.group.create",
        "azure.migrate.group.update",
    ):
        schema = _op(op_id).parameter_schema
        example = dict(schema["examples"][0])
        assert validate_params(schema, {**example, "project": bad}), op_id


def test_the_import_schema_refuses_unknown_fields_and_a_bad_site() -> None:
    schema = _op("azure.migrate.import.start").parameter_schema
    example = schema["examples"][0]

    assert validate_params(schema, {**example, "upload_url": LINK})
    assert validate_params(schema, {**example, "site": "a/b"})
    assert validate_params(schema, {**example, "allow_repeat": "yes"})


# ----------------------------------------------------------------------
# The flight recorder
# ----------------------------------------------------------------------


def test_the_trace_keeps_neither_the_upload_link_nor_a_job_link() -> None:
    connector = _connector()
    answer = httpx.Response(
        200,
        json={"uri": LINK, "jobArmId": f"{JOBS_PATH}/{JOB}", "properties": {"blobSasUri": LINK}},
    )

    masked = connector.trace_response_body(answer)

    assert SIGNATURE not in json.dumps(masked)
    assert masked["jobArmId"] == f"{JOBS_PATH}/{JOB}"

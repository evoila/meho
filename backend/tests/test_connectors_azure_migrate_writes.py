# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure Migrate writes: CSV import, groups and VM assessments.

Azure Resource Manager, the upload link and Microsoft's login service are
mocked with respx. All names, ids and links are made up. The tests run the
real connector, the token cache and the real retry rules of ``send``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog

from meho_backplane.connectors.azure import (
    AzureConnector,
    _http,
    _polling,
    migrate_import,
    migrate_writes,
)
from meho_backplane.connectors.azure._errors import (
    AzureConflictError,
    AzureForbiddenError,
    AzureRequestError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure.migrate import OPS, WHEN_TO_USE
from meho_backplane.connectors.azure.migrate_csv import MAX_CSV_BYTES, MAX_CSV_ROWS, prepare_csv
from meho_backplane.connectors.azure.migrate_jobs import default_site_name, site_param
from meho_backplane.connectors.azure.migrate_write_ops import WRITE_OPS
from meho_backplane.connectors.azure.migrate_writes import MAX_MACHINES_PER_CALL
from meho_backplane.operations._errors import result_connector_error
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
SIGNATURE = "madeup7value9q"
LINK = f"https://{BLOB_HOST}/import-container/servers.csv?sv=2024-01-01&sig={SIGNATURE}"

RG_PATH = f"/subscriptions/{SUB}/resourceGroups/{RG}"
PROJECT_PATH = f"{RG_PATH}/providers/Microsoft.Migrate/assessmentProjects/{PROJECT}"
SITE_PATH = f"{RG_PATH}/providers/Microsoft.OffAzure/importSites/{SITE}"
JOBS_PATH = f"{SITE_PATH}/importJobs"
SITE_TAGS_PATH = f"{SITE_PATH}/providers/Microsoft.Resources/tags/default"
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


@pytest.fixture(autouse=True)
def _site_lock(monkeypatch: pytest.MonkeyPatch) -> dict[int, asyncio.Lock]:
    """The PostgreSQL locks of an import site and an assessment, as one ``asyncio.Lock`` per key."""
    locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def fake(key: int, **_: Any) -> AsyncIterator[bool]:
        async with locks.setdefault(key, asyncio.Lock()):
            yield True

    monkeypatch.setattr(migrate_import, "advisory_lock", fake)
    monkeypatch.setattr(migrate_writes, "advisory_lock", fake)
    return locks


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
        self.site_patch = _arm(mock, "PATCH", SITE_TAGS_PATH).respond(200, json={})
        self.collector_get = _arm(mock, "GET", COLLECTOR_PATH).respond(404, json=_error(404).json())
        self.collector = _arm(mock, "PUT", COLLECTOR_PATH).respond(200, json={})
        self.import_uri = _arm(mock, "POST", f"{SITE_PATH}/importUri").respond(
            200, json={"uri": LINK, "jobArmId": f"{JOBS_PATH}/{JOB}"}
        )
        self.upload = mock.put(host=BLOB_HOST, path="/import-container/servers.csv").respond(201)
        self.job_list = _arm(mock, "GET", JOBS_PATH).respond(200, json={"value": []})
        self.job = _arm(mock, "GET", f"{JOBS_PATH}/{JOB}").mock(
            side_effect=[
                httpx.Response(200, json=_job("InProgress")),
                httpx.Response(200, json=_finished_job()),
            ]
        )


def _tag(csv_text: str = CSV) -> str:
    return prepare_csv(csv_text).tag_key


def _tag_patch(operation: str, key: str, job: str = JOB) -> dict[str, Any]:
    """The body of a Tags API PATCH that adds or removes one record tag."""
    return {"operation": operation, "properties": {"tags": {key: job}}}


def _patch_bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


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
    text = "Server name,Cores,Memory (In MB),OS name\n" + "a" * MAX_CSV_BYTES

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
    assert _patch_bodies(routes.site_patch) == [_tag_patch("Merge", _tag())]
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
    # Only MEHO's own tag is named: the owner tag is not sent back.
    assert _patch_bodies(routes.site_patch) == [_tag_patch("Merge", _tag())]


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


def _repeat_tag(key: str) -> str:
    return f"{_tag()}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


@pytest.mark.asyncio
async def test_a_new_repeat_key_sends_a_file_again_under_its_own_record() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): JOB})
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "repeat_key": "2026-11-10"}
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"
    assert _patch_bodies(routes.site_patch) == [_tag_patch("Merge", _repeat_tag("2026-11-10"))]


@pytest.mark.asyncio
async def test_a_retry_with_the_same_repeat_key_after_a_lost_answer_uploads_nothing() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): "older-job", _repeat_tag("2026-11-10"): JOB})
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "repeat_key": "2026-11-10"}
        )

    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0
    assert result["outcome"] == "already_sent"
    assert result["job_name"] == JOB


@pytest.mark.asyncio
async def test_a_bad_repeat_key_sends_nothing() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        with pytest.raises(ValueError, match="repeat_key"):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "repeat_key": "a/b"}
            )

    assert routes.project.call_count == 0


@pytest.mark.asyncio
async def test_a_job_still_waiting_for_its_file_is_followed_and_not_sent_again() -> None:
    # The first upload may still be under way, or its answer was lost.
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

    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0
    assert routes.site_patch.call_count == 0
    assert result["outcome"] == "earlier_upload_waiting"
    assert result["state"] == "CompletedWithErrors"


@pytest.mark.asyncio
async def test_a_refused_upload_drops_its_record_so_a_retry_uploads_again() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={"owner": "example-team"})
        routes.upload.respond(403)
        with pytest.raises(AzureRequestError):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert _patch_bodies(routes.site_patch) == [
        _tag_patch("Merge", _tag()),
        _tag_patch("Delete", _tag()),
    ]


def _tags_api(site_tags: dict[str, str]) -> Any:
    """A Tags API PATCH handler that merges into or deletes from *site_tags*."""

    def patch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        named = body["properties"]["tags"]
        if body["operation"] == "Merge":
            site_tags.update(named)
        else:
            for key, value in named.items():
                if site_tags.get(key) == value:
                    del site_tags[key]
        return httpx.Response(200, json={"properties": {"tags": dict(site_tags)}})

    return patch


@pytest.mark.asyncio
async def test_overlapping_imports_of_two_files_keep_both_records() -> None:
    other = CSV + "extra-server,2,4096,Ubuntu Linux\n"
    site_tags: dict[str, str] = {}

    async def read_site(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # let the other import run in between
        return httpx.Response(200, json={"tags": dict(site_tags)})

    jobs = iter(["example-job-01", "example-job-02"])

    def new_job(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"uri": LINK, "jobArmId": f"{JOBS_PATH}/{next(jobs)}"})

    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.site_get.mock(side_effect=read_site)
        routes.site_patch.mock(side_effect=_tags_api(site_tags))
        routes.import_uri.mock(side_effect=new_job)
        _arm(mock, "GET", f"{JOBS_PATH}/example-job-01").respond(200, json=_finished_job())
        _arm(mock, "GET", f"{JOBS_PATH}/example-job-02").respond(200, json=_finished_job())
        connector = _connector()
        await asyncio.gather(
            connector.migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS),
            connector.migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": other}
            ),
        )

    assert set(site_tags) == {_tag(), _tag(other)}
    assert sorted(site_tags.values()) == ["example-job-01", "example-job-02"]
    assert routes.upload.call_count == 2


@pytest.mark.asyncio
async def test_overlapping_imports_of_one_file_upload_it_once() -> None:
    site_tags: dict[str, str] = {}

    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.site_get.mock(side_effect=lambda _: httpx.Response(200, json={"tags": site_tags}))
        routes.site_patch.mock(side_effect=_tags_api(site_tags))
        routes.job.mock(return_value=httpx.Response(200, json=_finished_job()))
        connector = _connector()
        results = await asyncio.gather(
            connector.migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS),
            connector.migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS),
        )

    assert routes.upload.call_count == 1
    assert sorted(r["outcome"] for r in results) == ["already_sent", "uploaded"]


@pytest.mark.asyncio
async def test_a_site_whose_lock_stays_busy_gets_nothing_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def busy(key: int, **_: Any) -> AsyncIterator[bool]:
        yield False

    monkeypatch.setattr(migrate_import, "advisory_lock", busy)
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        with pytest.raises(AzureConflictError, match="another import"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.site_get.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_tag_that_names_a_job_that_is_gone_keeps_its_record_and_uploads_nothing() -> None:
    # A job Azure no longer has may still have imported the file.
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): "gone-job"})
        _arm(mock, "GET", f"{JOBS_PATH}/gone-job").respond(404, json=_error(404).json())
        with pytest.raises(AzureConflictError, match="repeat_key") as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert "gone-job" in str(caught.value)
    assert "repeat_key" in caught.value.remediation
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0
    assert routes.site_patch.call_count == 0


@pytest.mark.asyncio
async def test_a_gone_job_with_a_new_repeat_key_uploads_again() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={_tag(): "gone-job"})
        _arm(mock, "GET", f"{JOBS_PATH}/gone-job").respond(404, json=_error(404).json())
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "repeat_key": "again"}
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"


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
        "azurePricingTier": "Standard",
        "azureOfferCode": "MSAZR0003P",
        "percentile": "Percentile95",
        "timeRange": "Day",
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
@pytest.mark.parametrize(
    ("over", "named"),
    [
        ({"azureOfferCode": "MSAZR0023P"}, "azureOfferCode"),
        ({"percentile": "Percentile50"}, "percentile"),
        ({"timeRange": "Month"}, "timeRange"),
        ({"azurePricingTier": "Basic"}, "azurePricingTier"),
        ({"reservedInstance": "RI1Year"}, "pricing"),
        ({"scalingFactor": 1.5}, "comfort_factor"),
        ({"timeRange": None}, "timeRange"),
    ],
)
async def test_an_existing_assessment_with_other_fixed_settings_is_not_accepted(
    over: dict[str, Any], named: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).respond(200, json=_assessment(**over))
        put = _arm(mock, "PUT", ASSESSMENT_PATH).respond(200, json={})
        with pytest.raises(ValueError, match=rf"other settings \({named}\)"):
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
    assert validate_params(schema, {**example, "repeat_key": "a/b"})
    assert validate_params(schema, {**example, "allow_repeat": True})
    assert not validate_params(schema, {**example, "repeat_key": "2026-11-10"})


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


# ----------------------------------------------------------------------
# Refused uploads, wrong answer shapes and the steps Azure has not confirmed
# ----------------------------------------------------------------------


def _shared_site(mock: respx.MockRouter, site_tags: dict[str, str]) -> _Import:
    """Import routes whose site tags live in *site_tags* and change with the Tags API."""
    routes = _Import(mock, tags={})
    routes.site_get.mock(side_effect=lambda _: httpx.Response(200, json={"tags": dict(site_tags)}))
    routes.site_patch.mock(side_effect=_tags_api(site_tags))
    routes.job.mock(return_value=httpx.Response(200, json=_finished_job()))
    return routes


def _error_fields(error: AzureRequestError) -> str:
    """Every text an upload error carries: its own texts and the dispatcher's answer."""
    answer = result_connector_error("azure.migrate.import.start", error, 1.0)
    return json.dumps(
        [str(error), error.remediation, vars(error), answer.model_dump(mode="json")],
        default=str,
    )


#: A made-up Azure Storage error code for the refused upload answer.
STORAGE_ERROR_CODE = "ExampleRefusal7"


def _refused_upload_body() -> str:
    """An Azure Storage refusal that names every part of the made-up upload link."""
    return (
        f"<Error><Code>{STORAGE_ERROR_CODE}</Code><Message>Refused {BLOB_HOST} "
        f"import-container servers.csv {SIGNATURE}</Message></Error>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
async def test_every_refused_upload_is_sent_once_and_gives_the_fixed_upload_error(
    status: int,
) -> None:
    site_tags = {"owner": "example-team"}
    with respx.mock(assert_all_called=False) as mock:
        routes = _shared_site(mock, site_tags)
        routes.upload.respond(
            status,
            headers={"Retry-After": "1", "x-ms-error-code": STORAGE_ERROR_CODE},
            text=_refused_upload_body(),
        )
        with pytest.raises(AzureRequestError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

        assert routes.upload.call_count == 1
        assert routes.login.call_count == 1  # no new login after a 401 of the upload
        error = caught.value
        assert str(error).startswith(
            f"Azure target 'azure-sub-a': Azure Storage refused the upload (HTTP {status})"
        )
        assert error.http_status == status
        text = _error_fields(error)
        for part in (
            BLOB_HOST,
            "example-import",
            "import-container",
            "servers.csv",
            SIGNATURE,
            STORAGE_ERROR_CODE,
        ):
            assert part not in text
        assert SITE_PATH not in text
        assert _patch_bodies(routes.site_patch) == [
            _tag_patch("Merge", _tag()),
            _tag_patch("Delete", _tag()),
        ]
        assert site_tags == {"owner": "example-team"}

        # The same call again gets a new upload link and sends the file.
        routes.upload.respond(201)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.import_uri.call_count == 2
    assert routes.upload.call_count == 2
    assert result["outcome"] == "uploaded"


@pytest.mark.asyncio
async def test_a_tag_changed_during_a_refused_upload_survives() -> None:
    site_tags = {"owner": "example-team"}

    def refuse(_: httpx.Request) -> httpx.Response:
        site_tags["owner"] = "other-team"  # someone changes a tag in Azure meanwhile
        site_tags["cost-center"] = "example-1"
        return httpx.Response(403)

    with respx.mock(assert_all_called=False) as mock:
        routes = _shared_site(mock, site_tags)
        routes.upload.mock(side_effect=refuse)
        with pytest.raises(AzureRequestError):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert site_tags == {"owner": "other-team", "cost-center": "example-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[{"uri": LINK, "jobArmId": f"{JOBS_PATH}/{JOB}"}]),
        httpx.Response(200, json="text"),
    ],
)
async def test_an_upload_link_answer_that_is_not_an_object_uploads_and_records_nothing(
    answer: httpx.Response,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.import_uri.mock(return_value=answer)
        with pytest.raises(AzureUrlRefusedError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert str(caught.value) == (
        "Azure target 'azure-sub-a': Azure's answer for the upload link is not a JSON "
        "object. MEHO uploaded nothing."
    )
    assert routes.upload.call_count == 0
    assert routes.site_patch.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[_job("Completed")], "text"])
async def test_a_job_answer_that_is_not_an_object_after_the_upload_is_a_poll_error(
    body: object,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.job.mock(return_value=httpx.Response(200, json=body))
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.upload.call_count == 1
    assert result["outcome"] == "uploaded"
    assert result["done"] is False
    assert result["state"] is None
    assert "not a JSON object" in result["poll_error"]


@pytest.mark.asyncio
async def test_job_list_skips_an_item_that_is_not_an_object_and_counts_it() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", JOBS_PATH).respond(200, json={"value": [_finished_job(), ["x"], "y"]})
        result = await _connector().migrate_job_list(
            operator(), AzureTargetStub(), {"resource_group": RG, "project": PROJECT}
        )

    assert [row["name"] for row in result["value"]] == [JOB]
    assert result["truncated"] is True
    assert result["omissions"]["count"] == 1
    assert result["omissions"]["items"][0]["reason"] == "not_an_object"
    assert "2 item(s)" in result["omissions"]["items"][0]["detail"]


#: A poll link and a first answer whose wait is longer than MEHO waits.
_POLL = (
    f"https://{ARM_HOST}/subscriptions/{SUB}/providers/Microsoft.Migrate/"
    "locations/westeurope/operations/op-1?api-version=2024-01-15"
)
_PENDING = {"Azure-AsyncOperation": _POLL, "Retry-After": "3600"}


@pytest.mark.asyncio
async def test_group_create_adds_no_machines_while_azure_still_makes_the_group() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, headers=_PENDING, json=_group_answer())
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    assert routes.update.call_count == 0
    assert result["steps"][0]["done"] is False
    assert result["steps"][0]["operation"]["poll_url"] == _POLL
    assert result["steps"][1]["sent"] is False
    assert "Adding the machines was not sent" in result["not_sent"]


@pytest.mark.asyncio
async def test_group_update_does_not_remove_while_the_add_is_still_running() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.respond(202, headers=_PENDING, json={})
        result = await _connector().migrate_group_update(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "add": ["m-1"], "remove": ["m-2"]}
        )

    assert [body["properties"]["operationType"] for body in routes.bodies(routes.update)] == ["Add"]
    assert [step["sent"] for step in result["steps"]] == [True, False]
    assert "Taking machines out was not sent" in result["not_sent"]


@pytest.mark.asyncio
async def test_an_import_stops_before_the_upload_while_azure_still_makes_the_site() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_put.respond(201, headers=_PENDING, json={"name": SITE})
        with pytest.raises(AzureConflictError, match="the new import site"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.collector.call_count == 0
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_an_import_stops_before_the_upload_while_azure_still_makes_the_collector() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.collector.respond(201, headers=_PENDING, json={})
        with pytest.raises(AzureConflictError, match="the import collector"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_an_accepted_write_whose_status_cannot_be_read_keeps_its_reference() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, headers={"Azure-AsyncOperation": _POLL}, json=_group_answer())
        mock.get(_POLL).respond(403, json=_error(403, "AuthorizationFailed").json())
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    step = result["steps"][0]
    assert step["operation"]["poll_url"] == _POLL
    assert step["done"] is False
    assert "status could not be read" in step["poll_error"]
    assert routes.update.call_count == 0
    assert result["not_sent"] is not None


@pytest.mark.asyncio
async def test_an_accepted_assessment_whose_status_cannot_be_read_keeps_its_reference() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_assessment("Completed")),
            ]
        )
        _arm(mock, "PUT", ASSESSMENT_PATH).respond(
            202, headers={"Azure-AsyncOperation": _POLL}, json={}
        )
        mock.get(_POLL).mock(side_effect=httpx.ConnectError("lost"))
        result = await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert result["operation"]["poll_url"] == _POLL
    assert "status could not be read" in result["poll_error"]
    assert result["status"] == "Completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "done", "failed"),
    [
        ("Completed", True, False),
        ("OutOfSync", True, False),
        ("Invalid", False, True),
        ("Deleted", False, True),
    ],
)
async def test_an_assessment_azure_cannot_compute_is_failed_not_done(
    status: str, done: bool, failed: bool
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        get = _arm(mock, "GET", ASSESSMENT_PATH).mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_assessment(status)),
            ]
        )
        _arm(mock, "PUT", ASSESSMENT_PATH).respond(201, json=_assessment("Created"))
        result = await _connector().migrate_assessment_create(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert get.call_count == 2  # a final status ends the reads at once
    assert (result["done"], result["failed"], result["status"]) == (done, failed, status)


@pytest.mark.asyncio
async def test_two_overlapping_assessment_calls_with_other_settings_make_one() -> None:
    made: list[dict[str, Any]] = []

    async def read(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # let the other call run in between
        if not made:
            return httpx.Response(404, json=_error(404).json())
        return httpx.Response(200, json={"name": ASSESSMENT, **made[0], "status": "x"})

    def write(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        made.append({"properties": {**body["properties"], "status": "Completed"}})
        return httpx.Response(201, json={})

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _arm(mock, "GET", ASSESSMENT_PATH).mock(side_effect=read)
        put = _arm(mock, "PUT", ASSESSMENT_PATH).mock(side_effect=write)
        connector = _connector()
        results = await asyncio.gather(
            connector.migrate_assessment_create(operator(), AzureTargetStub(), ASSESSMENT_PARAMS),
            connector.migrate_assessment_create(
                operator(), AzureTargetStub(), {**ASSESSMENT_PARAMS, "currency": "USD"}
            ),
            return_exceptions=True,
        )

    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content)["properties"]["currency"] == "EUR"
    assert isinstance(results[0], dict)
    assert results[0]["state"] == "created"
    assert isinstance(results[1], ValueError)
    assert "currency" in str(results[1])


# ----------------------------------------------------------------------
# The file through MCP, the column names, the approval text
# ----------------------------------------------------------------------


def test_the_required_columns_must_be_there_by_their_full_name() -> None:
    text = "*Server name,*Cores,Memory utilization percentage,*OS name\na,1,50,x\n"

    with pytest.raises(ValueError, match="memory \\(in mb\\)"):
        prepare_csv(text)


def test_a_file_with_a_byte_order_mark_is_accepted_and_hashed_as_sent() -> None:
    text = "\ufeff" + CSV
    prepared = prepare_csv(text)

    assert prepared.rows == CSV_ROWS
    assert prepared.content == text.encode("utf-8")
    assert prepared.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("char", ["\x01", "\x1b", "\x7f", "\x85"])
def test_a_file_with_a_control_character_is_refused(char: str) -> None:
    with pytest.raises(ValueError, match="control character"):
        prepare_csv(f"Server name,Cores,Memory (In MB),OS name\na{char},1,1,x\n")


def _largest_file() -> str:
    """A file of the largest size MEHO takes, of the characters JSON makes longest."""
    header = "Server name,Cores,Memory (In MB),OS name\n"
    row = '"é\\""\u20ac,1,1,"\\"\n'
    rows = (MAX_CSV_BYTES - len(header.encode())) // len(row.encode())
    text = header + row * rows
    pad = MAX_CSV_BYTES - len(text.encode())
    return text + "é" * (pad // 2) + "x" * (pad % 2)


def test_the_largest_file_passes_the_check_and_the_schema() -> None:
    text = _largest_file()

    assert len(text.encode()) == MAX_CSV_BYTES
    prepare_csv(text)
    schema = _op("azure.migrate.import.start").parameter_schema
    assert validate_params(schema, {**IMPORT_PARAMS, "csv": text}) == []
    with pytest.raises(ValueError, match="bytes"):
        prepare_csv(text + "x")


def test_the_largest_file_reaches_the_operation_through_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from meho_backplane.connectors.schemas import OperationResult
    from meho_backplane.main import app
    from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
    from meho_backplane.mcp.server import _MAX_REQUEST_BODY_BYTES
    from meho_backplane.operations import meta_tools
    from meho_backplane.settings import get_settings

    for name, value in (
        ("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho"),
        ("KEYCLOAK_AUDIENCE", "meho-backplane"),
        ("VAULT_ADDR", "https://vault.test"),
        ("BACKPLANE_URL", "https://meho.test"),
    ):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    seen: list[dict[str, Any]] = []

    async def registered_target(*_: Any, **__: Any) -> tuple[Any, None]:
        return AzureTargetStub(), None

    async def recording_dispatch(**kwargs: Any) -> OperationResult:
        seen.append(kwargs)
        return OperationResult(status="pending", op_id=kwargs["op_id"], duration_ms=0.0)

    monkeypatch.setattr(meta_tools, "_resolve_target_or_error", registered_target)
    monkeypatch.setattr(meta_tools, "dispatch", recording_dispatch)
    text = _largest_file()
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "call_operation",
            "arguments": {
                "connector_id": "azure-arm-2026.10",
                "op_id": "azure.migrate.import.start",
                "target": "example-target",
                "params": {**IMPORT_PARAMS, "csv": text},
            },
        },
    }
    # ensure_ascii writes é as \u00e9 and € as \u20ac: the longest form a client can send.
    body = json.dumps(request).encode()
    assert len(body) <= _MAX_REQUEST_BODY_BYTES

    async def fixture_operator() -> Any:
        return operator()

    app.dependency_overrides[verify_mcp_jwt_and_bind] = fixture_operator
    try:
        response = TestClient(app).post(
            "/mcp", content=body, headers={"content-type": "application/json"}
        )
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["id"] == 7
    # The operation got the whole file: the same bytes, size and SHA-256.
    assert len(seen) == 1
    assert seen[0]["connector_id"] == "azure-arm-2026.10"
    assert seen[0]["op_id"] == "azure.migrate.import.start"
    received = seen[0]["params"]["csv"].encode()
    assert len(received) == MAX_CSV_BYTES
    assert hashlib.sha256(received).hexdigest() == hashlib.sha256(text.encode()).hexdigest()


#: The whole approval text of every write, written out. A dropped sentence fails the test.
_APPROVAL_TEXT = (
    "The call waits for a person's approval. A service login runs it at once when a "
    "standing grant or an approved run covers it. A platform admin can turn approval off "
    "for this operation for the whole install, with a reason and a fresh sign-in. A person "
    "in the web console then runs it at once. Calls by token, by a service login without a "
    "grant and by an agent still wait."
)


def test_the_guidance_of_every_write_says_who_waits_for_approval() -> None:
    writes = [op for op in WRITE_OPS if op.requires_approval]

    assert len(writes) == 4
    for op in writes:
        assert op.llm_instructions["preconditions"].startswith(_APPROVAL_TEXT), op.op_id


#: The advice sentences on a new repeat_key, written out.
_REPEAT_KEY_ADVICE = (
    "Set a new repeat_key also to import the same file on purpose. When an error says Azure "
    "no longer has the job of an earlier upload, the file may have been imported: tell the "
    "person and read azure.migrate.job.list and azure.migrate.machine.list first; a new "
    "repeat_key is only for a person who wants another import of the same file."
)


def test_the_import_advice_says_to_read_the_job_and_machine_lists_for_a_gone_job() -> None:
    advice = _op("azure.migrate.import.start").llm_instructions["next_step"]

    assert _REPEAT_KEY_ADVICE in advice
    assert "or when an error says Azure no longer has the job" not in advice


# ----------------------------------------------------------------------
# The site's tag limit, steps Azure has not finished, the collector, lost references
# ----------------------------------------------------------------------


def _other_file(number: int) -> str:
    return CSV + f"extra-server-{number},2,4096,Ubuntu Linux\n"


def _full_tags(count: int) -> dict[str, str]:
    return {f"owner-{number}": "example-team" for number in range(count)}


@pytest.mark.asyncio
async def test_a_site_with_49_tags_takes_one_more_record() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags=_full_tags(49))
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "uploaded"
    assert _patch_bodies(routes.site_patch) == [_tag_patch("Merge", _tag())]


@pytest.mark.asyncio
async def test_a_site_with_50_tags_gets_no_upload_link_and_no_upload() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags=_full_tags(50))
        with pytest.raises(AzureConflictError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    text = f"{caught.value} {caught.value.remediation}"
    assert "has 50 tags" in text
    assert "at most 50 tags" in text
    assert "site parameter" in text
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0
    assert routes.site_patch.call_count == 0


@pytest.mark.asyncio
async def test_a_retry_with_a_record_still_works_on_a_site_with_50_tags() -> None:
    tags = _full_tags(49)
    tags[_tag()] = JOB
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags=tags)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "already_sent"
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_file_split_over_many_calls_fills_the_site_and_the_next_call_is_refused() -> None:
    site_tags: dict[str, str] = {}
    with respx.mock(assert_all_called=False) as mock:
        routes = _shared_site(mock, site_tags)
        for number in range(50):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(number)}
            )
        assert len(site_tags) == 50
        links_asked = routes.import_uri.call_count
        uploads = routes.upload.call_count
        with pytest.raises(AzureConflictError, match="has 50 tags"):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(50)}
            )
        # A retry of a file that has a record still finds its job.
        again = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(7)}
        )

    assert again["outcome"] == "already_sent"
    assert links_asked == 50
    assert routes.import_uri.call_count == links_asked
    assert routes.upload.call_count == uploads == 50


#: The time "now" of the tests that make room on a full site.
_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _ended_job(name: str, ended: datetime | None, state: str = "Completed") -> dict[str, Any]:
    """An import job as Azure lists it, with the time it ended (or none)."""
    return {
        "name": name,
        "id": f"{JOBS_PATH}/{name}",
        "startTime": "2026-11-01T08:00:00Z",
        "endTime": ended.isoformat().replace("+00:00", "Z") if ended else None,
        "properties": {"jobResult": state, "numberOfMachinesImported": 1},
    }


class _SiteOverTime:
    """A shared site where each upload link makes a new job that ended *age* before now."""

    def __init__(self, mock: respx.MockRouter, site_tags: dict[str, str], age: timedelta) -> None:
        self.routes = _shared_site(mock, site_tags)
        self.jobs: list[dict[str, Any]] = []
        self.age = age
        self.routes.import_uri.mock(side_effect=self._new_job)
        self.routes.job_list.mock(
            side_effect=lambda _: httpx.Response(200, json={"value": self.jobs})
        )
        mock.route(method="GET", host=ARM_HOST, path__regex=rf"^{JOBS_PATH}/job-\d+$").mock(
            side_effect=self._read_job
        )

    def _new_job(self, _: httpx.Request) -> httpx.Response:
        name = f"job-{len(self.jobs):03d}"
        # Each later job ended one minute later, so the first job is the oldest.
        ended = _NOW - self.age + timedelta(minutes=len(self.jobs))
        self.jobs.append(_ended_job(name, ended))
        return httpx.Response(200, json={"uri": LINK, "jobArmId": f"{JOBS_PATH}/{name}"})

    def _read_job(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rpartition("/")[2]
        return httpx.Response(200, json=next(job for job in self.jobs if job["name"] == name))


@pytest.mark.asyncio
async def test_a_site_takes_more_than_50_files_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate_import, "_utc_now", lambda: _NOW)
    site_tags: dict[str, str] = {"owner": "example-team"}
    sizes: list[int] = []
    with respx.mock(assert_all_called=False) as mock:
        site = _SiteOverTime(mock, site_tags, timedelta(days=2))
        for number in range(60):
            result = await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(number)}
            )
            assert result["outcome"] == "uploaded"
            sizes.append(len(site_tags))
        deletes = [
            body for body in _patch_bodies(site.routes.site_patch) if body["operation"] == "Delete"
        ]

    assert max(sizes) == 50
    assert site.routes.upload.call_count == 60
    # One owner tag and 60 files need 61 tags: MEHO removed the records of the
    # eleven oldest jobs, oldest first, and no other tag.
    assert [next(iter(body["properties"]["tags"].values())) for body in deletes] == [
        f"job-{number:03d}" for number in range(11)
    ]
    assert site_tags["owner"] == "example-team"
    assert _tag(_other_file(0)) not in site_tags
    assert site_tags[_tag(_other_file(59))] == "job-059"


@pytest.mark.asyncio
async def test_a_retry_after_its_record_was_removed_imports_the_file_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migrate_import, "_utc_now", lambda: _NOW)
    site_tags: dict[str, str] = {}
    with respx.mock(assert_all_called=False) as mock:
        site = _SiteOverTime(mock, site_tags, timedelta(days=2))
        for number in range(51):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(number)}
            )
        # The record of file 0 was removed to make room for file 50.
        assert _tag(_other_file(0)) not in site_tags
        again = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(0)}
        )

    assert again["outcome"] == "uploaded"
    assert again["job_name"] == "job-051"
    assert site.routes.upload.call_count == 52


@pytest.mark.asyncio
async def test_records_of_jobs_that_ended_less_than_a_day_ago_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migrate_import, "_utc_now", lambda: _NOW)
    site_tags: dict[str, str] = {}
    with respx.mock(assert_all_called=False) as mock:
        site = _SiteOverTime(mock, site_tags, timedelta(hours=23))
        for number in range(50):
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(number)}
            )
        links_asked = site.routes.import_uri.call_count
        patches = site.routes.site_patch.call_count
        with pytest.raises(AzureConflictError) as caught:
            await _connector().migrate_import_start(
                operator(), AzureTargetStub(), {**IMPORT_PARAMS, "csv": _other_file(50)}
            )

    text = f"{caught.value} {caught.value.remediation}"
    assert "has 50 tags" in text
    assert "ended at least a day ago" in text
    assert "azure.migrate.job.list" in text
    assert len(site_tags) == 50
    assert site.routes.import_uri.call_count == links_asked
    assert site.routes.site_patch.call_count == patches  # no tag removed
    assert site.routes.upload.call_count == 50


@pytest.mark.asyncio
async def test_only_ended_records_are_removed_and_other_tags_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migrate_import, "_utc_now", lambda: _NOW)
    old = _NOW - timedelta(days=3)
    site_tags = _full_tags(46)
    site_tags.update(
        {
            "meho-csv-00000000000000a1": "job-running",
            "meho-csv-00000000000000a2": "job-not-listed",
            "meho-csv-00000000000000a3": "job-no-end-time",
            "meho-csv-00000000000000a4": "job-ended-old",
        }
    )
    with respx.mock(assert_all_called=False) as mock:
        routes = _shared_site(mock, site_tags)
        routes.job_list.respond(
            200,
            json={
                "value": [
                    _ended_job("job-running", None, "InProgress"),
                    _ended_job("job-no-end-time", None),
                    _ended_job("job-ended-old", old),
                    # A job that ended long ago but has no MEHO record.
                    _ended_job("job-of-someone-else", old - timedelta(days=9)),
                ]
            },
        )
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "uploaded"
    assert _patch_bodies(routes.site_patch) == [
        _tag_patch("Delete", "meho-csv-00000000000000a4", "job-ended-old"),
        _tag_patch("Merge", _tag()),
    ]
    assert len(site_tags) == 50
    assert {"meho-csv-00000000000000a1", "meho-csv-00000000000000a2"} <= site_tags.keys()


def _state_of(state: str) -> dict[str, Any]:
    return {"name": SITE, "properties": {"provisioningState": state}}


def _group_state(state: str, count: int = 0) -> dict[str, Any]:
    answer = _group_answer(count)
    answer["properties"]["provisioningState"] = state
    return answer


@pytest.mark.asyncio
async def test_a_pending_group_put_with_no_link_is_read_until_it_succeeds_before_the_add() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, json=_group_state("Provisioning"))
        routes.get.mock(
            side_effect=[
                httpx.Response(200, json=_group_state("Provisioning")),
                httpx.Response(200, json=_group_state("Succeeded")),
                httpx.Response(200, json=_group_state("Succeeded", 1)),
            ]
        )
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    assert routes.get.call_count == 3  # two reads until it ended, then the read-back
    assert routes.update.call_count == 1
    assert result["steps"][0]["done"] is True
    assert result["steps"][1]["sent"] is True
    assert result["not_sent"] is None


@pytest.mark.asyncio
async def test_a_group_put_still_pending_when_the_wait_is_over_sends_no_add() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, headers={"Retry-After": "3600"}, json=_group_state("Running"))
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    assert routes.update.call_count == 0
    assert result["steps"][0]["done"] is False
    assert result["steps"][0]["operation"] is None
    assert "had not finished the resource" in result["steps"][0]["poll_error"]
    assert result["steps"][1]["sent"] is False
    assert "Adding the machines was not sent" in result["not_sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Failed", "Canceled"])
async def test_a_group_that_azure_ends_as_failed_raises_with_the_reason(state: str) -> None:
    failed = {**_group_state(state), "error": {"code": "ExampleFault", "message": "Example."}}
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, json=_group_state("Provisioning"))
        routes.get.respond(200, json=failed)
        with pytest.raises(AzureRequestError) as caught:
            await _connector().migrate_group_create(
                operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
            )

    assert caught.value.azure_error_code == "ExampleFault"
    assert state in str(caught.value)
    assert routes.update.call_count == 0


@pytest.mark.asyncio
async def test_a_group_put_that_names_no_state_counts_as_done() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, json={"name": GROUP})
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    assert result["steps"][0]["done"] is True
    assert routes.update.call_count == 1


@pytest.mark.asyncio
async def test_a_pending_add_with_no_link_is_read_until_it_succeeds_before_the_remove() -> None:
    sent: list[str] = []

    def update(request: httpx.Request) -> httpx.Response:
        kind = json.loads(request.content)["properties"]["operationType"]
        sent.append(kind)
        return httpx.Response(200, json=_group_state("Updating" if kind == "Add" else "Succeeded"))

    def read(_: httpx.Request) -> httpx.Response:
        sent.append("GET")
        state = "Succeeded" if sent.count("GET") > 1 else "Updating"
        return httpx.Response(200, json=_group_state(state))

    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.mock(side_effect=update)
        routes.get.mock(side_effect=read)
        result = await _connector().migrate_group_update(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "add": ["m-1"], "remove": ["m-2"]}
        )

    # The Add answered Updating with no link: the group is read until it is
    # Succeeded, and only then is the Remove sent. The last GET is the read-back.
    assert sent == ["Add", "GET", "GET", "Remove", "GET"]
    assert result["steps"][0]["done"] is True
    assert result["steps"][1]["sent"] is True
    assert result["not_sent"] is None


@pytest.mark.asyncio
async def test_an_add_still_pending_when_the_wait_is_over_sends_no_remove() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.update.respond(200, headers={"Retry-After": "3600"}, json=_group_state("Updating"))
        result = await _connector().migrate_group_update(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "add": ["m-1"], "remove": ["m-2"]}
        )

    assert routes.update.call_count == 1
    assert result["steps"][0]["done"] is False
    assert "had not finished the resource" in result["steps"][0]["poll_error"]
    assert result["steps"][1] == {
        "operation_type": "Remove",
        "machines": 1,
        "sent": False,
        "done": False,
        "operation": None,
        "poll_error": None,
    }
    assert "Taking machines out was not sent" in result["not_sent"]


@pytest.mark.asyncio
async def test_a_pending_import_site_with_no_link_is_read_until_it_succeeds() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_put.respond(201, json=_state_of("Provisioning"))
        routes.site_get.mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_state_of("Provisioning")),
                httpx.Response(200, json=_state_of("Succeeded")),
            ]
        )
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "uploaded"
    assert routes.site_get.call_count == 3
    assert routes.collector.call_count == 1  # the collector came after the site ended


@pytest.mark.asyncio
async def test_a_pending_import_site_when_the_wait_is_over_goes_no_further() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_put.respond(
            201, headers={"Retry-After": "3600"}, json=_state_of("Provisioning")
        )
        with pytest.raises(AzureConflictError, match="the new import site"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.collector.call_count == 0
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_retry_that_finds_the_site_still_running_goes_no_further() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_get.respond(200, json={**_state_of("Running"), "tags": {}})
        with pytest.raises(AzureConflictError, match="the import site"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.site_put.call_count == 0
    assert routes.collector.call_count == 0
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_site_that_succeeded_is_used_as_it_is() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_get.respond(
            200, json={**_state_of("Succeeded"), "tags": {"owner": "example-team"}}
        )
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "uploaded"
    assert routes.site_get.call_count == 1


def _collector_answer(site: str = SITE_PATH, state: str = "Succeeded") -> dict[str, Any]:
    return {"properties": {"discoverySiteId": site, "provisioningState": state}}


@pytest.mark.asyncio
async def test_a_retry_after_a_slow_collector_write_finds_it_ready_and_sends_no_second_put() -> (
    None
):
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.collector.respond(201, headers=_PENDING, json={})
        routes.collector_get.mock(
            side_effect=[
                httpx.Response(404, json=_error(404).json()),
                httpx.Response(200, json=_collector_answer()),
            ]
        )
        with pytest.raises(AzureConflictError, match="the import collector"):
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert routes.collector.call_count == 1
    assert result["outcome"] == "uploaded"
    assert routes.upload.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        _collector_answer(state="Provisioning"),
        _collector_answer(site=f"{RG_PATH}/providers/Microsoft.OffAzure/importSites/other-site"),
        {"properties": {"provisioningState": "Succeeded"}},
        {},
    ],
    ids=["not-finished", "other-site", "no-site", "no-properties"],
)
async def test_a_collector_that_is_not_ready_for_this_site_is_written(
    answer: dict[str, Any],
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.collector_get.respond(200, json=answer)
        await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.collector.call_count == 1


@pytest.mark.asyncio
async def test_a_collector_that_cannot_be_read_is_written() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.collector_get.respond(403, json=_error(403, "AuthorizationFailed").json())
        await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    assert routes.collector.call_count == 1


_OPERATION_ONLY = {"Azure-AsyncOperation": _POLL}


@pytest.mark.asyncio
async def test_a_site_step_whose_status_cannot_be_read_keeps_its_reference() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.site_put.respond(201, headers=_OPERATION_ONLY, json={})
        mock.get(_POLL).respond(403, json=_error(403, "AuthorizationFailed").json())
        with pytest.raises(AzureConflictError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    text = str(caught.value)
    assert _POLL in text
    assert SITE_PATH in text
    assert "status could not be read" in text
    assert "azure.operation.status" in caught.value.remediation
    assert routes.collector.call_count == 0
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_a_collector_step_whose_status_cannot_be_read_keeps_its_own_reference() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.collector.respond(201, headers=_OPERATION_ONLY, json={})
        mock.get(_POLL).respond(403, json=_error(403, "AuthorizationFailed").json())
        with pytest.raises(AzureConflictError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    text = str(caught.value)
    assert _POLL in text
    assert COLLECTOR_PATH in text  # the collector's own resource id, not the site's
    assert "status could not be read" in text
    assert routes.import_uri.call_count == 0
    assert routes.upload.call_count == 0


_NOT_JSON = "<html>Example gateway page</html>"


@pytest.mark.asyncio
async def test_a_status_answer_that_is_not_json_is_a_failed_read_for_a_group_write() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Group(mock)
        routes.put.respond(201, headers=_OPERATION_ONLY, json=_group_answer())
        mock.get(_POLL).respond(200, content=_NOT_JSON.encode())
        result = await _connector().migrate_group_create(
            operator(), AzureTargetStub(), {**GROUP_PARAMS, "machines": ["m-1"]}
        )

    step = result["steps"][0]
    assert step["operation"]["poll_url"] == _POLL
    assert step["done"] is False
    assert "not JSON" in step["poll_error"]
    assert "gateway" not in step["poll_error"]
    assert routes.update.call_count == 0


@pytest.mark.asyncio
async def test_a_status_answer_that_is_not_json_is_a_failed_read_for_an_import_step() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock, tags={})
        routes.collector.respond(201, headers=_OPERATION_ONLY, json={})
        mock.get(_POLL).respond(200, content=_NOT_JSON.encode())
        with pytest.raises(AzureConflictError) as caught:
            await _connector().migrate_import_start(operator(), AzureTargetStub(), IMPORT_PARAMS)

    text = str(caught.value)
    assert _POLL in text
    assert "not JSON" in text
    assert "gateway" not in text
    assert routes.upload.call_count == 0


@pytest.mark.asyncio
async def test_an_upload_answer_of_408_is_unknown_so_the_record_stays() -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = _Import(mock)
        routes.upload.respond(408)
        result = await _connector().migrate_import_start(
            operator(), AzureTargetStub(), IMPORT_PARAMS
        )

    assert result["outcome"] == "upload_unconfirmed"
    assert routes.upload.call_count == 1
    assert _patch_bodies(routes.site_patch) == [_tag_patch("Merge", _tag())]
    assert result["job_name"] == JOB

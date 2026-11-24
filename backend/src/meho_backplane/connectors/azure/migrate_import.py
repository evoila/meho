# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Body of ``azure.migrate.import.start``: import a CSV file of servers into a project.

No Azure Migrate appliance is needed. How the import runs, and why a retry
never uploads the same file twice:

1. Read the project (its region). Make the import site when it is missing,
   and the import collector that links the project to the site (a ``PUT``,
   safe to repeat).
2. Look at the site's tags. MEHO writes one tag per uploaded file,
   ``meho-csv-<first 16 characters of the file's SHA-256>``, holding the job
   name. A ``repeat_key`` adds ``-<8 characters of its SHA-256>`` to the tag
   name, so an import of the same file on purpose has its own record, and a
   retry with the same key finds it. When the tag names a job that exists,
   the file was already sent, or its upload may still be under way: MEHO
   follows that job and uploads nothing. This holds also while the job still
   waits for its file, because the upload's outcome is not known then. When
   the tag names a job that Azure no longer has, the file may still have been
   imported: MEHO uploads nothing and asks for a new ``repeat_key``.
3. Ask the site for an upload link (``importUri``) and write the job name to
   the tag *before* the upload. If the tag cannot be written, nothing is
   uploaded. MEHO adds and removes only its own tag (the Tags API's
   ``Merge`` and ``Delete``), so the other tags of the site stay as they are.
4. Upload the file (:mod:`meho_backplane.connectors.azure.migrate_upload`).
   The link is a secret: MEHO's Azure token is never sent to it, and the link
   is in no result, error, log or audit text. A lost answer after the upload
   does not fail the call: MEHO follows the job to see whether the file
   arrived. The file is sent in one request, whatever the answer. When Azure
   Storage refuses the upload (any answer from 300 to 499), the file did not
   arrive: MEHO removes the tag, so the same call again uploads it.
5. Follow the job until it ends or MEHO's wait is over.

Steps 1 to 4 run under one PostgreSQL lock per import site, shared by every
MEHO worker, so the same file is never uploaded twice at once. A step of
the setup that Azure has not confirmed after MEHO's wait stops the call
before the upload. When the lock stays busy, MEHO sends nothing and
says so.

The audit row of a call holds a hash of the parameters, never the file. The
result names the file by its SHA-256, size and row count.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import (
    AzureApiError,
    AzureConflictError,
    AzureNotFoundError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import (
    MIGRATE_API_VERSION,
    MIGRATE_IMPORT_COLLECTOR_PATH,
    MIGRATE_PROJECT_PATH,
    OFFAZURE_API_VERSION,
    OFFAZURE_IMPORT_SITE_PATH,
    OFFAZURE_IMPORT_URI_PATH,
    RESOURCES_API_VERSION,
    TAGS_AT_SCOPE_PATH,
)
from meho_backplane.connectors.azure.migrate_csv import (
    REPEAT_KEY_PATTERN,
    PreparedCsv,
    prepare_csv,
)
from meho_backplane.connectors.azure.migrate_jobs import (
    WAITING_STATE,
    job_name_of,
    read_job,
    site_param,
    site_path,
)
from meho_backplane.connectors.azure.migrate_rows import project_params
from meho_backplane.connectors.azure.migrate_upload import upload_file, upload_host
from meho_backplane.connectors.azure.migrate_writes import (
    POLL_ERRORS,
    arm_write,
    lock_key,
    poll_error_text,
    poll_until,
)
from meho_backplane.connectors.azure.session import AzureTargetLike
from meho_backplane.db.advisory import advisory_lock

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["import_start_impl"]

_REPEAT_KEY_RE: Final[re.Pattern[str]] = re.compile(REPEAT_KEY_PATTERN)

#: How long a call waits for another import to the same site to finish its upload.
_LOCK_WAIT_SECONDS: Final[float] = 120.0


def _record_key(prepared: PreparedCsv, repeat_key: object) -> str:
    """The site tag that records this file, for this ``repeat_key`` (or none)."""
    if repeat_key is None:
        return prepared.tag_key
    if not isinstance(repeat_key, str) or not _REPEAT_KEY_RE.fullmatch(repeat_key):
        raise ValueError("repeat_key must be 1 to 64 letters, digits, dots, hyphens or _.")
    return f"{prepared.tag_key}-{hashlib.sha256(repeat_key.encode('utf-8')).hexdigest()[:8]}"


def _object_answer(response: httpx.Response) -> dict[str, Any] | None:
    """The JSON object of *response*, or ``None`` when its body is not one."""
    try:
        answer = response.json()
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


@dataclass
class _Import:
    """One import call: who runs it, where, and which file."""

    connector: AzureConnector
    operator: Operator
    target: AzureTargetLike
    project: Mapping[str, str]
    names: Mapping[str, str]
    prepared: PreparedCsv
    record_key: str

    def _path(self, template: str, **extra: str) -> str:
        return site_path(self.target, template, **extra)

    @property
    def site_id(self) -> str:
        return self._path(OFFAZURE_IMPORT_SITE_PATH, **self.names)

    async def ensure_site(self) -> dict[str, str]:
        """Make the import site when it is missing. Returns its tags.

        An existing site is not written again: a ``PUT`` would replace its
        tags, and the tags are where MEHO keeps the record of uploaded files.
        """
        found = await self.connector.arm_get(
            self.target,
            self._path(MIGRATE_PROJECT_PATH, **self.project),
            operator=self.operator,
            api_version=MIGRATE_API_VERSION,
        )
        location = found.get("location")
        if not isinstance(location, str) or not location:
            raise ValueError("Azure returned no region for the project. MEHO imported nothing.")
        try:
            site = await self.connector.arm_get(
                self.target,
                self.site_id,
                operator=self.operator,
                api_version=OFFAZURE_API_VERSION,
            )
        except AzureNotFoundError:
            await self._put(
                self.site_id,
                OFFAZURE_API_VERSION,
                {"location": location, "properties": {}},
                "the new import site",
            )
            return {}
        tags = site.get("tags")
        return {
            key: value
            for key, value in (tags.items() if isinstance(tags, dict) else ())
            if isinstance(key, str) and isinstance(value, str)
        }

    async def _put(self, path: str, api_version: str, body: dict[str, Any], what: str) -> None:
        """Send one ``PUT`` of the setup. Stops when Azure has not confirmed it."""
        outcome = await arm_write(
            self.connector,
            self.operator,
            self.target,
            "PUT",
            path,
            api_version,
            body,
            resource_id=self.site_id,
            repeatable=True,
        )
        if not outcome.done:
            raise AzureConflictError(
                f"Azure target {self.target.name!r}: Azure had not confirmed {what} after "
                f"MEHO's wait. MEHO uploaded nothing. {outcome.poll_error or ''}".rstrip(),
                http_status=409,
                azure_error_code=None,
                azure_message=None,
                remediation="Send the same call again in a few minutes.",
            )

    @property
    def collector(self) -> str:
        return f"{self.names['site']}-collector"

    async def ensure_collector(self) -> None:
        """Link the project to the site. A ``PUT`` with a fixed name, safe to repeat."""
        path = self._path(MIGRATE_IMPORT_COLLECTOR_PATH, collector=self.collector, **self.project)
        await self._put(
            path,
            MIGRATE_API_VERSION,
            {"properties": {"discoverySiteId": self.site_id}},
            "the import collector",
        )

    async def known_job(self, tags: Mapping[str, str]) -> dict[str, Any] | None:
        """The job the tags name for this file, or ``None`` when there is no record.

        A job that still waits for its file is followed too: its upload may
        still be under way, or its answer was lost. A recorded job that Azure
        no longer has is not a sign that the file never arrived, so MEHO
        keeps the record, uploads nothing and asks for a new ``repeat_key``.
        """
        job = job_name_of(tags.get(self.record_key))
        if job is None:
            return None
        try:
            return await read_job(self.connector, self.operator, self.target, self.names, job)
        except AzureNotFoundError:
            remediation = (
                "Call azure.migrate.job.list and azure.migrate.machine.list to see what was "
                "imported. To import the file again, send the call with a new repeat_key."
            )
            raise AzureConflictError(
                f"Azure target {self.target.name!r}: MEHO sent this file before as import job "
                f"{job!r}, and Azure no longer has that job. The file may have been imported. "
                f"MEHO uploaded nothing. {remediation}",
                http_status=409,
                azure_error_code=None,
                azure_message=None,
                remediation=remediation,
            ) from None

    async def _patch_record(self, operation: str, job: str) -> None:
        """Add (``Merge``) or remove (``Delete``) only this file's record tag.

        The Tags API changes the named tag alone, so a tag that someone
        changes at the same time, in MEHO or in Azure, stays as it is.
        """
        await send(
            self.connector,
            self.target,
            "PATCH",
            TAGS_AT_SCOPE_PATH.format(scope=self.site_id[1:]),
            operator=self.operator,
            params={"api-version": RESOURCES_API_VERSION},
            json={"operation": operation, "properties": {"tags": {self.record_key: job}}},
            repeatable=True,
        )

    async def send_file(self) -> tuple[str, bool]:
        """Get an upload link, record the job, upload. Returns the job and if Azure confirmed."""
        response = await send(
            self.connector,
            self.target,
            "POST",
            self._path(OFFAZURE_IMPORT_URI_PATH, **self.names),
            operator=self.operator,
            params={"api-version": OFFAZURE_API_VERSION},
            json={},
            repeatable=False,
        )
        answer = _object_answer(response)
        if answer is None:
            raise AzureUrlRefusedError(
                f"Azure target {self.target.name!r}: Azure's answer for the upload link is "
                "not a JSON object. MEHO uploaded nothing."
            )
        link = answer.get("uri")
        job = job_name_of(answer.get("jobArmId"))
        upload_host(self.target, link)  # refuse a bad link before anything is recorded
        if job is None or not isinstance(link, str):
            raise AzureUrlRefusedError(
                f"Azure target {self.target.name!r}: Azure returned no usable import job "
                "name. MEHO uploaded nothing."
            )
        # The record comes first: a retry after a lost answer finds the job and
        # does not upload again. When it cannot be written, nothing is uploaded.
        await self._patch_record("Merge", job)
        try:
            confirmed = await upload_file(
                self.connector, self.operator, self.target, link, self.prepared.content
            )
        except AzureApiError:
            # Azure Storage refused the file, so it did not arrive. Drop the
            # record, so the same call again uploads it. A failed drop only
            # means the retry follows the job that waits for its file.
            with contextlib.suppress(AzureApiError, httpx.HTTPError):
                await self._patch_record("Delete", job)
            raise
        return job, confirmed

    async def follow(self, job: str | None, outcome: str) -> dict[str, Any]:
        """Follow the job to its end and build the result. A failed read never hides the upload."""
        row: dict[str, Any] | None = None
        done = False
        poll_error: str | None = None
        if job is not None:
            try:
                polled, done = await poll_until(
                    lambda: read_job(self.connector, self.operator, self.target, self.names, job),
                    lambda value: bool(value["done"]),
                )
                row = polled
            except POLL_ERRORS as exc:
                poll_error = (
                    f"The file was sent but the job could not be read: {poll_error_text(exc)} "
                    "Call azure.migrate.job.list to see the job."
                )
        return {
            "site": self.names["site"],
            "site_id": self.site_id,
            "collector": self.collector,
            "outcome": outcome,
            "job_name": job,
            "state": row["state"] if row else None,
            "done": done,
            "csv": self.prepared.describe(),
            "rows_read": self.prepared.rows,
            "rows_imported": row["rows_imported"] if row else None,
            "error_count": row["error_count"] if row else None,
            "warning_count": row["warning_count"] if row else None,
            "bad_rows": row["bad_rows"] if row else [],
            "bad_rows_truncated": row["bad_rows_truncated"] if row else False,
            "poll_error": poll_error,
        }


async def import_start_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.import.start``: import a CSV file of servers into a project."""
    project = project_params(params)
    prepared = prepare_csv(params.get("csv"))
    run = _Import(
        connector,
        operator,
        target,
        project,
        {
            "resource_group": project["resource_group"],
            "site": site_param(params, project["project"]),
        },
        prepared,
        _record_key(prepared, params.get("repeat_key")),
    )
    async with advisory_lock(
        lock_key("import", run.site_id),
        subsystem="azure_migrate_import",
        wait_seconds=_LOCK_WAIT_SECONDS,
    ) as locked:
        if not locked:
            raise AzureConflictError(
                f"Azure target {target.name!r}: another import to site "
                f"{run.names['site']!r} is still uploading. MEHO sent nothing.",
                http_status=409,
                azure_error_code=None,
                azure_message=None,
                remediation="Send the same call again in a few minutes.",
            )
        tags = await run.ensure_site()
        await run.ensure_collector()
        known = await run.known_job(tags)
        if known is None:
            job, confirmed = await run.send_file()
            outcome = "uploaded" if confirmed else "upload_unconfirmed"
        else:
            job = known["name"]
            waiting = known["state"].casefold() == WAITING_STATE
            outcome = "earlier_upload_waiting" if waiting else "already_sent"
    return await run.follow(job, outcome)

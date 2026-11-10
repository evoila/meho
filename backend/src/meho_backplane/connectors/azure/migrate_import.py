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
   name. When the tag is there, the file was already sent: MEHO reads that
   job and follows it, and uploads nothing. ``allow_repeat: true`` skips this
   check.
3. Ask the site for an upload link (``importUri``) and write the job name to
   the tag *before* the upload. If the tag cannot be written, nothing is
   uploaded.
4. Upload the file (:mod:`meho_backplane.connectors.azure.migrate_upload`).
   The link is a secret: MEHO's Azure token is never sent to it, and the link
   is in no result, error, log or audit text. A lost answer after the upload
   does not fail the call: MEHO follows the job to see whether the file
   arrived.
5. Follow the job until it ends or MEHO's wait is over.

The audit row of a call holds a hash of the parameters, never the file. The
result names the file by its SHA-256, size and row count.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureNotFoundError, AzureUrlRefusedError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import (
    MIGRATE_API_VERSION,
    MIGRATE_IMPORT_COLLECTOR_PATH,
    MIGRATE_PROJECT_PATH,
    OFFAZURE_API_VERSION,
    OFFAZURE_IMPORT_SITE_PATH,
    OFFAZURE_IMPORT_URI_PATH,
)
from meho_backplane.connectors.azure.migrate_csv import PreparedCsv, prepare_csv
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
    poll_error_text,
    poll_until,
)
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["import_start_impl"]


@dataclass
class _Import:
    """One import call: who runs it, where, and which file."""

    connector: AzureConnector
    operator: Operator
    target: AzureTargetLike
    project: Mapping[str, str]
    names: Mapping[str, str]
    prepared: PreparedCsv

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
                self.site_id, OFFAZURE_API_VERSION, {"location": location, "properties": {}}
            )
            return {}
        tags = site.get("tags")
        return {
            key: value
            for key, value in (tags.items() if isinstance(tags, dict) else ())
            if isinstance(key, str) and isinstance(value, str)
        }

    async def _put(self, path: str, api_version: str, body: dict[str, Any]) -> None:
        await arm_write(
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

    @property
    def collector(self) -> str:
        return f"{self.names['site']}-collector"

    async def ensure_collector(self) -> None:
        """Link the project to the site. A ``PUT`` with a fixed name, safe to repeat."""
        path = self._path(MIGRATE_IMPORT_COLLECTOR_PATH, collector=self.collector, **self.project)
        await self._put(
            path, MIGRATE_API_VERSION, {"properties": {"discoverySiteId": self.site_id}}
        )

    async def known_job(self, tags: Mapping[str, str]) -> dict[str, Any] | None:
        """The job the tags name for this file, or ``None`` when there is none to follow.

        A job that still waits for its file is not followed: the file did not
        arrive, so the caller gets a new upload link.
        """
        job = job_name_of(tags.get(self.prepared.tag_key))
        if job is None:
            return None
        try:
            row = await read_job(self.connector, self.operator, self.target, self.names, job)
        except AzureNotFoundError:
            return None
        return None if row["state"].casefold() == WAITING_STATE else row

    async def send_file(self, tags: dict[str, str]) -> tuple[str, bool]:
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
        answer = json_payload_or_empty(response)
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
        await send(
            self.connector,
            self.target,
            "PATCH",
            self.site_id,
            operator=self.operator,
            params={"api-version": OFFAZURE_API_VERSION},
            json={"tags": {**tags, self.prepared.tag_key: job}},
            repeatable=True,
        )
        confirmed = await upload_file(
            self.connector, self.operator, self.target, link, self.prepared.content
        )
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
    run = _Import(
        connector,
        operator,
        target,
        project,
        {
            "resource_group": project["resource_group"],
            "site": site_param(params, project["project"]),
        },
        prepare_csv(params.get("csv")),
    )
    tags = await run.ensure_site()
    await run.ensure_collector()
    known = None if params.get("allow_repeat") is True else await run.known_job(tags)
    if known is not None:
        return await run.follow(known["name"], "already_sent")
    job, confirmed = await run.send_file(tags)
    return await run.follow(job, "uploaded" if confirmed else "upload_unconfirmed")

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Import sites and import jobs of Azure Migrate: names, job rows and the job list.

* :func:`default_site_name` and :func:`site_param` give the import site name.
  It is a function of the project name, so a retry finds the same site.
* :func:`job_row` turns Azure's answer for one import job into a row. Azure's
  answer holds the upload link of the job (``blobSasUri``); the row never does.
* :func:`job_list_impl` is ``azure.migrate.job.list``, a ``safe`` read.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import clean_azure_text
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    OFFAZURE_API_VERSION,
    OFFAZURE_IMPORT_JOB_PATH,
    OFFAZURE_IMPORT_JOBS_PATH,
)
from meho_backplane.connectors.azure.migrate_rows import migrate_name_param, resource_group_param
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "IMPORT_SITE_PATTERN",
    "JOB_DONE_STATES",
    "WAITING_STATE",
    "default_site_name",
    "job_list_impl",
    "job_name_of",
    "job_row",
    "read_job",
    "site_param",
    "site_path",
]

#: Azure's rule for an import site name and an import job name.
IMPORT_SITE_PATTERN: Final[str] = r"^[a-zA-Z0-9-]{3,24}$"
_SITE_RE: Final[re.Pattern[str]] = re.compile(IMPORT_SITE_PATTERN)

#: How a job ends (lower case). Any other state means it still runs, or is not known.
JOB_DONE_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "completedwithwarnings", "completedwitherrors", "failed"}
)

#: The state of a job that has not seen its file yet (lower case).
WAITING_STATE: Final[str] = "waitingforblobupload"

#: Most bad rows one result lists, and the longest text of one.
_MAX_BAD_ROWS: Final[int] = 100
_MAX_BAD_ROW_TEXT: Final[int] = 300


def default_site_name(project: str) -> str:
    """The import site name MEHO uses for *project* when the caller names none.

    At most 24 characters, as Azure requires.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", project.casefold()).strip("-")[:13].strip("-") or "project"
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-imp-{digest}"


def site_param(params: Mapping[str, Any], project: str) -> str:
    """The checked ``site`` parameter, or the default name for *project*."""
    value = params.get("site")
    if value is None:
        return default_site_name(project)
    if isinstance(value, str) and _SITE_RE.fullmatch(value):
        return value
    raise ValueError("site must be 3 to 24 letters, digits and hyphens.")


def job_name_of(value: object) -> str | None:
    """The job name at the end of a job ARM id, when it has Azure's shape."""
    if not isinstance(value, str):
        return None
    last = value.rstrip("/").rpartition("/")[2]
    return last if _SITE_RE.fullmatch(last) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bad_rows(summary: Mapping[str, Any]) -> tuple[list[dict[str, str]], int]:
    """The reasons Azure gave for rows it did not import, cleaned, and the count."""
    errors = summary.get("errors")
    reasons: list[dict[str, str]] = []
    for item in errors if isinstance(errors, list) else ():
        cleaned = clean_azure_text(item)
        if cleaned:
            reasons.append({"reason": cleaned[:_MAX_BAD_ROW_TEXT]})
    count = max(len(reasons), _count(summary.get("errorCount")) or 0)
    return reasons[:_MAX_BAD_ROWS], count


def job_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One import job. It holds no link: Azure's answer has the upload link, the row drops it."""
    raw = item.get("properties")
    props: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    summary = props.get("errorSummary")
    summary = summary if isinstance(summary, Mapping) else {}
    reasons, bad_count = _bad_rows(summary)
    state = _text(props.get("jobResult")) or _text(item.get("status")) or "Unknown"
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "state": state,
        "done": state.casefold() in JOB_DONE_STATES,
        "start_time": _text(item.get("startTime")),
        "end_time": _text(item.get("endTime")),
        "rows_imported": _count(props.get("numberOfMachinesImported")),
        "error_count": bad_count,
        "warning_count": _count(summary.get("warningCount")),
        "bad_rows": reasons,
        "bad_rows_truncated": bad_count > len(reasons),
    }


def site_path(target: AzureTargetLike, template: str, **names: str) -> str:
    """Fill *template* with the target's subscription and the checked *names*."""
    return template.format(subscription_id=resolve_target_config(target).subscription_id, **names)


async def read_job(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Mapping[str, str],
    job: str,
) -> dict[str, Any]:
    """Read one import job of the site in *names* (``resource_group``, ``site``)."""
    path = site_path(target, OFFAZURE_IMPORT_JOB_PATH, job=job, **names)
    payload = await connector.arm_get(
        target, path, operator=operator, api_version=OFFAZURE_API_VERSION
    )
    return job_row(payload)


async def job_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.job.list``: the import jobs of one import site, with state and counts."""
    names = {
        "resource_group": resource_group_param(params),
        "site": site_param(params, migrate_name_param(params, "project")),
    }
    paged = await list_all_pages(
        connector,
        target,
        site_path(target, OFFAZURE_IMPORT_JOBS_PATH, **names),
        operator=operator,
        api_version=OFFAZURE_API_VERSION,
    )
    rows = [job_row(item) for item in paged.rows if isinstance(item, Mapping)]
    items: list[dict[str, Any]] = []
    if paged.omission is not None:
        items.append(
            {
                "what": "import jobs",
                "reason": str(paged.omission.get("kind")),
                "detail": str(paged.omission.get("message")),
            }
        )
    state_counts: dict[str, int] = {}
    for row in rows:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    return {
        "site": names["site"],
        "value": rows,
        "state_counts": state_counts,
        "truncated": bool(items),
        "omissions": {"count": len(items), "items": items},
    }

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure Migrate writes and the import job list.

The operation table in
:mod:`meho_backplane.connectors.azure.migrate_write_ops` uses these schemas.
The names reuse the property schemas of the reads in
:mod:`meho_backplane.connectors.azure.migrate_schemas`, so a project, group or
assessment name is checked by the same pattern everywhere.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL
from meho_backplane.connectors.azure.migrate_csv import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    REPEAT_KEY_PATTERN,
)
from meho_backplane.connectors.azure.migrate_jobs import IMPORT_SITE_PATTERN
from meho_backplane.connectors.azure.migrate_schemas import (
    ASSESSMENT_LIST_RESPONSE,
    GROUP_LIST_RESPONSE,
    params_schema,
)
from meho_backplane.connectors.azure.migrate_writes import MAX_MACHINES_PER_CALL

__all__ = [
    "ASSESSMENT_CREATE_PARAMS",
    "ASSESSMENT_CREATE_RESPONSE",
    "CURRENCIES",
    "GROUP_CREATE_PARAMS",
    "GROUP_RESPONSE",
    "GROUP_UPDATE_PARAMS",
    "IMPORT_START_PARAMS",
    "IMPORT_START_RESPONSE",
    "JOB_LIST_PARAMS",
    "JOB_LIST_RESPONSE",
]

_NAMES = params_schema("resource_group", "project", "group", "assessment")["properties"]

#: The currencies Azure Migrate prices in.
CURRENCIES: Final[tuple[str, ...]] = (
    "USD", "DKK", "CAD", "IDR", "JPY", "KRW", "NZD", "NOK", "RUB", "SAR", "ZAR", "SEK",
    "TRY", "GBP", "MXN", "MYR", "INR", "HKD", "BRL", "TWD", "EUR", "CHF", "ARS", "AUD", "CNY",
)  # fmt: skip

_EXAMPLE_NAMES: Final[dict[str, str]] = {
    "resource_group": "rg-migrate-example",
    "project": "example-assessment-project",
    "group": "example-group",
    "assessment": "example-assessment",
}

_EXAMPLE_CSV: Final[str] = (
    "*Server name,*Cores,*Memory (In MB),*OS name\nexample-server-01,4,16384,Ubuntu Linux\n"
)

_SITE: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": IMPORT_SITE_PATTERN,
    "description": (
        "The import site to use, 3 to 24 letters, digits and hyphens, as "
        "azure.migrate.site.list shows an import site. Leave it out to use the site MEHO "
        "names from the project, which it makes when it is missing."
    ),
}


def _schema(
    names: tuple[str, ...],
    required: tuple[str, ...],
    example: dict[str, Any],
    **extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**{name: _NAMES[name] for name in names}, **extra},
        "required": list(required),
        "additionalProperties": False,
        "examples": [{**{name: _EXAMPLE_NAMES[name] for name in names}, **example}],
    }


def _machine_list(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": MAX_MACHINES_PER_CALL,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 260,
            "description": "One machine name, as azure.migrate.machine.list returns it (name).",
        },
        "description": description,
    }


IMPORT_START_PARAMS: Final[dict[str, Any]] = _schema(
    ("resource_group", "project"),
    ("resource_group", "project", "csv"),
    {"csv": _EXAMPLE_CSV},
    csv={
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_CSV_BYTES,
        "description": (
            "The text of the CSV file in Azure's import template: a header row, then one "
            f"row per server (at most {MAX_CSV_ROWS:,}). Required columns: server name, "
            "cores, memory in MB, OS name. MEHO uploads the file as it is."
        ),
    },
    site=_SITE,
    repeat_key={
        "type": "string",
        "pattern": REPEAT_KEY_PATTERN,
        "description": (
            "Set a new key to import a file again that MEHO already sent to this site, for "
            "example the date. Without it, the same file (same SHA-256) is not uploaded "
            "twice: MEHO follows the first job instead. MEHO keeps one record per file and "
            "key, so the same call with the same key after a lost answer uploads nothing "
            "twice either."
        ),
    },
)

JOB_LIST_PARAMS: Final[dict[str, Any]] = _schema(
    ("resource_group", "project"), ("resource_group", "project"), {}, site=_SITE
)

GROUP_CREATE_PARAMS: Final[dict[str, Any]] = _schema(
    ("resource_group", "project", "group"),
    ("resource_group", "project", "group"),
    {"machines": ["example-machine-name"]},
    machines=_machine_list("Machines to add to the group after it is made. Leave out for none."),
    group_type={
        "type": "string",
        "enum": ["Default", "Import"],
        "description": (
            "Default, or Import for a group of servers that came from a CSV import. "
            "Default is Default."
        ),
    },
)

GROUP_UPDATE_PARAMS: Final[dict[str, Any]] = _schema(
    ("resource_group", "project", "group"),
    ("resource_group", "project", "group"),
    {"add": ["example-machine-name"]},
    add=_machine_list("Machines to add to the group. A machine already in it stays."),
    remove=_machine_list("Machines to take out of the group. MEHO runs this after add."),
)

ASSESSMENT_CREATE_PARAMS: Final[dict[str, Any]] = _schema(
    ("resource_group", "project", "group", "assessment"),
    ("resource_group", "project", "group", "assessment", "region", "sizing"),
    {"region": "westeurope", "sizing": "as_on_premises"},
    region={
        "type": "string",
        "pattern": "^[a-z0-9]{2,40}$",
        "description": (
            "The Azure region to price in, in the form Azure uses (westeurope, "
            "germanywestcentral). azure.location.list shows the names."
        ),
    },
    sizing={
        "type": "string",
        "enum": ["as_on_premises", "performance"],
        "description": (
            "as_on_premises sizes each VM from the cores and memory of the server. "
            "performance sizes it from the use numbers. For servers from a CSV import "
            "both use only the numbers in the file."
        ),
    },
    currency={
        "type": "string",
        "enum": list(CURRENCIES),
        "description": "The currency of the costs. Default EUR.",
    },
    pricing={
        "type": "string",
        "enum": ["pay_as_you_go", "reserved_1_year", "reserved_3_year"],
        "description": "Pay as you go, or reserved for 1 or 3 years. Default pay_as_you_go.",
    },
    hybrid_benefit={
        "type": "boolean",
        "description": "True when the Windows licenses use Azure Hybrid Benefit. Default false.",
    },
    comfort_factor={
        "type": "number",
        "minimum": 1.0,
        "maximum": 2.0,
        "description": "Safety margin on the size, from 1.0 to 2.0. Default 1.0.",
    },
)

# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------


def _fields(**fields: tuple[Any, str]) -> dict[str, Any]:
    return {name: {"type": kind, "description": text} for name, (kind, text) in fields.items()}


_POLL_ERROR: Final[tuple[Any, str]] = (
    TEXT_OR_NULL,
    "Set when a read after the write failed. The write was sent; this says what was not read.",
)

_OPERATION: Final[tuple[Any, str]] = (
    ["object", "null"],
    "The reference of a long-running Azure operation, to read with azure.operation.status.",
)

_BAD_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": _fields(reason=("string", "Why Azure did not import the row.")),
}

IMPORT_START_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": _fields(
        site=("string", "The import site the file went to."),
        site_id=("string", "The resource ID of the import site."),
        collector=("string", "The import collector that links the project to the site."),
        outcome=(
            "string",
            "uploaded (MEHO sent the file), upload_unconfirmed (MEHO sent it but Azure's answer "
            "was lost) or already_sent (this file was sent before; MEHO followed that job).",
        ),
        job_name=(TEXT_OR_NULL, "The import job name."),
        state=(
            TEXT_OR_NULL,
            "The job state: Completed, CompletedWithWarnings, CompletedWithErrors, Failed, "
            "InProgress, WaitingForBlobUpload. Null when the job could not be read.",
        ),
        done=(
            "boolean",
            "True when the job ended. False: read it later with azure.migrate.job.list.",
        ),
        csv=(
            "object",
            "The file by its sha256, bytes and rows. The file itself is never in a result.",
        ),
        rows_read=("integer", "Server rows in the file MEHO sent."),
        rows_imported=(["integer", "null"], "Machines Azure imported. Null while unknown."),
        error_count=(["integer", "null"], "Rows Azure did not import."),
        warning_count=(["integer", "null"], "Rows Azure imported with a warning."),
        bad_rows=("array", "Azure's reasons for the rows it did not import, at most 100."),
        bad_rows_truncated=("boolean", "True when more bad rows exist than bad_rows lists."),
        poll_error=_POLL_ERROR,
    ),
    "required": ["site", "outcome", "done", "csv", "rows_read", "bad_rows"],
}
IMPORT_START_RESPONSE["properties"]["bad_rows"]["items"] = _BAD_ROW

_JOB_ROW: Final[dict[str, Any]] = _fields(
    name=(TEXT_OR_NULL, "The job name."),
    id=(TEXT_OR_NULL, "The resource ID of the job."),
    state=(
        "string",
        "Completed, CompletedWithWarnings, CompletedWithErrors, Failed, InProgress...",
    ),
    done=("boolean", "True when the job ended."),
    start_time=(TEXT_OR_NULL, "When the job started."),
    end_time=(TEXT_OR_NULL, "When the job ended."),
    rows_imported=(["integer", "null"], "Machines the job imported."),
    error_count=(["integer", "null"], "Rows the job did not import."),
    warning_count=(["integer", "null"], "Rows imported with a warning."),
    bad_rows=("array", "Azure's reasons for the bad rows, at most 100."),
    bad_rows_truncated=("boolean", "True when more bad rows exist than bad_rows lists."),
)
_JOB_ROW["bad_rows"]["items"] = _BAD_ROW

JOB_LIST_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "site": {"type": "string", "description": "The import site the jobs belong to."},
        "value": {
            "type": "array",
            "items": {"type": "object", "properties": _JOB_ROW},
            "description": "One row per import job. A long list comes back as a stored result.",
        },
        "state_counts": {
            "type": "object",
            "description": "Jobs per state.",
            "additionalProperties": {"type": "integer", "description": "Jobs in this state."},
        },
        "truncated": {"type": "boolean", "description": "True when rows are missing."},
        "omissions": GROUP_LIST_RESPONSE["properties"]["omissions"],
    },
    "required": ["site", "value", "truncated", "omissions"],
}

_STEP: Final[dict[str, Any]] = {
    "type": "object",
    "properties": _fields(
        step=("string", "group_put, when the step made the group."),
        operation_type=("string", "Add or Remove, when the step changed machines."),
        machines=("integer", "How many machines the step named."),
        done=("boolean", "False when Azure was still working after MEHO's wait."),
        operation=_OPERATION,
    ),
}

GROUP_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": _STEP, "description": "The writes that ran, in order."},
        "group": {
            **GROUP_LIST_RESPONSE["properties"]["value"]["items"],
            "type": ["object", "null"],
            "description": "The group as Azure reads it after the writes, or null.",
        },
        "poll_error": {"type": TEXT_OR_NULL, "description": _POLL_ERROR[1]},
    },
    "required": ["steps", "group", "poll_error"],
}

ASSESSMENT_CREATE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_fields(
            state=(
                "string",
                "created (MEHO made it) or existing (an assessment of this name and these "
                "settings was already there; MEHO followed it).",
            ),
            done=("boolean", "True when Azure has computed the assessment."),
            status=(TEXT_OR_NULL, "Azure's status: Created, Running, Completed, OutOfSync..."),
            machine_count=(["number", "null"], "Machines in the assessment."),
            readiness_summary=(
                "object",
                "Machines per readiness (Suitable, ConditionallySuitable, NotSuitable, ...).",
            ),
            operation=_OPERATION,
            poll_error=_POLL_ERROR,
        ),
        "assessment": {
            **ASSESSMENT_LIST_RESPONSE["properties"]["value"]["items"],
            "type": ["object", "null"],
            "description": "The assessment as Azure reads it, with its settings and monthly costs.",
        },
    },
    "required": ["state", "done", "status", "readiness_summary", "assessment"],
}

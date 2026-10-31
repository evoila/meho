# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema parts shared by the VCFA region and quota ops (:mod:`.infra_ops`)."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import field, list_of, obj

__all__ = [
    "CHANGE_OBJECTS",
    "CHANGE_SCALARS",
    "END",
    "GROUP",
    "JOB_ID_PARAM",
    "JOB_RULES",
    "K8S_NAME",
    "NAME",
    "PASSWORD_PROPS",
    "READ_COMMON",
    "READ_OBJECTS",
    "READ_SCALARS",
    "REF",
    "START_JOB",
    "STEP_ROWS",
    "URL",
    "WARNINGS",
    "change_response",
]

GROUP: Final = "vcfa-provider-infra"
#: A value that is not a whole line of text (``$`` alone also matches before ``\n``).
END: Final = "$(?!\n)"

# ---------------------------------------------------------------------------
# Shared schema parts
# ---------------------------------------------------------------------------

JOB_ID_PARAM: dict[str, Any] = {
    "job_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": f"^[A-Za-z0-9:._-]+{END}",
        "description": (
            "Optional: the job id a change returned (job.id). The read then also gives "
            "the job's state, progress and error."
        ),
    }
}

REF = {"name": field("string", "The name."), "id": field("string", "The id (a URN).")}

_JOB_STATE = field(
    "string",
    "The job state: queued, running, succeeded, failed, canceled, unknown or not_reachable.",
    enum=["queued", "running", "succeeded", "failed", "canceled", "unknown", "not_reachable"],
)

_READ_JOB = {
    **obj(
        "The job of job_id, read now; null without job_id.",
        {
            "id": field("string", "The job id."),
            "state": _JOB_STATE,
            "vendor_state": field(["string", "null"], "The vendor's own state word."),
            "progress": field(["integer", "null"], "The progress in percent, when given."),
            "error": field(["string", "null"], "The vendor's error text when the job failed."),
            "operation": field(["string", "null"], "What the job does."),
            "started_at": field(["string", "null"], "When the job started."),
            "ended_at": field(["string", "null"], "When the job ended."),
        },
    ),
    "type": ["object", "null"],
}

READ_COMMON = {
    "state": field(
        "string",
        "ok, not_found, or not_reachable (the system did not answer; not a failure).",
        enum=["ok", "not_found", "not_reachable"],
    ),
    "reachable": field("boolean", "False when VCF Automation did not answer."),
    "checked_at": field("string", "When MEHO read it (UTC, ISO 8601)."),
    "job": _READ_JOB,
    "guidance": field(["string", "null"], "What to do next, or null."),
}

READ_SCALARS = {"keys": ["state", "reachable", "checked_at", "guidance"]}
#: The job of a status read stays inline when a long list spills.
READ_OBJECTS = {"objects": {"job": ["id", "state", "vendor_state", "progress", "error"]}}

PASSWORD_PROPS: dict[str, Any] = {
    "password_secret_ref": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Vault KV-v2 path that holds the password, read under your identity. Never "
            "pass the password itself."
        ),
    },
    "password_secret_mount": {
        "type": "string",
        "minLength": 1,
        "description": "KV-v2 mount of password_secret_ref (default 'secret').",
    },
    "password_secret_key": {
        "type": "string",
        "minLength": 1,
        "description": "Field of the secret that holds the password (default 'password').",
    },
}

URL = {
    "type": "string",
    "pattern": f"^https://[^\\s/?#]+/?{END}",
    "maxLength": 512,
}
NAME = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": f"^[^\\n]+{END}"}
K8S_NAME = {
    "type": "string",
    "pattern": f"^[a-z0-9]([-a-z0-9]{{0,61}}[a-z0-9])?{END}",
}

STEP_ROWS = list_of(
    "Every step of the change in order, with what happened to it.",
    {
        "step": field("string", "The step name."),
        "result": field(
            "string",
            "done, started (a vendor job), skipped, failed or not_run.",
            enum=["done", "started", "skipped", "failed", "not_run"],
        ),
        "detail": field(["string", "null"], "A short note on the result."),
        "job_id": field(["string", "null"], "The vendor job id of a started step."),
        "http_status": field(["integer", "null"], "The HTTP status of a failed step."),
        "error": field(["string", "null"], "The vendor's error text of a failed step."),
    },
)

START_JOB = {
    **obj(
        "The vendor job the call started; null when it started none.",
        {
            "id": field(["string", "null"], "The job id; null when the vendor gave none."),
            "href": field(["string", "null"], "The vendor's link to the job."),
            "status_read": field("string", "The read to call until the job ends."),
        },
    ),
    "type": ["object", "null"],
}


def change_response(statuses: list[str], extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": field("string", "What the call did.", enum=statuses),
            "object": {
                **obj(
                    "The object of the call.",
                    {
                        "kind": field("string", "The kind of object."),
                        "name": field("string", "Its name."),
                        "id": field(["string", "null"], "Its id; null until the vendor made it."),
                    },
                ),
                "type": ["object", "null"],
            },
            "job": START_JOB,
            "steps": STEP_ROWS,
            "checked_at": field("string", "When MEHO answered (UTC, ISO 8601)."),
            "guidance": field("string", "What to do next."),
            **extra,
        },
        "required": ["status", "job", "steps", "guidance"],
        "additionalProperties": True,
    }


CHANGE_SCALARS = {"keys": ["status", "checked_at", "guidance"]}
#: The parts of a change answer that stay inline when a long list spills.
CHANGE_OBJECTS = {
    "objects": {
        "object": ["kind", "name", "id", "state"],
        "job": ["id", "href", "status_read"],
    }
}
WARNINGS = {
    "warnings": {
        "type": "array",
        "description": "Warnings, for example a quota that namespaces cannot use yet.",
        "items": {"type": "string"},
    }
}
JOB_RULES = (
    "Use async: true for this call, so a lost reply does not lose the job id. After a "
    "lost reply, do not call again blindly: read the status read first."
)

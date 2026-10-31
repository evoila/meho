# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema helpers and shared schema parts of the integration ops.

:mod:`.vcfa_integration_ops` builds the five ops from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "ASYNC_HINT",
    "INTEGRATION",
    "JOB",
    "SECRET_REF",
    "STEPS",
    "TRACKER",
    "WAIT",
    "IntegrationOpSpec",
    "field",
    "obj",
    "render",
    "rows",
    "strings",
]

NS: Final[str] = "{ns}"


@dataclass(frozen=True)
class IntegrationOpSpec:
    """One op before a connector gives it an id prefix and a group key."""

    name: str
    handler_attr: str
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any]


def render(value: Any, ns: str) -> Any:
    """Put the op id prefix *ns* in for ``{ns}`` in every string of *value*."""
    if isinstance(value, str):
        return value.replace(NS, ns)
    if isinstance(value, dict):
        return {key: render(item, ns) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, ns) for item in value]
    if isinstance(value, tuple):
        return tuple(render(item, ns) for item in value)
    return value


def field(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": description, **extra}


def obj(description: str, properties: dict[str, Any], *, nullable: bool = False) -> dict[str, Any]:
    return {
        "type": ["object", "null"] if nullable else "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def rows(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": properties, "additionalProperties": True},
    }


def strings(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": {"type": "string"},
    }


WAIT = field(
    "integer",
    "How long MEHO reads the vendor's request tracker before it answers 'unfinished' "
    "(seconds, default 300).",
    minimum=10,
    maximum=1800,
)

STEPS = rows(
    "The steps of the change, in order: what ran, what failed (with the vendor's error) "
    "and what did not run.",
    {
        "step": field("string", "The step name."),
        "result": field(
            "string", "What happened to the step.", enum=["done", "failed", "unknown", "not_run"]
        ),
        "error": field(["string", "null"], "The error text of a failed or unknown step."),
    },
)

TRACKER = obj(
    "The vendor's request tracker of a step.",
    {
        "id": field(["string", "null"], "The tracker id; read it with request_tracker_id."),
        "status": field(
            ["string", "null"],
            "FINISHED, INPROGRESS or FAILED; not_reachable when the system stopped answering.",
        ),
        "progress": field(["integer", "null"], "Percent done."),
        "message": field(
            ["string", "null"], "The vendor's message (the error text when it failed)."
        ),
    },
    nullable=True,
)

INTEGRATION = obj(
    "One integration. No password, token or private key.",
    {
        "id": field(["string", "null"], "The integration id."),
        "name": field(["string", "null"], "The integration name."),
        "integration_type": field(
            ["string", "null"], "The vendor's type: vro, ipam, com.github.saas, ..."
        ),
        "state": field(["string", "null"], "The vendor's state, when it gives one."),
        "has_certificate": field("boolean", "True when a certificate is stored."),
        "integration_properties": field(
            "object", "The properties without secrets and certificate."
        ),
    },
    nullable=True,
)

JOB = obj(
    "The state of one vendor job (a Git sync or a request tracker).",
    {
        "kind": field("string", "git_sync or request_tracker."),
        "job_id": field(["string", "null"], "The vendor's job id."),
        "state": field(
            ["string", "null"],
            "The vendor's state (REQUESTED, PROCESSING, COMPLETED, FAILED, ...), or "
            "not_reachable or not_found.",
        ),
        "progress": field(["integer", "null"], "Percent done, when the vendor gives it."),
        "message": field(
            ["string", "null"], "The vendor's message; the error text when it failed."
        ),
        "done": field("boolean", "True when the job has ended."),
        "checked_at": field("string", "When MEHO read the state (ISO 8601, UTC)."),
    },
    nullable=True,
)

SECRET_REF = {
    "password_secret_ref": field(
        "string",
        "The Vault path of the password or token (never the value). MEHO reads it when the "
        "approved call runs and sends it only to the vendor.",
        minLength=1,
    ),
    "password_secret_mount": field(
        "string", "The Vault KV mount of the secret (default 'secret')."
    ),
    "password_secret_key": field(
        "string", "The key in the secret that holds it (default 'password')."
    ),
}

ASYNC_HINT = (
    "Call it with async: true: MEHO answers 202 with a run handle at once and stores the "
    "final answer under it, so a lost reply loses nothing."
)

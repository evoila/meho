# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The privacy-bounded shape of one request usage event (#3734).

A request usage event says "this principal in this tenant used this route
or tool, and it ended like this". It is telemetry about a transport
boundary. It is not an accountable action, so it lives outside
``audit_log``.

The schema allows only:

* server-stamped opaque ids (event, tenant, principal, session, run,
  request, and an optional correlation id),
* the transport (``rest`` or ``mcp``),
* an enumerated route or tool (checked against a :class:`RouteCatalog`),
* the duration, and
* a bounded outcome code.

Anything else is rejected (``extra="forbid"``): request or response
bodies, token values, query strings, tool arguments, prompts, target
names, and any claim that an agent followed the context it was given.
An authorization failure is only the ``denied`` outcome code; the reason
is never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, Self, get_args
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "CATALOG_CONTEXT_KEY",
    "EVENT_KINDS",
    "OUTCOMES",
    "TRANSPORTS",
    "UNMATCHED_ROUTE",
    "EventKind",
    "Outcome",
    "RequestUsageEventData",
    "RouteCatalog",
    "Transport",
    "outcome_for_status",
]

EventKind = Literal["request_finished", "context_selected"]
Transport = Literal["rest", "mcp"]
Outcome = Literal["ok", "client_error", "denied", "not_found", "rate_limited", "server_error"]

EVENT_KINDS: Final[tuple[str, ...]] = get_args(EventKind)
TRANSPORTS: Final[tuple[str, ...]] = get_args(Transport)
OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)

#: Route label for a request that matched no known route or tool. Used
#: instead of the raw path so a concrete path (which can carry a target
#: name or other free text) is never stored.
UNMATCHED_ROUTE: Final[str] = "<unmatched>"

#: Key under which callers pass the :class:`RouteCatalog` in the Pydantic
#: validation context.
CATALOG_CONTEXT_KEY: Final[str] = "route_catalog"

#: A JSON Web Token is three base64url parts joined by dots, and its
#: header always starts with ``eyJ`` (base64 of ``{"``).
_JWT_SHAPE: Final[re.Pattern[str]] = re.compile(r"eyJ[\w-]*\.[\w-]+\.[\w-]*")


@dataclass(frozen=True, slots=True)
class RouteCatalog:
    """The closed set of route labels an event may carry.

    * ``rest`` -- route templates of the running app, for example
      ``/api/v1/targets/{target_id}``. Never a concrete path.
    * ``mcp`` -- ``tool:<name>``, ``resource:<uri template>`` and
      ``method:<json-rpc method>`` for every registered MCP tool,
      resource template and JSON-RPC method.

    :data:`UNMATCHED_ROUTE` is always allowed.
    """

    rest: frozenset[str]
    mcp: frozenset[str]

    def allows(self, transport: str, route: str) -> bool:
        """Return whether *route* is a known label for *transport*."""
        if route == UNMATCHED_ROUTE:
            return True
        known = self.rest if transport == "rest" else self.mcp
        return route in known


def outcome_for_status(status_code: int) -> Outcome:
    """Map an HTTP status (or the MCP audit projection of one) to an outcome.

    ``401`` and ``403`` both become ``denied``: the event records that
    access was refused, never why. A status of ``0`` means no response
    started (the handler crashed), so it counts as ``server_error``.
    """
    if status_code in (401, 403):
        return "denied"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500 or status_code < 100:
        return "server_error"
    return "ok"


class RequestUsageEventData(BaseModel):
    """One validated usage event, ready to store.

    Build it with ``model_validate(data, context={CATALOG_CONTEXT_KEY:
    catalog})``. Without a catalog the route check fails, so an event
    can never be stored with an unchecked route.

    Rules per kind:

    * ``request_finished`` -- needs ``outcome`` and ``duration_ms``; has
      no ``correlation_id``.
    * ``context_selected`` -- needs ``correlation_id``; has no outcome
      and no duration. It only says that context was picked for this
      request. It does not say the agent used or followed it, and the
      schema has no field to say so.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    kind: EventKind
    occurred_at: AwareDatetime
    tenant_id: UUID
    principal_sub: str = Field(min_length=1, max_length=255)
    session_id: UUID | None = None
    run_id: UUID | None = None
    request_id: UUID | None = None
    correlation_id: UUID | None = None
    transport: Transport
    route: str = Field(min_length=1, max_length=512)
    outcome: Outcome | None = None
    duration_ms: float | None = Field(default=None, ge=0)

    @field_validator("principal_sub")
    @classmethod
    def _principal_is_opaque_id(cls, value: str) -> str:
        """Reject a principal value that is not a plain opaque id.

        A subject id has no spaces. A value that looks like a token (or
        a ``Bearer`` header) means a caller passed the wrong thing, and
        storing it would leak a credential.
        """
        if any(char.isspace() for char in value):
            raise ValueError("principal_sub must be an opaque id without spaces")
        if _JWT_SHAPE.search(value):
            raise ValueError("principal_sub must not hold a token")
        return value

    @field_validator("route")
    @classmethod
    def _route_is_enumerated(cls, value: str, info: ValidationInfo) -> str:
        """Reject a route that is not in the route catalog.

        The transport is validated first (field order), so it is
        available here. A missing catalog is a caller bug and fails.
        """
        context = info.context or {}
        catalog = context.get(CATALOG_CONTEXT_KEY)
        if not isinstance(catalog, RouteCatalog):
            raise ValueError("route cannot be checked: no route catalog given")
        transport = info.data.get("transport")
        if not isinstance(transport, str) or not catalog.allows(transport, value):
            raise ValueError("route is not a known route or tool")
        return value

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        """Enforce the per-kind field rules in the class docstring."""
        if self.kind == "request_finished":
            if self.outcome is None or self.duration_ms is None:
                raise ValueError("request_finished needs outcome and duration_ms")
            if self.correlation_id is not None:
                raise ValueError("request_finished has no correlation_id")
        else:
            if self.correlation_id is None:
                raise ValueError("context_selected needs a correlation_id")
            if self.outcome is not None or self.duration_ms is not None:
                raise ValueError("context_selected has no outcome or duration")
        return self

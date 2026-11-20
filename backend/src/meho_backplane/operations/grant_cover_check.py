# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An op's check, by its params, of whether a grant may clear one call.

A standing grant and a run approval cover an operation, a connector and a
target. They do not read the params of the call. Some operations need a
person's approval for some calls only, for example a Keycloak write in
MEHO's own login realm. Such an operation registers a check with
:func:`register_grant_cover_check`. The check gets the same
:class:`~meho_backplane.operations._preview.PreviewContext` the preview
hooks get and answers ``True`` when no grant may clear this call.

:func:`grant_cover_refusal` runs it in the policy gate, right before a
standing grant or a run approval is looked up
(``_validate._service_grant_verdict``). When it answers ``True``, no grant
is looked up and the call waits for a person's approval. The check is
fail-closed: a call without params, and a check that raises, also wait.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.operations._preview import PreviewContext

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "GRANT_COVER_REFUSAL",
    "GrantCoverCheck",
    "grant_cover_refusal",
    "register_grant_cover_check",
]

_log = structlog.get_logger(__name__)

#: ``True`` when no standing grant and no run approval may clear the call.
GrantCoverCheck = Callable[[PreviewContext], Awaitable[bool]]

#: The note on the wait reason when the check says no.
GRANT_COVER_REFUSAL: Final[str] = (
    "no standing grant and no run approval covers this call; a person approves it"
)

_CHECKS: dict[str, GrantCoverCheck] = {}


def register_grant_cover_check(op_id: str, check: GrantCoverCheck) -> None:
    """Register *check* for *op_id*. Registering again overwrites it."""
    _CHECKS[op_id] = check


async def grant_cover_refusal(
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
    params: dict[str, Any] | None,
) -> str | None:
    """:data:`GRANT_COVER_REFUSAL` when the op's check says no grant may clear this call.

    ``None`` when the op has no check or the check lets a grant clear it.
    Resolves the connector the same way the park-time preview does.
    """
    check = _CHECKS.get(descriptor.op_id)
    if check is None:
        return None
    if params is None:
        return GRANT_COVER_REFUSAL
    # Imported lazily: the dispatcher imports the policy gate.
    from meho_backplane.operations.dispatcher import _resolve_connector_instance

    try:
        connector_instance, resolution_error, _ = await _resolve_connector_instance(
            descriptor, target
        )
    except Exception:
        connector_instance, resolution_error = None, None
    if resolution_error is not None:
        connector_instance = None
    ctx = PreviewContext(
        descriptor=descriptor,
        connector_instance=connector_instance,
        operator=operator,
        target=target,
        params=params,
        connector_id=connector_id,
    )
    try:
        refused = await check(ctx)
    except Exception as exc:
        _log.warning(
            "grant_cover_check_failed", op_id=descriptor.op_id, error_type=type(exc).__name__
        )
        refused = True
    return GRANT_COVER_REFUSAL if refused else None

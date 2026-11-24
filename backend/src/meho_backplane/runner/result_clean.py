# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Clean secret values from a runner result before it is kept anywhere.

The runner and the central MEHO both call :func:`clean_result`: the runner
before it sends a result or writes it to its retry file, the central MEHO
before it stores the result. The answer gets the same cleanup as the
answer of a central check: the connector boundary redaction, then the
default view that hides secret-named fields. The error text is redacted and
shortened like the error text of a central call. No database is needed.
"""

from __future__ import annotations

from meho_backplane.operations._errors import _sanitize_free_text
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.runner.wire import RunnerResult

__all__ = ["clean_error_text", "clean_result"]


def clean_error_text(text: str) -> str:
    """*text* redacted and shortened like the error text of a central call."""
    return _sanitize_free_text(text)


def clean_result(
    item: RunnerResult,
    *,
    connector_id: str | None,
    tenant: str | None,
) -> RunnerResult:
    """Return *item* with secret values taken out of its answer and error text."""
    update: dict[str, object] = {}
    if item.result is not None:
        redacted = apply_connector_boundary_redaction(
            item.result, connector_id=connector_id, tenant=tenant, op=item.op_id
        ).redacted
        update["result"] = hide_secret_values(item.op_id, redacted, raw=item.result)
    if item.error:
        update["error"] = clean_error_text(item.error)
    return item.model_copy(update=update) if update else item

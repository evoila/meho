# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared structured-error envelope for a runbook-template hydration failure.

``meho.runbook.show_template`` (REST and MCP) re-validates the stored
``runbook_templates.steps`` JSONB back through
:class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody` on every
read (:func:`~meho_backplane.runbooks.service._steps_from_storage`). A row
that reached storage before a schema tightening -- the #2122
``min_length=1`` non-empty-body constraint is the known case (Task #2239)
-- fails that re-validation with a :class:`pydantic.ValidationError`.

Before #2239 that error leaked out of both transports as an opaque fault:
the REST route had no ``ValidationError`` handler, so Starlette rendered a
bare ``text/plain`` 500; the MCP dispatcher's catch-all flattened it to
``-32603 "internal error: ValidationError"`` with no ``data``. This
module is the single builder both transports call so the envelope they
emit cannot drift -- the same shared-builder posture the connector-ingest
envelopes use (:mod:`meho_backplane.operations.ingest.error_envelopes`,
G0.9.1-T5 #777) and the convention codified in
``docs/codebase/error-message-shape.md``.

The envelope follows that convention: a stable ``snake_case`` code
(:data:`TEMPLATE_BODY_VALIDATION_FAILED`), a human-readable ``message``
naming the offending row + the remediation + the doc reference, and the
machine-actionable ``errors`` list an agent can branch on. REST embeds
the dict in ``HTTPException.detail`` (body ``{"detail": {...}}``); MCP
embeds it in the JSON-RPC ``error.data`` member. The migration
``0054_backfill_empty_runbook_step_bodies`` is the durable fix that
removes the offending data; this envelope is the diagnosable surface for
any row that predates it or is otherwise malformed.
"""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError

__all__ = [
    "TEMPLATE_BODY_VALIDATION_FAILED",
    "build_template_body_validation_detail",
]

#: Stable machine-readable code for a stored-template hydration failure.
#: Renaming it is a breaking API change (``docs/codebase/error-message-shape.md``
#: §"A short code"); adding it is additive.
TEMPLATE_BODY_VALIDATION_FAILED: Final[str] = "template_body_validation_failed"

#: Doc the ``message`` points operators at for the why + the remediation.
_DOCS_REF: Final[str] = "docs/codebase/runbook-template-hydration.md"


def _version_clause(version: int | None) -> str:
    """Render the version for the human message (``v3`` / ``(latest version)``)."""
    return f"v{version}" if version is not None else "(latest version)"


def build_template_body_validation_detail(
    *,
    slug: str,
    version: int | None,
    exc: ValidationError,
) -> dict[str, object]:
    """Build the structured envelope for a template-hydration ``ValidationError``.

    Args:
        slug: The template slug the read targeted (the operator's own
            value -- not infrastructure topology, safe to echo per the
            info-leak boundary in ``docs/codebase/error-message-shape.md``).
        version: The requested version, or ``None`` when the latest was
            requested (the resolved-latest number is not surfaced -- the
            caller asked for "latest", and re-deriving it would need a
            second query off the read path).
        exc: The re-validation failure raised while hydrating the stored
            ``steps``.

    Returns:
        A JSON-safe dict with the stable ``error`` code, the ``slug`` /
        ``version`` coordinates, a compact ``errors`` list (each
        ``{"type", "loc", "msg"}`` -- URL / ctx / input stripped so no
        non-serialisable object rides the envelope), and a
        three-clause ``message``. Both transports serialise this verbatim.
    """
    errors = [
        {"type": entry["type"], "loc": list(entry["loc"]), "msg": entry["msg"]}
        for entry in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    ]
    message = (
        f"{TEMPLATE_BODY_VALIDATION_FAILED}: stored runbook template {slug!r} "
        f"{_version_clause(version)} has step content that no longer satisfies "
        f"the template schema (an empty or whitespace-only step body predating "
        f"the v0.20.0 non-empty-body requirement is the known cause). Apply "
        f"Alembic migration 0054 to backfill legacy rows, or re-save the body "
        f"via meho.runbook.edit_template / PATCH /api/v1/runbooks/templates/{slug}. "
        f"See {_DOCS_REF}."
    )
    return {
        "error": TEMPLATE_BODY_VALIDATION_FAILED,
        "slug": slug,
        "version": version,
        "errors": errors,
        "message": message,
    }

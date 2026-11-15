"""Clean the ticket id (work ref) of a console form with the one shared rule.

The rule is :func:`meho_backplane.work_ref.normalize_work_ref`. A blank
value means "no ticket"; a value that breaks the rule is refused with a 422
that names the rule.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from meho_backplane.work_ref import WorkRefError, normalize_work_ref

__all__ = ["form_work_ref"]


def form_work_ref(raw: str | None) -> str | None:
    """Return the cleaned ticket id of a form field, ``None`` when it is blank."""
    if raw is None or not raw.strip():
        return None
    try:
        return normalize_work_ref(raw)
    except WorkRefError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

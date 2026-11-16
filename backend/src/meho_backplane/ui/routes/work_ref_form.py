"""Clean the ticket id (work ref) of a console form with the one shared rule.

The rule is :func:`meho_backplane.work_ref.normalize_work_ref`. A blank
value means "no ticket" (or "no filter" in a search box); a value that breaks
the rule is refused with a 422 that names the rule.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from meho_backplane.work_ref import (
    WorkRefError,
    check_work_ref_filter_input,
    normalize_work_ref,
)

__all__ = ["filter_work_ref", "form_work_ref"]


def _refused(exc: WorkRefError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def form_work_ref(raw: str | None) -> str | None:
    """Return the cleaned ticket id of a form field, ``None`` when it is blank."""
    if raw is None or not raw.strip():
        return None
    try:
        return normalize_work_ref(raw)
    except WorkRefError as exc:
        raise _refused(exc) from exc


def filter_work_ref(raw: str | None) -> str | None:
    """Check a ticket id search box with the rule of the REST filters.

    A blank box means "no filter" and gives ``None``. A good value is
    returned as typed, so the search also finds rows stored before the
    rule (:func:`meho_backplane.work_ref.work_ref_filter_values`). The
    length is counted after cleaning, as on REST.
    """
    if raw is None or not raw.strip():
        return None
    try:
        checked: str = check_work_ref_filter_input(raw)
    except WorkRefError as exc:
        raise _refused(exc) from exc
    return checked

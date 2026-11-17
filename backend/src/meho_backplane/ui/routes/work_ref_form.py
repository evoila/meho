"""Clean the ticket id (work ref) of a console form with the one shared rule.

The rule is :func:`meho_backplane.work_ref.normalize_work_ref`. A blank
value means "no ticket" (or "no filter" in a search box); a value that breaks
the rule is refused with a 422 that names the rule.

A search box that refuses its value still answers with its page or list,
with the rule text in it (:func:`refused_filter_text`) and no rows. The answer
is a 422 with the header :data:`WORK_REF_REFUSED_HEADER`, and the console
script ``work-ref-refused.js`` swaps it into the page like a good answer. So a
bookmarked page and a search-as-you-type box both show why the value was
refused.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, Response, status

from meho_backplane.work_ref import (
    WorkRefError,
    check_work_ref_filter_input,
    normalize_work_ref,
)

__all__ = [
    "WORK_REF_REFUSED_HEADER",
    "filter_work_ref",
    "form_work_ref",
    "mark_work_ref_refused",
    "refused_filter_text",
]

#: Header of a 422 answer that shows a refused ticket id search value.
WORK_REF_REFUSED_HEADER: Final[str] = "Meho-Work-Ref-Refused"


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
    rule whose text follows the rule
    (:func:`meho_backplane.work_ref.work_ref_filter_values`). The
    length is counted after cleaning, as on REST. A value that breaks the
    rule raises :class:`WorkRefError`; the page shows
    :func:`refused_filter_text` and answers with :func:`mark_work_ref_refused`.
    """
    if raw is None or not raw.strip():
        return None
    checked: str = check_work_ref_filter_input(raw)
    return checked


def refused_filter_text(exc: WorkRefError) -> str:
    """The text a page shows for a refused ticket id search value."""
    return f"MEHO did not search. The ticket id is refused: {exc}."


def mark_work_ref_refused(response: Response) -> Response:
    """Make *response* the 422 answer of a refused ticket id search value."""
    response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    response.headers[WORK_REF_REFUSED_HEADER] = "1"
    return response

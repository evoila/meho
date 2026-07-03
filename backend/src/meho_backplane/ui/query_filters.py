# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared query-parameter coercion for ``/ui/*`` HTMX filter controls.

The operator-console list views (runbook runs, runbook template catalog,
scheduler, agent runs) render a filter ``<select>`` whose default/"All"
option carries ``value=""``. HTMX submits that control's value verbatim,
so picking "All" (or clearing a filter) sends ``?status=`` -- an empty
string -- on the request.

The matching FastAPI handlers type those filter params as
``Literal[...] | None`` / ``StrEnum | None`` ``Query`` params. An empty
string is neither a member of the literal/enum **nor** ``None``, so it
fails validation and the request 422s. HTMX does not swap a 4xx
response, so the control silently no-ops: the list never refreshes to
the unfiltered view. The ``hx-include`` cross-wiring between sibling
filters means the empty value is resubmitted on ordinary filter use,
not just when "All" is clicked.

:data:`EMPTY_STR_TO_NONE` is a :class:`pydantic.BeforeValidator` that
maps the empty string to ``None`` **before** the literal/enum check
runs, so "All"/cleared resolves to the no-filter sentinel and returns
200 with the unfiltered fragment. A genuinely out-of-vocabulary value
(e.g. ``?status=bogus``) is left untouched and still 422s at the HTTP
boundary -- the rejection contract callers rely on is preserved.

Usage -- annotate each ``Literal[...] | None`` / ``StrEnum | None``
filter ``Query`` param. The ``Query()`` marker MUST sit **inside**
``Annotated`` (alongside the validator), not as the parameter default:
with ``from __future__ import annotations`` in force across these
modules, FastAPI evaluates the stringised annotation via
``get_type_hints(..., include_extras=True)`` and only recovers the
``BeforeValidator`` when ``Query()`` is an ``Annotated`` member -- the
legacy ``= Query(default=None)`` default-sentinel form silently drops
the extra and the empty string keeps 422-ing::

    from typing import Annotated

    from fastapi import Query

    from meho_backplane.ui.query_filters import EMPTY_STR_TO_NONE

    status: Annotated[_RunStateFilter | None, EMPTY_STR_TO_NONE, Query()] = None

``str | None`` filter params never 422 on the empty string (it is a
valid ``str``) -- but they still need the coercion whenever the value
flows into an exact-match SQL filter, where ``""`` silently becomes
``WHERE col = ''`` and matches nothing (the runbook catalog's
``target_kind``, the topology table's ``kind``). For those, pin any
``max_length`` guard on the inner ``str`` branch via
:class:`pydantic.StringConstraints` -- a bare ``Query(max_length=...)``
on the nullable field raises ``TypeError`` when the validator produces
``None``::

    kind: Annotated[
        Annotated[str, StringConstraints(max_length=64)] | None,
        EMPTY_STR_TO_NONE,
        Query(),
    ] = None

Existing surfaces that use an in-vocabulary ``all`` sentinel enum member
(e.g. the connector-registry ``ConnectorStatusFilter`` -- ``value="all"``,
never ``value=""``) are an alternative encoding of the same idea and do
not use this validator.

Verified against the pinned stack (Pydantic 2.13.4, FastAPI 0.136.3):
``"" -> None``, an in-vocabulary value passes unchanged, ``None -> None``,
and an out-of-vocabulary value still raises ``ValidationError`` (422).
"""

from __future__ import annotations

from pydantic import BeforeValidator

__all__ = ["EMPTY_STR_TO_NONE", "empty_str_to_none"]


def empty_str_to_none(value: object) -> object:
    """Coerce the empty string to ``None``; pass everything else through.

    Runs as a ``BeforeValidator`` -- ahead of the literal/enum check -- so
    the "All"/cleared filter value (``""``) becomes the no-filter sentinel
    ``None`` instead of failing validation, while a non-empty value
    (including an out-of-vocabulary one that must still 422) is handed to
    the wrapped type unchanged.
    """
    return None if value == "" else value


EMPTY_STR_TO_NONE = BeforeValidator(empty_str_to_none)
"""Reusable annotation that maps ``""`` to ``None`` before literal/enum validation."""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared typed-exception → ``HTTPException`` mapping for the ``api/v1`` routes.

The runbook routes (and, in time, any other route that wants the
property) raise a small typed-exception vocabulary; this module turns a
caught exception into the canonical :class:`HTTPException` the route
re-raises.

The load-bearing reason this is a *shared* module rather than a
per-route helper: **the 422 body shape has to match the OpenAPI
schema FastAPI auto-generates for the route.** FastAPI declares every
route's 422 response as the ``HTTPValidationError`` model — a list of
``{"loc": [...], "msg": "...", "type": "..."}`` objects under
``detail``. The framework's own ``RequestValidationError`` handler
emits that list shape, but a hand-raised ``HTTPException(status_code=
422, detail=str(exc))`` emits ``{"detail": "<string>"}`` instead. The
two don't match, so a typed client generated from the OpenAPI spec
(the Go CLI's oapi-codegen client, an openapi-python-client SDK, an
openapi-typescript SDK) fails to deserialize the 422 body — it
expects a list, gets a string, errors with a type mismatch.

:func:`http_for` closes that gap: for a 422 it wraps the detail into
the Pydantic validation-error list shape so the body round-trips
through any codegen-generated SDK; for every other status it falls
through to the plain ``{"detail": "<string>"}`` body (consistent with
FastAPI convention — the OpenAPI schemas for 400 / 403 / 404 / 409
don't declare a structured detail shape, so the string form is
conformant there).

The ``type`` tag in the 422 list entry is the discriminator typed
clients key on (the Go CLI reads ``detail[0].type`` to tell
``verify_response_required`` apart from ``verify_response_mismatch``);
the ``loc`` tuple matches FastAPI's Pydantic-validation ``loc``
convention so the entry is indistinguishable from a framework-emitted
one.

Usage — register each exception class once at module-import time, then
map a caught instance:

.. code-block:: python

    from meho_backplane.api.v1._errors import http_for, register_error

    register_error(
        MissingParamsError,
        status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        type_tag="missing_params",
        loc=("body", "params"),
    )
    register_error(
        RunNotFoundError,
        status=http_status.HTTP_404_NOT_FOUND,
    )

    try:
        ...
    except (MissingParamsError, RunNotFoundError) as exc:
        raise http_for(exc) from exc

A registered exception class with no explicit ``type_tag`` / ``loc``
(typical for the non-422 entries) carries ``None`` for both; those
fields are only consulted on the 422 branch, so a 404 / 403 / 400
entry never needs them.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import HTTPException
from fastapi import status as http_status

__all__ = ["http_for", "register_error"]


#: Registry of typed-exception class → ``(status, type_tag, loc)``.
#: ``type_tag`` / ``loc`` are populated only for 422 entries (they
#: feed the validation-error list entry); for other statuses they are
#: ``None`` and never consulted. Populated by :func:`register_error`
#: at each route module's import time. The strict lookup in
#: :func:`http_for` is the point: an unregistered type raises
#: :class:`KeyError`, which surfaces a missing-mapping bug at first
#: call rather than letting it fall through to a silent 500.
_ERROR_REGISTRY: Final[dict[type[Exception], tuple[int, str | None, tuple[str, ...] | None]]] = {}


def register_error(
    exc_cls: type[Exception],
    *,
    status: int,
    type_tag: str | None = None,
    loc: tuple[str, ...] | None = None,
) -> None:
    """Register the canonical HTTP mapping for *exc_cls*.

    Args:
        exc_cls: The typed exception class to map.
        status: The HTTP status code the route surfaces for it.
        type_tag: The structured-error ``type`` discriminator a typed
            client keys on. Required for 422 entries (it lands in the
            validation-error list entry); ignored for other statuses.
        loc: The ``loc`` path for the 422 validation-error entry,
            matching FastAPI's Pydantic-validation convention (e.g.
            ``("body", "verify_response")`` or ``("path", "slug")``).
            Defaults to ``("body",)`` on the 422 branch when omitted;
            ignored for other statuses.

    Idempotent re-registration of the same class overwrites the prior
    mapping (so re-importing a route module under test reload is
    harmless).
    """
    _ERROR_REGISTRY[exc_cls] = (status, type_tag, loc)


def http_for(exc: Exception) -> HTTPException:
    """Map a typed exception to its canonical :class:`HTTPException`.

    For a 422 status, wraps the exception message into the Pydantic
    validation-error **list** shape
    (``{"detail": [{"loc": [...], "msg": str(exc), "type": <tag>}]}``)
    so the body conforms to the ``HTTPValidationError`` schema FastAPI
    auto-generates and any codegen-generated SDK deserializes it. For
    every other status, emits the plain ``{"detail": "<string>"}``
    body.

    Looks the *concrete* type up in :data:`_ERROR_REGISTRY`; an
    unregistered type raises :class:`KeyError` (treat that as a
    contract bug — the handler caught something the route's
    typed-error vocabulary doesn't promise to map). Returns an
    :class:`HTTPException` the caller chains with ``from exc``.
    """
    status_code, type_tag, loc = _ERROR_REGISTRY[type(exc)]
    if status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT:
        detail: Any = [
            {
                "loc": list(loc) if loc is not None else ["body"],
                "msg": str(exc),
                "type": type_tag if type_tag is not None else "value_error",
            }
        ]
    else:
        detail = str(exc)
    return HTTPException(status_code=status_code, detail=detail)

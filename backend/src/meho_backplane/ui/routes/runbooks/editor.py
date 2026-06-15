# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI authoring editor: draft + edit template handler bodies.

Initiative #1381 (G10.6 Runbooks UI), Task #1383 (T2). The authoring half
of the ``/ui/runbooks*`` surface -- the ``tenant_admin`` editor that builds
a valid :class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody` from a
structured, repeatable step form and submits it through the same service the
REST surface uses:

* ``POST /ui/runbooks/new``         -> :meth:`RunbookTemplateService.create_draft`
  (mirrors REST ``POST /api/v1/runbooks/templates`` -- 201 / 409 / 422)
* ``POST /ui/runbooks/{slug}/edit`` -> :meth:`RunbookTemplateService.update_or_fork`
  (mirrors REST ``PATCH /api/v1/runbooks/templates/{slug}`` -- in-place vs fork)

Split out of :mod:`meho_backplane.ui.routes.runbooks.routes` (the read
surface, T1 #1382) so the read and authoring concerns live in separate
modules and neither file crosses the code-quality size gate -- the same
package-split convention :mod:`meho_backplane.ui.routes.memory` uses. This
module holds the form-tree (de)serialisation + the create/edit submission
logic; the FastAPI route wiring lives in
:mod:`meho_backplane.ui.routes.runbooks.editor_routes`, which imports the
helpers here.

Form transport
--------------

The editor posts its entire step tree as one JSON string in the ``steps``
form field (built by the Alpine model client-side) rather than as dozens of
indexed form fields. The discriminated-union shape (manual vs operation_call
step; confirm vs operation_call verify) survives the round-trip intact, and
the server rebuilds one ``Step``-shaped dict per step before handing the whole
body to Pydantic -- which performs the *authoritative* validation (non-empty
title / description, the step-id pattern + uniqueness, the ``${...}``
substitution allowlist). Client-side checks are convenience that block an
obviously-bad submit early; the server is the source of truth and a server
409 / 422 / 404 re-renders the editor inline with the entered data preserved.

CSRF
----

State-changing requests carry the double-submit token via ``hx-headers`` /
the cookie; the token is minted on render and **re-minted** on every
re-render that follows a consumed POST (a validation error, or a fork
notice). The prior token was consumed by the request, so both the cookie
and the ``hx-headers`` echo must refresh or every subsequent HTMX call 403s.
:func:`set_csrf_cookie` is the shared cookie-set the read surface also uses.

References
----------

* HTMX 2.0.9 ``HX-Redirect`` response header (client-side navigation on a
  204): https://htmx.org/reference/#response_headers
* Pydantic v2 ``ValidationError.errors()`` shape (``loc`` / ``msg``):
  https://docs.pydantic.dev/2.11/errors/validation_errors/
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import structlog
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from meho_backplane.kb.schemas import InvalidKbSlugError, validate_slug
from meho_backplane.runbooks.schemas import (
    DraftTemplateRequest,
    EditTemplateRequest,
    RunbookTemplateBody,
    ShowTemplateResponse,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    RunbookTemplateService,
    TemplateNotFoundError,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "build_editor_context",
    "empty_step",
    "handle_editor_submit",
    "set_csrf_cookie",
    "template_to_form_steps",
]

log = structlog.get_logger(__name__)


def set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set the JS-readable CSRF cookie on *response* (shared posture).

    Mirrors the kb / dashboard surfaces: not ``HttpOnly`` (HTMX must read it
    to echo ``X-CSRF-Token`` on any future state-changing request), ``Secure``
    + ``SameSite=Strict``, scoped to ``/ui``. Shared by the read surface and
    the authoring surface so the cookie attributes never drift between them.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


@dataclass(frozen=True)
class _EditorResult:
    """Outcome of a draft-create / edit-in-place authoring submission.

    Exactly one of :attr:`redirect_slug` (success -> navigate to detail) or
    :attr:`error_message` (failure -> re-render the editor inline) is set.
    On the fork path (editing a published template) :attr:`fork_notice` is
    populated so the editor can surface ``forked_from`` + the source
    version's ``in_flight_run_count`` to the admin.
    """

    redirect_slug: str | None
    error_message: str | None
    fork_notice: dict[str, object] | None


def empty_step() -> dict[str, object]:
    """Return the seed shape for one blank step in a fresh editor.

    The editor's Alpine model and the server-side prefill share this shape
    so the discriminated-union form starts on the ``manual`` + ``confirm``
    branch (the simplest valid step) with empty strings the operator fills
    in. ``op_id`` / ``params`` / verify fields are carried for both branches
    so an Alpine ``type`` toggle never has to synthesise missing keys.
    """
    return {
        "id": "",
        "title": "",
        "body": "",
        "type": "manual",
        "op_id": "",
        "params": "{}",
        "verify": {
            "type": "confirm",
            "prompt": "",
            "op_id": "",
            "params": "{}",
            "expect": "{}",
        },
    }


def template_to_form_steps(template: ShowTemplateResponse) -> list[dict[str, object]]:
    """Project an existing template's steps into the editor's flat form shape.

    Inverse of :func:`_parse_form_steps`: the discriminated-union step /
    verify models are flattened so the Alpine editor can bind every branch's
    fields with ``x-model`` without inspecting Pydantic types. ``params`` /
    ``expect`` dicts are re-serialised to pretty JSON strings (the shape the
    JSON textareas edit); the unused branch's fields default to empty so a
    ``type`` toggle reveals blank inputs rather than ``undefined``.
    """
    form_steps: list[dict[str, object]] = []
    for step in template.steps:
        verify = step.verify
        form_verify: dict[str, object] = {
            "type": verify.type,
            "prompt": getattr(verify, "prompt", ""),
            "op_id": getattr(verify, "op_id", ""),
            "params": _dict_to_json(getattr(verify, "params", {})),
            "expect": _dict_to_json(getattr(verify, "expect", {})),
        }
        form_steps.append(
            {
                "id": step.id,
                "title": step.title,
                "body": step.body,
                "type": step.type,
                "op_id": getattr(step, "op_id", ""),
                "params": _dict_to_json(getattr(step, "params", {})),
                "verify": form_verify,
            }
        )
    return form_steps


def _dict_to_json(value: object) -> str:
    """Serialise a params/expect dict to a pretty JSON string for the form."""
    if not isinstance(value, dict) or not value:
        return "{}"
    return json.dumps(value, indent=2, sort_keys=True)


def _parse_json_object(raw: object, field: str) -> dict[str, object]:
    """Parse a JSON-object string from the form, raising ``ValueError`` on junk.

    The operation_call params / verify params / verify expect fields are
    edited as raw JSON textareas in the form. An empty / whitespace value is
    treated as ``{}`` (the empty payload). Anything that is not a JSON object
    raises with a field-pointing message so the editor can surface it inline
    -- the server stays the source of truth even for the JSON sub-fields the
    client also pre-validates.
    """
    if raw is None:
        return {}
    text = raw if isinstance(raw, str) else json.dumps(raw)
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{field}: not valid JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field}: must be a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_form_verify(raw_verify: object, step_index: int) -> dict[str, object]:
    """Reassemble one step's verify gate from the flat editor payload.

    ``confirm`` drops the operation_call fields; ``operation_call`` carries
    ``op_id`` + the parsed JSON ``params`` / ``expect``. Raises
    :class:`ValueError` on a structurally-broken verify (Pydantic then does
    the authoritative validation when the dict is fed to the step model).
    """
    if not isinstance(raw_verify, dict):
        raise ValueError(f"steps[{step_index}].verify: not an object")
    verify_type = raw_verify.get("type")
    if verify_type == "confirm":
        return {"type": "confirm", "prompt": str(raw_verify.get("prompt", ""))}
    if verify_type == "operation_call":
        return {
            "type": "operation_call",
            "op_id": str(raw_verify.get("op_id", "")).strip(),
            "params": _parse_json_object(
                raw_verify.get("params"), f"steps[{step_index}].verify.params"
            ),
            "expect": _parse_json_object(
                raw_verify.get("expect"), f"steps[{step_index}].verify.expect"
            ),
        }
    raise ValueError(f"steps[{step_index}].verify.type: unknown verify type {verify_type!r}")


def _parse_form_steps(raw_steps: object) -> list[dict[str, object]]:
    """Reassemble the flat editor form payload into ``Step``-shaped dicts.

    The editor posts its entire step tree as one JSON string (built by the
    Alpine model) rather than dozens of indexed form fields -- the
    discriminated-union shape survives the round-trip intact and the server
    rebuilds one dict per step keyed exactly as
    :class:`~meho_backplane.runbooks.schemas.Step` expects. ``manual`` steps
    drop ``op_id`` / ``params``; ``operation_call`` steps carry them with the
    JSON sub-fields parsed. The verify branch is reassembled the same way.

    Raises :class:`ValueError` on a structurally-broken payload (not a list,
    an entry that is not an object, an unknown ``type``, a malformed JSON
    sub-field). Pydantic then performs the authoritative validation (step-id
    pattern, uniqueness, substitution allowlist) when the dicts are fed to
    :class:`RunbookTemplateBody`.
    """
    if not isinstance(raw_steps, list):
        raise ValueError("steps: payload is not a list")
    steps: list[dict[str, object]] = []
    for index, entry in enumerate(raw_steps):
        if not isinstance(entry, dict):
            raise ValueError(f"steps[{index}]: not an object")
        step_type = entry.get("type")
        verify = _parse_form_verify(entry.get("verify"), index)
        base: dict[str, object] = {
            "id": str(entry.get("id", "")).strip(),
            "title": str(entry.get("title", "")),
            "body": str(entry.get("body", "")),
            "verify": verify,
        }
        if step_type == "manual":
            base["type"] = "manual"
        elif step_type == "operation_call":
            base["type"] = "operation_call"
            base["op_id"] = str(entry.get("op_id", "")).strip()
            base["params"] = _parse_json_object(entry.get("params"), f"steps[{index}].params")
        else:
            raise ValueError(f"steps[{index}].type: unknown step type {step_type!r}")
        steps.append(base)
    return steps


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic ``ValidationError`` as a compact, operator-readable line.

    The editor surfaces this verbatim in the inline error banner. Each error
    is rendered as ``<dotted.loc>: <message>`` so an admin can map the
    failure back to the offending field (``steps.0.id``, ``title``, etc.)
    without parsing the raw Pydantic JSON.
    """
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(segment) for segment in error.get("loc", ()))
        message = error.get("msg", "invalid")
        parts.append(f"{loc}: {message}" if loc else message)
    return "; ".join(parts) if parts else "validation failed"


def _build_body_from_form(
    *,
    title: str,
    description: str,
    target_kind: str,
    raw_steps: object,
) -> RunbookTemplateBody:
    """Construct + validate a :class:`RunbookTemplateBody` from editor form fields.

    Reassembles the step tree (:func:`_parse_form_steps`) and hands the whole
    shape to Pydantic, which enforces the authoritative contract: the step-id
    pattern, step-id uniqueness, and the ``${...}`` substitution allowlist. A
    blank ``target_kind`` field maps to ``None`` (the column is nullable).

    The non-empty ``title`` / ``description`` and the ``>=1 step`` floor are
    authoring-surface contracts the shared :class:`RunbookTemplateBody` schema
    does not enforce on its own (it permits an empty title and an empty steps
    list), but a runbook with a blank title or no steps is meaningless to run
    and the issue's body shape requires them. Enforce them here (the client
    mirrors the same checks) so the editor never produces a degenerate
    template. Raises :class:`ValueError` (form-shape / floor problems) or
    :class:`pydantic.ValidationError` (schema-contract violations); the caller
    maps both to the inline error banner.
    """
    if not title.strip():
        raise ValueError("title: must not be empty")
    if not description.strip():
        raise ValueError("description: must not be empty")
    steps = _parse_form_steps(raw_steps)
    if not steps:
        raise ValueError("steps: a runbook needs at least one step")
    return RunbookTemplateBody(
        title=title,
        description=description,
        target_kind=target_kind.strip() or None,
        steps=steps,  # type: ignore[arg-type]
    )


async def _create_draft_from_form(
    session: UISessionContext,
    *,
    slug: str,
    body: RunbookTemplateBody,
) -> _EditorResult:
    """Create a new draft via the service; map service errors to inline form errors.

    Mirrors the REST ``POST /api/v1/runbooks/templates`` contract over the
    same service call the route handler uses -- 201 -> redirect to the new
    draft's detail page; ``DuplicateDraftError`` (the slug already has a
    version, i.e. the REST 409) and a slug failing :data:`SLUG_PATTERN`
    (the REST 422) map to inline field errors rather than a raw status code.
    Tenant + author identity come from the session, never the form.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        return _EditorResult(redirect_slug=None, error_message=f"slug: {exc}", fork_notice=None)

    try:
        await RunbookTemplateService().create_draft(
            session.tenant_id,
            session.operator_sub,
            DraftTemplateRequest(slug=slug, body=body),
        )
    except DuplicateDraftError as exc:
        return _EditorResult(redirect_slug=None, error_message=str(exc), fork_notice=None)
    except InvalidKbSlugError as exc:
        return _EditorResult(redirect_slug=None, error_message=f"slug: {exc}", fork_notice=None)
    return _EditorResult(redirect_slug=slug, error_message=None, fork_notice=None)


async def _edit_template_from_form(
    session: UISessionContext,
    *,
    slug: str,
    body: RunbookTemplateBody,
) -> _EditorResult:
    """Edit a draft in place, or fork a new draft from the latest published.

    Mirrors the REST ``PATCH /api/v1/runbooks/templates/{slug}`` contract:
    the service picks the in-place-vs-fork path. On the fork path (the slug's
    only versions are published / deprecated) the response carries
    ``forked_from``; the notice is surfaced to the admin so they see the
    source version + its ``in_flight_run_count`` before navigating on. A slug
    with no versions at all (or a cross-tenant probe) is
    :class:`TemplateNotFoundError` -> inline error (the REST 404). Success
    redirects to the resulting draft's detail page.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        return _EditorResult(redirect_slug=None, error_message=f"slug: {exc}", fork_notice=None)

    try:
        result = await RunbookTemplateService().update_or_fork(
            session.tenant_id,
            session.operator_sub,
            EditTemplateRequest(slug=slug, body=body),
        )
    except TemplateNotFoundError:
        return _EditorResult(
            redirect_slug=None,
            error_message=f"No runbook template {slug!r} to edit.",
            fork_notice=None,
        )
    fork_notice: dict[str, object] | None = None
    if result.forked_from is not None:
        fork_notice = {
            "slug": result.forked_from.slug,
            "source_version": result.forked_from.version,
            "new_version": result.version,
            "in_flight_run_count": result.forked_from.in_flight_run_count,
        }
    return _EditorResult(redirect_slug=slug, error_message=None, fork_notice=fork_notice)


def build_editor_context(
    session: UISessionContext,
    *,
    mode: Literal["new", "edit"],
    slug: str,
    title: str,
    description: str,
    target_kind: str,
    form_steps: list[dict[str, object]],
    csrf_token: str,
    error_message: str | None = None,
    fork_notice: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the Jinja context the ``runbooks/editor.html`` template needs.

    ``form_steps`` is JSON-serialised into ``initial_steps_json`` so the
    Alpine model can hydrate from the server-rendered prefill (an edit
    pre-loads the existing steps; a fresh ``new`` seeds one blank step). The
    post target + submit verb differ by ``mode`` so a single template serves
    both the draft-create and edit-in-place flows.
    """
    return {
        "mode": mode,
        "slug": slug,
        "title": title,
        "description": description,
        "target_kind": target_kind,
        "initial_steps_json": json.dumps(form_steps),
        "submit_url": "/ui/runbooks/new" if mode == "new" else f"/ui/runbooks/{slug}/edit",
        "submit_label": "Create draft" if mode == "new" else "Save changes",
        "error_message": error_message,
        "fork_notice": fork_notice,
        "csrf_token": csrf_token,
        "operator_sub": session.operator_sub,
        "active_surface": "runbooks",
        "page_title": "New runbook" if mode == "new" else f"Edit {slug}",
    }


def _render_editor(
    request: Request,
    session: UISessionContext,
    *,
    mode: Literal["new", "edit"],
    slug: str,
    title: str,
    description: str,
    target_kind: str,
    raw_steps: object,
    status_code: int = 200,
    error_message: str | None = None,
    fork_notice: dict[str, object] | None = None,
) -> HTMLResponse:
    """Re-render the editor with the operator's entered data preserved.

    CSRF is re-minted (the prior token was consumed by the POST that led
    here) and the cookie refreshed so subsequent HTMX calls carry a live
    token. A non-list ``raw_steps`` (the JSON payload was structurally
    broken) falls back to a single blank step so the form is never empty.
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    form_steps = raw_steps if isinstance(raw_steps, list) else [empty_step()]
    context = build_editor_context(
        session,
        mode=mode,
        slug=slug,
        title=title,
        description=description,
        target_kind=target_kind,
        form_steps=form_steps,
        csrf_token=csrf_token,
        error_message=error_message,
        fork_notice=fork_notice,
    )
    response = get_templates().TemplateResponse(
        request, "runbooks/editor.html", context, status_code=status_code
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def handle_editor_submit(
    request: Request,
    session: UISessionContext,
    *,
    mode: Literal["new", "edit"],
    slug: str,
    title: str,
    description: str,
    target_kind: str,
    steps_json: str,
) -> Response:
    """Shared draft-create / edit-in-place submission handler.

    Parses + validates the form into a :class:`RunbookTemplateBody`, then
    dispatches to the create or edit service path. On a plain success returns
    a 204 with ``HX-Redirect`` to the resulting draft's detail page. On a fork
    success (an edit of a published template) re-renders the editor (200) in
    ``edit`` mode against the new draft with the fork notice so the admin sees
    the source version + ``in_flight_run_count`` from the PATCH response. On
    any failure -- malformed form shape (:class:`ValueError`), a Pydantic
    contract violation (:class:`ValidationError`), a duplicate slug, or a
    missing template -- the editor is re-rendered inline at HTTP 422 with the
    entered data preserved and a freshly-minted CSRF token.
    """
    raw_steps: object = []
    error_message: str | None = None
    body: RunbookTemplateBody | None = None
    try:
        raw_steps = json.loads(steps_json) if steps_json.strip() else []
    except json.JSONDecodeError as exc:
        error_message = f"steps: malformed payload ({exc})"

    if error_message is None:
        try:
            body = _build_body_from_form(
                title=title,
                description=description,
                target_kind=target_kind,
                raw_steps=raw_steps,
            )
        except ValidationError as exc:
            error_message = _format_validation_error(exc)
        except ValueError as exc:
            error_message = str(exc)

    if body is not None:
        if mode == "new":
            result = await _create_draft_from_form(session, slug=slug, body=body)
        else:
            result = await _edit_template_from_form(session, slug=slug, body=body)
        if result.redirect_slug is not None and result.fork_notice is None:
            # Plain success (new draft, or in-place draft edit) -> navigate to
            # the resulting draft's detail page via the HTMX redirect header.
            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/ui/runbooks/{result.redirect_slug}"},
            )
        if result.redirect_slug is not None and result.fork_notice is not None:
            # Fork success: stay open in edit mode against the new draft with
            # the fork notice so the admin sees the source version + its
            # in_flight_run_count straight from the PATCH response.
            return _render_editor(
                request,
                session,
                mode="edit",
                slug=result.redirect_slug,
                title=title,
                description=description,
                target_kind=target_kind,
                raw_steps=raw_steps,
                fork_notice=result.fork_notice,
            )
        error_message = result.error_message

    return _render_editor(
        request,
        session,
        mode=mode,
        slug=slug,
        title=title,
        description=description,
        target_kind=target_kind,
        raw_steps=raw_steps,
        status_code=422,
        error_message=error_message or "validation failed",
    )

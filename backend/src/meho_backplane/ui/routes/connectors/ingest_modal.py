# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-ingest modal + async job-poll (Task #1886, T2).

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1886 (T2). The ingest on-ramp the T1 registry list
(``registry_list.py``) "Ingest" button drives:

* ``GET  /ui/connectors/registry/ingest`` -- the HTMX-loaded
  ``<dialog class="modal">`` fragment with two modes (catalog dropdown
  from :func:`catalog_endpoint` / explicit ``product``/``version``/
  ``impl_id`` + ``specs[].uri`` quadruple) and a dry-run toggle.
* ``POST /ui/connectors/registry/ingest`` -- the submit handler. Builds
  exactly **one** :class:`IngestRequest` shape (a pre-check renders a
  friendly inline error rather than the raw 422 the
  ``_exactly_one_request_shape`` validator would raise), calls
  :func:`ingest_endpoint` **in-process** (the ``forms_router.py``
  pattern, never the Bearer route), and branches on the response: a
  dry-run renders the sync parse counts (writes nothing); a real ingest
  renders the job-poll fragment seeded with the 202 ``job_id``. The
  ``scope`` field (#2209) picks the write scope: ``global`` (default)
  leaves ``tenant_id`` unset (the omit-equals-global REST contract per
  #2085); ``tenant`` sends the authenticated operator's own tenant
  UUID for a tenant-curated ingest (derived server-side, never posted
  by the client).
* ``GET  /ui/connectors/registry/ingest/jobs/{job_id}`` -- the job poll.
  Calls :func:`get_ingest_job_endpoint` in-process and renders
  ``_ingest_job_status.html``; while ``status == "running"`` the fragment
  self-polls (the ``_status_panel.html`` "stop returning the polling
  element" idiom), dropping the poll directive on a terminal status. A
  404 (process-local job evaporated on pod restart) renders the
  "job lost -- re-check the registry list" panel and stops -- never an
  infinite spinner.

RBAC: write is **TENANT_ADMIN** via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
on all three routes (mirroring the REST ``_require_admin`` gate on
``ingest_endpoint`` / ``get_ingest_job_endpoint``); the "Ingest" entry
point is soft-hidden from non-admins on the T1 list.

Route ordering is load-bearing (first-match-wins): the literal
``/ui/connectors/registry/ingest`` routes register **before** the
``/ui/connectors/{name}`` catch-all AND before the
``/ui/connectors/registry/{connector_id}/...`` param routes (so
``ingest`` is never captured as a ``connector_id``); the include order in
:func:`~meho_backplane.ui.routes.connectors.build_router` enforces it.

Error panels reuse the T1 shape-generic ``_registry_error.html`` renderer
(the inline action-error panel): the catalog-resolution 422s (with
``available_entries[]`` for ``catalog_entry_not_found``), the
explicit-shape 422s (``VersionMismatchError`` / ``ProductImplIdMismatch``
/ ``UncoveredVersionLabel``), the ``400`` spec-parse family, and the
``503 LlmClientUnavailable`` all render as actionable inline panels in
place of a 5xx.

CSRF: every fragment render mints a fresh double-submit token + re-sets
the ``meho_csrf`` cookie so the next state-changing submit's header echo
lines up.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from meho_backplane.api.v1.connectors_ingest import (
    catalog_endpoint,
    get_ingest_job_endpoint,
    ingest_endpoint,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.ingest import (
    IngestRequest,
    SpecSource,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

#: The render / submit / poll helpers are the module's public API -- the
#: route registration lives in
#: :mod:`~meho_backplane.ui.routes.connectors.ingest_router` (the
#: ``forms.py`` / ``forms_router.py`` split) and imports them by name,
#: along with the public form-field caps below.
__all__ = [
    "CATALOG_ENTRY_MAX",
    "IMPL_ID_MAX",
    "PRODUCT_MAX",
    "VERSION_MAX",
    "poll_job",
    "render_modal",
    "submit_ingest",
]

_log = structlog.get_logger(__name__)

#: Self-poll cadence for the running-job fragment. 2s keeps the operator
#: feedback tight without hammering the in-process registry read (the
#: ``_status_panel.html`` precedent runs the same order of magnitude).
_POLL_INTERVAL_SECONDS = 2

#: Form-field length caps -- bound the form-body parse against a
#: paste-from-clipboard accident before the bytes reach the
#: ``IngestRequest`` Pydantic validation (which is authoritative).
#: Mirror the ``IngestRequest`` field bounds. Public so the route module
#: (:mod:`~meho_backplane.ui.routes.connectors.ingest_router`) declares
#: the ``Form`` ``max_length`` from the same source.
PRODUCT_MAX = 64
VERSION_MAX = 64
IMPL_ID_MAX = 128
CATALOG_ENTRY_MAX = 128
#: Cap the number of spec-uri rows the form accepts (the schema caps
#: ``specs`` at 16); a longer list is a malformed / fuzzed body. Each
#: URI's own length is bounded by the template ``maxlength`` + the
#: ``SpecSource.uri`` ``max_length=2048`` validator (a ``ValidationError``
#: -- itself a ``ValueError`` -- the submit handler catches as a friendly
#: inline error).
_MAX_SPEC_URIS = 16


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Value MUST equal the token the rendered markup echoes via
    ``hx-headers`` or the CSRF middleware rejects the next submit. Same
    posture as :mod:`~meho_backplane.ui.routes.connectors.registry_list`.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _render_error_panel(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    available_entries: list[str] | None = None,
) -> HTMLResponse:
    """Render the shared inline error panel (the T1 shape-generic renderer).

    Swapped into the modal's ``#ingest-result`` slot so a catalog /
    explicit-shape 422, a spec-parse 400, or the 503 ``LlmClientUnavailable``
    surfaces as an actionable message in place of a 5xx / stack trace.
    ``available_entries`` carries the ``catalog_entry_not_found``
    ``available_entries[]`` list so the panel enumerates the valid refs.
    """
    return get_templates().TemplateResponse(
        request,
        "connectors/_registry_error.html",
        {
            "title": title,
            "message": message,
            "candidates": [],
            "available_entries": available_entries or [],
        },
        status_code=status_code,
    )


def _detail_field(detail: Any, key: str) -> Any:
    """Read *key* off a structured ``HTTPException.detail`` dict, else ``None``."""
    if isinstance(detail, dict):
        return detail.get(key)
    return None


def _detail_message(detail: Any, *, fallback: str) -> str:
    """Project an ``HTTPException.detail`` into operator-readable copy.

    The ingest route raises two detail shapes: the structured envelopes
    (a dict carrying ``message`` + a classifier) and a plain string (the
    503 / 400 arms pass ``str(exc)``). Prefer the structured ``message``;
    fall back to the string form, then to *fallback*.
    """
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(detail, str) and detail:
        return detail
    return fallback


def _panel_from_http_exception(
    request: Request,
    exc: HTTPException,
) -> HTMLResponse:
    """Map an in-process ingest :class:`HTTPException` to an inline panel.

    The catalog-resolution 422s, the explicit-shape 422s
    (``VersionMismatchError`` / ``ProductImplIdMismatch`` /
    ``UncoveredVersionLabel``), the ``400`` spec-parse family, the
    ``502`` upstream-fetch arm, and the ``503 LlmClientUnavailable`` all
    render as actionable panels. The ``catalog_entry_not_found`` 422
    enumerates the ``available_entries[]`` it carries. Anything else
    (a genuinely unexpected fault) re-raises unchanged so it still
    surfaces as a real error rather than a mislabelled panel.
    """
    detail = exc.detail
    classifier = _detail_field(detail, "detail")

    if exc.status_code == 422 and classifier == "catalog_entry_not_found":
        available = _detail_field(detail, "available_entries")
        return _render_error_panel(
            request,
            title="Catalog entry not found",
            message=_detail_message(
                detail,
                fallback="That catalog entry is not in the packaged catalog.",
            ),
            status_code=422,
            available_entries=available if isinstance(available, list) else [],
        )
    if exc.status_code == 422:
        return _render_error_panel(
            request,
            title="Ingest request rejected",
            message=_detail_message(
                detail,
                fallback=(
                    "The request was well-formed but the spec-vs-label / "
                    "catalog check refused it. Adjust the inputs and re-submit."
                ),
            ),
            status_code=422,
        )
    if exc.status_code == 503:
        return _render_error_panel(
            request,
            title="LLM grouping is unavailable",
            message=(
                "The operation-grouping step needs an LLM client that is not "
                "configured in this deploy, so the ingest cannot complete. "
                "Ask an administrator to configure the ingest LLM, then "
                "re-submit."
            ),
            status_code=503,
        )
    if exc.status_code in (400, 502):
        return _render_error_panel(
            request,
            title="Spec could not be ingested",
            message=_detail_message(
                detail,
                fallback="The spec could not be parsed or fetched.",
            ),
            status_code=exc.status_code,
        )
    raise exc


def _build_catalog_entries(catalog: Any) -> list[dict[str, Any]]:
    """Project the catalog response into the dropdown's row shape.

    Each option is a ``"<product>/<version>"`` ref (the catalog-entry
    reference :func:`ingest_endpoint` resolves) plus the ``impl_id`` so
    the operator can disambiguate two versions of the same product.

    ``ships_local`` (#1980) is ``True`` when the catalog row carries a
    MEHO-authored ``spec_resource`` and/or ``profile_resource`` shipped as
    package data (#1975 / #1976) -- the catalog-driven ingest loads those
    bytes via ``importlib.resources`` rather than fetching ``upstream``, so
    the row needs no operator-supplied spec upload. Display-only: it marks
    the option so the operator knows a ``--catalog`` ingest of that row is
    self-contained.
    """
    return [
        {
            "ref": f"{entry.product}/{entry.version}",
            "product": entry.product,
            "version": entry.version,
            "impl_id": entry.impl_id,
            "ships_local": entry.spec_resource is not None or entry.profile_resource is not None,
        }
        for entry in catalog.catalog
    ]


async def render_modal(
    request: Request,
    *,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Render the ingest modal fragment (the ``GET`` handler body).

    Calls :func:`catalog_endpoint` in-process for the catalog dropdown
    (operator-level read on the REST side; the modal route already gated
    TENANT_ADMIN, a superset). Mints + re-sets the CSRF cookie so the
    form's ``hx-headers`` echo lines up with the next submit.
    """
    catalog = await catalog_endpoint(operator=operator)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "catalog_entries": _build_catalog_entries(catalog),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_ingest_modal.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _build_ingest_request(
    *,
    mode: str,
    catalog_entry: str,
    product: str,
    version: str,
    impl_id: str,
    spec_uris: list[str],
    dry_run: bool,
    scope: str,
    operator_tenant_id: UUID,
) -> IngestRequest:
    """Build exactly one :class:`IngestRequest` shape from the form fields.

    Pre-checks the mutually-exclusive-shape contract so the operator gets
    a friendly inline error rather than the raw 422 the
    ``_exactly_one_request_shape`` validator would raise: the
    ``catalog`` mode sends ONLY ``catalog_entry``; the ``explicit`` mode
    sends ONLY ``product``/``version``/``impl_id``/``specs[]``. A blank
    required field in the chosen mode raises :class:`ValueError` with
    operator-readable copy; a body that populates BOTH shapes (a tampered
    or stale-Alpine form posting catalog_entry alongside the quadruple
    fields) raises the friendly conflict error rather than reaching the
    validator's raw ``catalog_entry_conflict`` 422. ``async`` is left at
    its default (``True``) so a non-dry-run submit kicks the async job.

    ``scope`` (#2209) selects the write scope orthogonally to the shape
    split: ``"global"`` leaves ``tenant_id`` unset (the built-in /
    global scope, the REST contract's omit-equals-global default per
    #2085); ``"tenant"`` pins the ingest to the operator's own tenant.
    The UUID is derived server-side from the *authenticated* operator —
    the form posts only the discriminator, never a raw UUID the handler
    would have to distrust (the REST route 403s a foreign UUID anyway,
    but a tampered value should not get that far). An unknown value is
    a tampered form and raises the friendly :class:`ValueError`.
    """
    if scope == "global":
        tenant_id: UUID | None = None
    elif scope == "tenant":
        tenant_id = operator_tenant_id
    else:
        raise ValueError("Unknown write scope; reopen the modal and try again.")

    if mode == "catalog":
        entry = catalog_entry.strip()
        if not entry:
            raise ValueError("Pick a catalog entry, or switch to the explicit-quadruple tab.")
        if (
            product.strip()
            or version.strip()
            or impl_id.strip()
            or any(uri.strip() for uri in spec_uris)
        ):
            raise ValueError(
                "Supply a catalog entry OR an explicit product / version / impl_id / "
                "spec URL set, never both. Clear the fields on the other tab and re-submit."
            )
        return IngestRequest(catalog_entry=entry, dry_run=dry_run, tenant_id=tenant_id)

    if mode == "explicit":
        product_v = product.strip()
        version_v = version.strip()
        impl_id_v = impl_id.strip()
        uris = [uri.strip() for uri in spec_uris if uri.strip()]
        missing = [
            label
            for label, value in (
                ("product", product_v),
                ("version", version_v),
                ("impl_id", impl_id_v),
            )
            if not value
        ]
        if not uris:
            missing.append("at least one spec URL")
        if missing:
            raise ValueError("The explicit-quadruple tab needs " + ", ".join(missing) + ".")
        return IngestRequest(
            product=product_v,
            version=version_v,
            impl_id=impl_id_v,
            specs=[SpecSource(uri=uri) for uri in uris],
            dry_run=dry_run,
            tenant_id=tenant_id,
        )

    raise ValueError("Unknown ingest mode; reopen the modal and try again.")


def _render_dry_run(request: Request, *, payload: dict[str, Any]) -> HTMLResponse:
    """Render the dry-run parse-counts fragment from the 200 sync body.

    The sync ``IngestResponse`` shape is ``{ingestion: {...},
    grouping: null}``; the dry-run path always writes nothing, so the
    fragment surfaces the projected counts only.
    """
    return get_templates().TemplateResponse(
        request,
        "connectors/_ingest_dry_run.html",
        {"ingestion": payload.get("ingestion") or {}},
    )


def _render_job_seed(
    request: Request,
    *,
    job_id: str,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the job-poll fragment seeded from the 202 ``IngestJobHandle``.

    Seeds the fragment in the ``running`` state so it self-polls
    ``GET .../ingest/jobs/{job_id}`` until terminal. Mints + re-sets the
    CSRF cookie (the poll GET is safe, but keeping the cookie fresh
    matches every other fragment render in the surface).
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "connectors/_ingest_job_status.html",
        {
            "job_id": job_id,
            # The 202 handle carries no triple; seed explicit ``None`` so
            # the template's ``{% if job.product %}`` is a clean falsy
            # check under Jinja StrictUndefined (a missing key would raise).
            "job": {"status": "running", "product": None, "version": None, "impl_id": None},
            "job_lost": False,
            "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def submit_ingest(
    request: Request,
    *,
    mode: str,
    catalog_entry: str,
    product: str,
    version: str,
    impl_id: str,
    spec_uris: list[str],
    dry_run: bool,
    scope: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """``POST .../ingest`` -- build one request shape, ingest in-process, branch.

    A pre-check shape error renders a friendly inline 422 panel (not the
    raw validator 422). The in-process ``ingest_endpoint`` call branches:
    a 200 sync body renders the dry-run counts; a 202 body seeds the
    job-poll fragment. A domain ``HTTPException`` (catalog / explicit 422,
    400 spec-parse, 503 LLM-unavailable) renders the shared inline panel.

    ``scope`` (#2209) is the modal's write-scope discriminator:
    ``"global"`` (the default) ingests under the built-in / global scope
    (``tenant_id`` unset, matching the REST/CLI/MCP omit-equals-global
    contract); ``"tenant"`` sends the authenticated operator's own
    ``tenant_id`` for a tenant-curated ingest.
    """
    if len(spec_uris) > _MAX_SPEC_URIS:
        return _render_error_panel(
            request,
            title="Too many spec URLs",
            message=f"Supply at most {_MAX_SPEC_URIS} spec URLs.",
            status_code=422,
        )
    try:
        body = _build_ingest_request(
            mode=mode,
            catalog_entry=catalog_entry,
            product=product,
            version=version,
            impl_id=impl_id,
            spec_uris=spec_uris,
            dry_run=dry_run,
            scope=scope,
            operator_tenant_id=operator.tenant_id,
        )
    except ValueError as exc:
        # Friendly pre-check error -- the operator mixed / omitted a shape.
        return _render_error_panel(
            request,
            title="Check the ingest form",
            message=str(exc),
            status_code=422,
        )

    try:
        response = await ingest_endpoint(body=body, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc)

    payload = _decode_json_response(response)
    if response.status_code == 200:
        _log.info(
            "ui_connector_ingest_dry_run",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
        )
        return _render_dry_run(request, payload=payload)

    job_id = str(payload.get("job_id", ""))
    _log.info(
        "ui_connector_ingest_started",
        job_id=job_id,
        write_scope=scope,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
    )
    return _render_job_seed(request, job_id=job_id, session_ctx=session_ctx)


def _decode_json_response(response: JSONResponse) -> dict[str, Any]:
    """Decode the in-process ``ingest_endpoint`` ``JSONResponse`` body.

    ``ingest_endpoint`` returns a Starlette ``JSONResponse`` directly
    (not a Pydantic model), so the BFF reads the already-rendered JSON
    bytes off ``response.body`` rather than re-serialising. A non-object
    body (never produced by the route) projects to an empty dict so the
    caller's ``.get`` reads stay safe.
    """
    decoded = json.loads(bytes(response.body))
    return decoded if isinstance(decoded, dict) else {}


async def poll_job(
    request: Request,
    *,
    job_id: str,
    operator: Operator,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """``GET .../ingest/jobs/{job_id}`` -- render the job status fragment.

    Calls :func:`get_ingest_job_endpoint` in-process (TENANT_ADMIN, the
    REST gate). A 404 (process-local job evaporated on pod restart, or a
    cross-tenant / built-in probe) renders the terminal "job lost --
    re-check the registry list" panel with NO poll directive -- the
    process-local-jobs footgun guard, never an infinite spinner.
    Otherwise the fragment renders the live status; while ``running`` it
    keeps the ``hx-trigger`` self-poll, dropping it on a terminal status.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    try:
        job = await get_ingest_job_endpoint(job_id=_parse_job_uuid(job_id), operator=operator)
    except HTTPException as exc:
        if exc.status_code == 404:
            response = get_templates().TemplateResponse(
                request,
                "connectors/_ingest_job_status.html",
                {
                    "job_id": job_id,
                    "job": None,
                    "job_lost": True,
                    "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
                },
            )
            _set_csrf_cookie(response, csrf_token)
            return response
        raise

    response = get_templates().TemplateResponse(
        request,
        "connectors/_ingest_job_status.html",
        {
            "job_id": job_id,
            "job": job.model_dump(mode="json"),
            "job_lost": False,
            "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _parse_job_uuid(job_id: str) -> UUID:
    """Parse the ``job_id`` path segment into a ``UUID``, 404ing a bad shape.

    A non-UUID segment cannot name a real job, so reject it with the same
    404 the registry's ``get`` raises after a round trip -- the fragment
    then renders the job-lost panel (no poll), never a 422 the operator
    can't act on inside the modal.
    """
    try:
        return UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="ingest_job_not_found") from exc

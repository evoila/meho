# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bulk ``targets.yaml`` import UI (paste / upload -> preview -> confirm).

Initiative #340 (G10.3 Connectors + Targets UI), Task #875 (T3) work
item #5. The operator pastes or uploads a ``targets.yaml`` file; the
server parses it (:func:`yaml.safe_load`), classifies every entry as
CREATE or UPDATE against the caller's tenant, and renders a preview
table. On confirm the route applies the plan **in-process** via the
existing target CRUD handlers
(:func:`~meho_backplane.api.v1.targets.create_target` for new names,
:func:`~meho_backplane.api.v1.targets.update_target` for existing
ones) and renders a result summary.

There is **no** ``/api/v1/targets/import`` server endpoint. This UI
mirrors the client-orchestrated CRUD the ``meho targets import`` CLI
tool (G0.3-T6 #257, ``cli/internal/cmd/targets/import.go``) performs:
parse -> list existing names -> classify CREATE vs UPDATE -> POST new /
PATCH existing. The key-mapping + classification logic here is a
server-side port of that CLI's ``mapEntry`` / ``buildLivePlan``, so the
web import and the CLI import produce byte-identical writes for the
same YAML.

Mapping rules (parity with ``import.go``)
-----------------------------------------

* **Known top-level keys** -- ``name``, ``aliases``, ``product``,
  ``host``, ``port``, ``fqdn``, ``secret_ref``, ``auth_model``,
  ``vpn_required``, ``notes``, ``preferred_impl_id``, ``extras`` -- map
  1:1 to :class:`~meho_backplane.targets.schemas.TargetCreate` /
  :class:`~meho_backplane.targets.schemas.TargetUpdate` fields.
* **Unknown keys** spill into the ``extras`` JSONB column (merged with
  an explicit ``extras:`` block when one is present in the YAML).
* **``fingerprint``** is server-managed (the probe verb is the only
  legitimate writer; both write schemas reject it via
  ``extra='forbid'``) -> dropped from the import with a preview
  warning.
* **CREATE vs UPDATE** is decided by an existing-name lookup scoped to
  the caller's tenant.
* On **UPDATE** the body is **sparse** (only keys present in the YAML);
  ``name`` and ``product`` are stripped because the PATCH route rejects
  ``name`` and the CLI strips ``product`` on update (rename = delete +
  create per the G0.3 contract).

RBAC + isolation
----------------

Every route is **tenant_admin only**; the gate is server-side via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
(the same write-gate T1's re-probe and T2's create / edit use). CSRF is
enforced by the chassis :class:`~meho_backplane.ui.csrf.CSRFMiddleware`
on the ``POST`` routes. Cross-tenant isolation is enforced at two
layers: the existing-name lookup filters on ``session_ctx.tenant_id``,
and the in-process ``create_target`` / ``update_target`` handlers write
/ resolve under ``operator.tenant_id`` (the caller's tenant), so an
import can only ever land in the caller's own tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
import yaml
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.targets import create_target, update_target
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "PlanEntry",
    "build_plan",
    "render_import_page",
    "render_preview",
    "submit_confirm",
]

_log = structlog.get_logger(__name__)

#: YAML keys that map 1:1 to a top-level column on the write schemas.
#: Verbatim parity with ``knownTopLevel`` in
#: ``cli/internal/cmd/targets/import.go`` so the web import and the CLI
#: import spill the same keys into ``extras``.
_KNOWN_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "aliases",
        "auth_model",
        "extras",
        "fqdn",
        "host",
        "name",
        "notes",
        "port",
        "preferred_impl_id",
        "product",
        "secret_ref",
        "vpn_required",
    }
)

#: YAML keys deliberately dropped with a warning rather than passed to
#: the write schema. ``fingerprint`` is server-managed (probe verb is
#: the only writer; the schemas reject it via ``extra='forbid'``).
#: Parity with ``skipSilent`` in ``import.go``.
_SKIP_SILENT: frozenset[str] = frozenset({"fingerprint"})


@dataclass
class PlanEntry:
    """One classified import entry: what the confirm step would do.

    ``action`` is ``"CREATE"`` or ``"UPDATE"``. ``body`` is the mapped
    field dict already shaped for the chosen write path (full for
    CREATE; sparse -- ``name`` / ``product`` stripped -- for UPDATE).
    ``warnings`` collects per-entry advisories (e.g. a dropped
    ``fingerprint`` key). The dataclass is the in-process equivalent of
    the CLI's ``planEntry`` JSON shape; it is never serialised to the
    wire here (the preview renders straight from it).
    """

    name: str
    action: str
    body: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _map_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Partition one YAML entry into a write body + per-entry warnings.

    Port of ``mapEntry`` in ``import.go``: an explicit ``extras:`` block
    is read first so unknown-key spills merge into it (rather than
    overwriting the operator's intentional payload); ``fingerprint`` is
    dropped with a warning; known keys pass through to the top level;
    every other key spills into ``extras``.
    """
    body: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    warnings: list[str] = []

    raw_extras = entry.get("extras")
    if isinstance(raw_extras, dict):
        extras.update(raw_extras)

    for key, value in entry.items():
        if key == "extras":
            # Handled in the pre-pass above; skip so it is not double-counted.
            continue
        if key in _SKIP_SILENT:
            warnings.append(f"skipped field {key!r}: server-managed, set via probe verb")
            continue
        if key in _KNOWN_TOP_LEVEL:
            body[key] = value
            continue
        extras[key] = value

    if extras:
        body["extras"] = extras
    return body, warnings


def _entry_to_update_body(entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Map one YAML entry to a sparse PATCH body.

    Only keys present in the YAML appear in the body so
    ``update_target``'s ``model_dump(exclude_unset=True)`` touches only
    the listed columns (the load-bearing sparse-PATCH contract PR #362's
    review on #257 pinned). ``name`` and ``product`` are stripped --
    the PATCH route rejects ``name`` and ``product`` is immutable on the
    CLI update path.
    """
    body, warnings = _map_entry(entry)
    body.pop("name", None)
    body.pop("product", None)
    return body, warnings


class ImportParseError(Exception):
    """Raised when the pasted / uploaded YAML cannot be turned into a plan.

    Carries an operator-facing message rendered inline in the preview
    fragment (no 500). Covers a YAML syntax error, a non-mapping root, a
    missing / empty ``targets:`` list, and a per-entry required-field
    violation (``name`` / ``product`` / ``host``) -- the same fail-fast
    validation the CLI parser does before any write.
    """


def _parse_targets_yaml(raw: str) -> list[dict[str, Any]]:
    """Parse the ``targets.yaml`` text into a list of per-entry maps.

    Mirrors ``parseTargetsYAML`` in ``import.go``: ``yaml.safe_load`` so
    no arbitrary Python objects can be constructed from operator input;
    require a ``targets:`` list; validate ``name`` / ``product`` /
    ``host`` per entry so a malformed file fails before any CRUD call.
    """
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ImportParseError(f"parse YAML: {exc}") from exc

    if doc is None:
        raise ImportParseError("parse YAML: file is empty")
    if not isinstance(doc, dict):
        raise ImportParseError("parse YAML: root must be a mapping with a `targets:` list")

    targets = doc.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ImportParseError("parse YAML: no `targets:` list found, or list is empty")

    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(targets):
        if not isinstance(entry, dict):
            raise ImportParseError(f"entry {index}: each target must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ImportParseError(f"entry {index}: missing or empty `name` field")
        if not isinstance(entry.get("product"), str):
            raise ImportParseError(f"entry {name!r}: missing or non-string `product` field")
        if not isinstance(entry.get("host"), str):
            raise ImportParseError(f"entry {name!r}: missing or non-string `host` field")
        entries.append(entry)
    return entries


async def _existing_names(db_session: AsyncSession, tenant_id: object) -> set[str]:
    """Return the set of live target names in the caller's tenant.

    The CREATE-vs-UPDATE classifier. Scoped to ``tenant_id`` and
    excluding soft-deleted rows (``deleted_at IS NULL``) -- the same
    filter the ``/api/v1/targets`` list route (which the CLI calls for
    its classification) applies, so the web import and the CLI import
    classify identically.
    """
    stmt = select(TargetORM.name).where(
        TargetORM.tenant_id == tenant_id,
        TargetORM.deleted_at.is_(None),
    )
    result = await db_session.execute(stmt)
    return set(result.scalars().all())


def build_plan(entries: list[dict[str, Any]], existing: set[str]) -> list[PlanEntry]:
    """Classify every entry CREATE-vs-UPDATE and build its write body.

    Existing names plan as UPDATE (sparse body); new names plan as
    CREATE (full mapped body). Source order is preserved so the preview
    table reads top-to-bottom in the same order as the YAML.
    """
    plan: list[PlanEntry] = []
    for entry in entries:
        name = str(entry["name"])
        if name in existing:
            body, warnings = _entry_to_update_body(entry)
            plan.append(PlanEntry(name=name, action="UPDATE", body=body, warnings=warnings))
        else:
            body, warnings = _map_entry(entry)
            plan.append(PlanEntry(name=name, action="CREATE", body=body, warnings=warnings))
    return plan


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for the import renders."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _plan_to_rows(plan: list[PlanEntry]) -> list[dict[str, Any]]:
    """Project the plan into the template-friendly row shape.

    Each row carries the name, the action, the JSON-present field keys
    (so the preview can show *what* would be written without dumping the
    full body), and any warnings.
    """
    return [
        {
            "name": entry.name,
            "action": entry.action,
            "fields": sorted(entry.body.keys()),
            "warnings": entry.warnings,
        }
        for entry in plan
    ]


async def render_import_page(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the full bulk-import page (paste box + upload control)."""
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "connectors/import.html",
        {
            "page_title": "Connectors",
            "active_surface": "connectors",
            "ready": False,
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_preview(
    request: Request,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    *,
    yaml_text: str,
) -> HTMLResponse:
    """Parse the submitted YAML and render the CREATE / UPDATE preview.

    A parse error renders the preview fragment with an inline error
    message and HTTP 422 (never a 500). A clean parse renders the plan
    table; the YAML is echoed back into a hidden field so the confirm
    submit re-parses the same bytes (the preview is stateless -- the
    server holds no plan between the two requests).
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    try:
        entries = _parse_targets_yaml(yaml_text)
    except ImportParseError as exc:
        response = get_templates().TemplateResponse(
            request,
            "connectors/_import_preview.html",
            {
                "ready": False,
                "csrf_token": csrf_token,
                "error": str(exc),
                "rows": [],
                "yaml_text": yaml_text,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        _set_csrf_cookie(response, csrf_token)
        return response

    existing = await _existing_names(db_session, session_ctx.tenant_id)
    plan = build_plan(entries, existing)
    rows = _plan_to_rows(plan)
    response = get_templates().TemplateResponse(
        request,
        "connectors/_import_preview.html",
        {
            "ready": False,
            "csrf_token": csrf_token,
            "error": None,
            "rows": rows,
            "create_count": sum(1 for r in rows if r["action"] == "CREATE"),
            "update_count": sum(1 for r in rows if r["action"] == "UPDATE"),
            "yaml_text": yaml_text,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _build_create_body(body: dict[str, Any]) -> TargetCreate:
    """Coerce a mapped CREATE body dict into a :class:`TargetCreate`.

    ``auth_model`` arrives as a YAML string; coerce it through the enum
    so an unknown value raises the same ``ValueError`` the schema would
    on a bad enum member. All other fields flow straight to Pydantic,
    which runs the identical validation the REST POST body runs.
    """
    coerced = dict(body)
    if "auth_model" in coerced and coerced["auth_model"] is not None:
        coerced["auth_model"] = AuthModel(coerced["auth_model"])
    return TargetCreate.model_validate(coerced)


def _build_update_body(body: dict[str, Any]) -> TargetUpdate:
    """Coerce a sparse UPDATE body dict into a :class:`TargetUpdate`."""
    coerced = dict(body)
    if "auth_model" in coerced and coerced["auth_model"] is not None:
        coerced["auth_model"] = AuthModel(coerced["auth_model"])
    return TargetUpdate.model_validate(coerced)


async def submit_confirm(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    db_session: AsyncSession,
    *,
    yaml_text: str,
) -> HTMLResponse:
    """Re-parse, re-classify, and apply the plan in-process.

    Re-parsing on confirm (rather than trusting a client-held plan)
    keeps the server the source of truth for the classification: the
    CREATE-vs-UPDATE decision is re-made against the tenant's *current*
    target set, so a target created between preview and confirm is
    correctly PATCHed rather than re-CREATEd into a 409. Each entry is
    applied via the in-process REST handler -- ``create_target`` for new
    names, ``update_target`` for existing -- so the product-registry
    check, the audit binding, and the broadcast hook fire exactly as
    they do on the REST and CLI surfaces. The result summary (N created,
    M updated) renders into the same slot.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    try:
        entries = _parse_targets_yaml(yaml_text)
    except ImportParseError as exc:
        response = get_templates().TemplateResponse(
            request,
            "connectors/_import_preview.html",
            {
                "ready": False,
                "csrf_token": csrf_token,
                "error": str(exc),
                "rows": [],
                "yaml_text": yaml_text,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        _set_csrf_cookie(response, csrf_token)
        return response

    existing = await _existing_names(db_session, session_ctx.tenant_id)
    plan = build_plan(entries, existing)

    created = 0
    updated = 0
    for entry in plan:
        if entry.action == "CREATE":
            await create_target(
                body=_build_create_body(entry.body),
                operator=operator,
                session=db_session,
            )
            created += 1
        else:
            await update_target(
                name=entry.name,
                body=_build_update_body(entry.body),
                operator=operator,
                session=db_session,
            )
            updated += 1

    _log.info(
        "ui_target_import",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        created=created,
        updated=updated,
    )
    response = get_templates().TemplateResponse(
        request,
        "connectors/_import_result.html",
        {
            "ready": False,
            "csrf_token": csrf_token,
            "created": created,
            "updated": updated,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response

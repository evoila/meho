# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Export, preview, apply and history for the settings file (``core.md`` §5).

* :func:`export_settings` builds the file from the live state.
* :func:`build_plan` checks a file and works out what it would change. It
  reads the database and nothing else: no write, no outside system. The
  preview is exactly this.
* :func:`apply_import` runs :func:`build_plan` and, when the file has no
  mistakes, adds or updates (never deletes) in the request's one transaction,
  in the order tenants, policies, conventions, targets, with a savepoint per
  item so one failed item does not spoil the rest. One ``audit_log`` row
  covers the whole import and the revision keeps its id; each convention
  history row keeps the import's ``request_id``. An unchanged file writes no revision.
* :func:`list_history` returns the revisions, newest first, each with its
  diff worked out on read.

Writes reuse the existing paths so the file cannot do anything the routes
cannot: :mod:`meho_backplane.tenancy.policies` for the tenant columns,
:class:`~meho_backplane.conventions.service.ConventionsService` for
conventions, and the ``create_target`` / ``update_target`` handlers (with
their product, impl-id, ``secret_ref`` scope and TLS checks) for targets.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog
import yaml
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.targets import (
    _canonicalise_and_validate_product,
    _enforce_tls_trust_exclusion,
    _resolve_create_secret_ref,
    _to_full,
    _validate_preferred_impl_id,
    _validate_update_body_fields,
    create_target,
    update_target,
)
from meho_backplane.audit import bind_preallocated_audit_id
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
from meho_backplane.conventions._internal import OverBudgetError, enforce_budget
from meho_backplane.conventions.schemas import ConventionCreate, ConventionKind, ConventionUpdate
from meho_backplane.conventions.service import ConventionsService
from meho_backplane.db.models import (
    InstanceSettingsRevision,
    Tenant,
    TenantConvention,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.instance_settings.diff import DEFAULT_POLICIES, diff_specs, is_empty, summarise
from meho_backplane.instance_settings.schema import (
    API_VERSION,
    KIND,
    SettingsConvention,
    SettingsFile,
    SettingsTarget,
    SettingsTenant,
    contains_secret,
    find_secret_shaped_values,
    format_validation_errors,
    parse_settings_file,
)
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.targets.secret_field_map import merge_secret_field_map
from meho_backplane.tenancy.ensure import ensure_tenant
from meho_backplane.tenancy.policies import (
    set_announce_gate_enabled,
    set_flight_recorder_policy,
    set_mail_recipient_allowlist,
    set_run_approval_target_kinds,
)
from meho_backplane.version import deployed_version_label

__all__ = [
    "DEFAULT_POLICIES",
    "ImportPlan",
    "ImportResult",
    "SettingsExportRefusedError",
    "SettingsForbiddenError",
    "apply_import",
    "build_plan",
    "dump_settings_yaml",
    "export_settings",
    "list_history",
    "stored_secret_mistakes",
]

_log = structlog.get_logger(__name__)

#: Target entry keys, in file order (the ``TargetCreate`` keys plus extras).
TARGET_KEYS: Final[tuple[str, ...]] = (
    "name",
    "product",
    "version",
    "host",
    "port",
    "fqdn",
    "aliases",
    "auth_model",
    "secret_ref",
    "secret_field_map",
    "verify_tls",
    "tls_ca_pin",
    "tls_server_name",
    "vpn_required",
    "preferred_impl_id",
    "flight_recorder_capture",
    "notes",
    "extras",
)

_conventions = ConventionsService()


class SettingsForbiddenError(Exception):
    """The caller may not import (or preview) this file. The route answers 403."""


class SettingsExportRefusedError(Exception):
    """Stored data holds a secret, so the file is not written. The route answers 409.

    ``mistakes`` names each path (never the value), so an admin can move the
    secret into OpenBao and export again.
    """

    def __init__(self, mistakes: list[str]) -> None:
        super().__init__("; ".join(mistakes))
        self.mistakes = mistakes


# ---------------------------------------------------------------------------
# Export shape
# ---------------------------------------------------------------------------


def _policies_block(tenant: Tenant) -> dict[str, Any]:
    return {
        "announce_gate_enabled": tenant.announce_gate_enabled,
        "flight_recorder": {
            "enabled": tenant.flight_recorder_enabled,
            "retention_days": tenant.flight_recorder_retention_days,
            "agent_readable": tenant.flight_recorder_agent_readable,
        },
        "mail_recipient_allowlist": tenant.mail_recipient_allowlist,
        "run_approval_target_kinds": tenant.run_approval_target_kinds_enabled,
    }


def _convention_block(row: TenantConvention) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "title": row.title,
        "body": row.body,
        "kind": row.kind,
        "priority": row.priority,
    }


def _derived_secret_ref(tenant_id: uuid.UUID, name: str) -> str | None:
    try:
        return tenant_secret_ref(tenant_id, name)
    except ValueError:
        return None


def _canonical_target(tenant_id: uuid.UUID, values: dict[str, Any]) -> dict[str, Any]:
    """Order *values* as :data:`TARGET_KEYS`; drop a ``secret_ref`` MEHO can derive."""
    block = {key: values[key] for key in TARGET_KEYS if key in values}
    if "secret_ref" in block and block["secret_ref"] == _derived_secret_ref(
        tenant_id, block["name"]
    ):
        del block["secret_ref"]
    return block


def _target_block(row: TargetORM) -> dict[str, Any]:
    dumped = _to_full(row).model_dump(mode="json")
    return _canonical_target(row.tenant_id, {key: dumped[key] for key in TARGET_KEYS})


async def _load_state(
    session: AsyncSession,
    tenant_ids: list[uuid.UUID],
) -> tuple[
    dict[uuid.UUID, Tenant],
    dict[uuid.UUID, list[TenantConvention]],
    dict[uuid.UUID, list[TargetORM]],
]:
    tenants = {
        t.id: t
        for t in (await session.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars()
    }
    conventions: dict[uuid.UUID, list[TenantConvention]] = {tid: [] for tid in tenant_ids}
    for row in (
        await session.execute(
            select(TenantConvention)
            .where(TenantConvention.tenant_id.in_(tenant_ids))
            .order_by(TenantConvention.slug)
        )
    ).scalars():
        conventions[row.tenant_id].append(row)
    targets: dict[uuid.UUID, list[TargetORM]] = {tid: [] for tid in tenant_ids}
    for target in (
        await session.execute(
            select(TargetORM)
            .where(TargetORM.tenant_id.in_(tenant_ids), TargetORM.deleted_at.is_(None))
            .order_by(TargetORM.name)
        )
    ).scalars():
        targets[target.tenant_id].append(target)
    return tenants, conventions, targets


async def load_tenant_blocks(
    session: AsyncSession,
    tenant_ids: list[uuid.UUID],
) -> dict[str, dict[str, Any]]:
    """The export block of each *existing* tenant in *tenant_ids*, keyed by id."""
    tenants, conventions, targets = await _load_state(session, tenant_ids)
    return {
        str(tid): {
            "id": str(tid),
            "policies": _policies_block(tenants[tid]),
            "conventions": [_convention_block(c) for c in conventions[tid]],
            "targets": [_target_block(t) for t in targets[tid]],
        }
        for tid in sorted(tenants, key=str)
    }


async def _migration_head(session: AsyncSession) -> str | None:
    try:
        return (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception:
        return None


def stored_secret_mistakes(blocks: dict[str, dict[str, Any]]) -> list[str]:
    """One sentence per stored value that looks like a secret, in *blocks*.

    *blocks* is a spec view (tenant id -> export block). Paths name the tenant
    id, the convention slug and the target name, since the stored data has no
    file positions: ``stored.<tenant>.targets.vc-01.extras.password``.
    """
    mistakes: list[str] = []
    for tid, block in blocks.items():
        by_key = {
            "policies": block.get("policies"),
            "conventions": {c["slug"]: c for c in block.get("conventions") or []},
            "targets": {t["name"]: t for t in block.get("targets") or []},
        }
        mistakes.extend(find_secret_shaped_values(by_key, ("stored", tid)))
    return mistakes


#: Leads the mistakes when stored data, not the file, holds a secret.
STORED_SECRET_LEAD: Final[str] = (
    "MEHO already stores a value that looks like a secret for a tenant this file "
    "names; the preview, the import and the history never show or keep one, so move "
    "it into OpenBao (change the target or convention through its own route) and try "
    "again."
)


async def export_settings(session: AsyncSession, tenant_ids: list[uuid.UUID]) -> dict[str, Any]:
    """Build the whole settings document for *tenant_ids* from the live state.

    Raises :class:`SettingsExportRefusedError` when stored data (a target's
    ``notes`` or ``extras``, a convention body, ...) holds something the import
    would refuse as a secret: the file must never carry one.
    """
    blocks = await load_tenant_blocks(session, tenant_ids)
    spec = {"tenants": list(blocks.values())}
    mistakes = find_secret_shaped_values(spec, ("spec",))
    if mistakes:
        raise SettingsExportRefusedError(mistakes)
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "exportedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exportedFrom": {
                "mehoVersion": deployed_version_label(),
                "migrationHead": await _migration_head(session),
            },
        },
        "spec": spec,
    }


class _NoAliasDumper(yaml.SafeDumper):  # type: ignore[misc]  # yaml has no stubs here
    """``SafeDumper`` that writes a value out each time it appears.

    The import refuses anchors and aliases, so the export never writes one,
    even when one object appears twice in the document.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_settings_yaml(document: dict[str, Any]) -> str:
    """Render *document* as YAML; stable for equal input (key order kept)."""
    return str(
        yaml.dump(
            document,
            Dumper=_NoAliasDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=100,
        )
    )


# ---------------------------------------------------------------------------
# Plan (the preview)
# ---------------------------------------------------------------------------


@dataclass
class _TenantPlan:
    tenant_id: uuid.UUID
    index: int
    exists: bool
    policies: dict[str, Any] | None = None
    #: (index, slug, ConventionCreate | ConventionUpdate)
    conventions: list[tuple[int, str, ConventionCreate | ConventionUpdate]] = field(
        default_factory=list
    )
    #: (index, name, TargetCreate | TargetUpdate)
    targets: list[tuple[int, str, TargetCreate | TargetUpdate]] = field(default_factory=list)


@dataclass
class ImportPlan:
    """What a file would change, or why it cannot be imported."""

    mistakes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diff: dict[str, Any] | None = None
    tenants: list[_TenantPlan] = field(default_factory=list)
    before: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_sha256: str = ""
    api_version: str = API_VERSION
    exported_from: dict[str, Any] | None = None


def _detail_sentence(path: str, detail: Any) -> str:
    """One sentence from a route's ``HTTPException.detail``, prefixed with *path*."""
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("kind") or "was refused"
    elif isinstance(detail, list):
        message = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    else:
        message = str(detail)
    message = message.strip().rstrip(".")
    if contains_secret(message):
        # A route message may quote the value it refused; never repeat a secret.
        return f"`{path}` was refused."
    return f"`{path}`: {message}."


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", value or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _newer_export_warning(model: SettingsFile, current_head: str | None) -> str | None:
    exported = model.metadata.exportedFrom if model.metadata else None
    if exported is None:
        return None
    file_version = _version_tuple(exported.mehoVersion)
    here_version = _version_tuple(deployed_version_label())
    newer_version = file_version and here_version and file_version > here_version
    newer_head = (
        exported.migrationHead is not None
        and current_head is not None
        and exported.migrationHead.isdigit()
        and current_head.isdigit()
        and int(exported.migrationHead) > int(current_head)
    )
    if not (newer_version or newer_head):
        return None
    return (
        f"This file was exported from a newer MEHO ({exported.mehoVersion or 'unknown'}, "
        f"migration {exported.migrationHead or 'unknown'}) than this one "
        f"({deployed_version_label()}, migration {current_head or 'unknown'}); the import "
        f"still runs, and keys this MEHO does not know are listed as mistakes."
    )


def _operator_for(operator: Operator, tenant_id: uuid.UUID) -> Operator:
    if tenant_id == operator.tenant_id:
        return operator
    return operator.model_copy(update={"tenant_id": tenant_id})


def _authorize(operator: Operator, tenant_ids: list[uuid.UUID]) -> None:
    """A tenant_admin may import only their own tenant; more needs platform_admin."""
    if operator.platform_admin:
        return
    if len(tenant_ids) > 1 or any(tid != operator.tenant_id for tid in tenant_ids):
        raise SettingsForbiddenError(
            "Only a platform_admin may import a file that names another tenant or "
            "more than one tenant; a tenant_admin may import a file for their own tenant only."
        )


def _plan_policies(
    tenant: SettingsTenant,
    current: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return (desired policies block, the updates to write or None)."""
    desired = copy.deepcopy(current)
    if "policies" not in tenant.model_fields_set:
        return desired, None
    policies = tenant.policies
    if policies is None:
        return copy.deepcopy(DEFAULT_POLICIES), copy.deepcopy(DEFAULT_POLICIES)
    sent = policies.model_fields_set
    if "announce_gate_enabled" in sent:
        desired["announce_gate_enabled"] = bool(policies.announce_gate_enabled)
    if "flight_recorder" in sent:
        recorder = policies.flight_recorder
        if recorder is None:
            desired["flight_recorder"] = copy.deepcopy(DEFAULT_POLICIES["flight_recorder"])
        else:
            for key in recorder.model_fields_set:
                value = getattr(recorder, key)
                desired["flight_recorder"][key] = bool(value) if key == "enabled" else value
    if "mail_recipient_allowlist" in sent:
        desired["mail_recipient_allowlist"] = policies.mail_recipient_allowlist
    if "run_approval_target_kinds" in sent:
        desired["run_approval_target_kinds"] = bool(policies.run_approval_target_kinds)
    return desired, desired


def _plan_conventions(
    loc: str,
    entries: list[SettingsConvention],
    current: dict[str, dict[str, Any]],
    plan: _TenantPlan,
    mistakes: list[str],
) -> list[dict[str, Any]]:
    desired = {slug: dict(block) for slug, block in current.items()}
    for i, entry in enumerate(entries):
        path = f"{loc}.conventions[{i}]"
        sent = entry.model_fields_set
        existing = current.get(entry.slug)
        if existing is None:
            missing = [key for key in ("title", "kind") if key not in sent]
            for key in missing:
                mistakes.append(f"`{path}.{key}` is required for a new convention.")
            if missing:
                continue
            block: dict[str, Any] = {
                "slug": entry.slug,
                "title": entry.title,
                "body": entry.body,
                "kind": entry.kind.value,
                "priority": entry.priority,
            }
            action: ConventionCreate | ConventionUpdate = ConventionCreate(
                slug=entry.slug,
                title=entry.title,
                body=entry.body,
                kind=entry.kind,
                priority=entry.priority,
            )
        else:
            if "kind" in sent and entry.kind.value != existing["kind"]:
                mistakes.append(
                    f"`{path}.kind` cannot change on import (it is {existing['kind']}); "
                    f"delete the convention and import it again."
                )
                continue
            block = dict(existing)
            for key in ("title", "body", "priority"):
                if key in sent:
                    block[key] = getattr(entry, key)
            if block == existing:
                continue
            action = ConventionUpdate(
                **{key: block[key] for key in ("title", "body", "priority") if key in sent}
            )
        try:
            kind = ConventionKind(block["kind"])
        except ValueError:
            kind = ConventionKind.REFERENCE  # same fallback as ConventionsService
        try:
            enforce_budget(block["title"], block["body"], kind)
        except OverBudgetError as exc:
            mistakes.append(
                f"`{path}.body` is too long for the session preamble "
                f"(about {exc.estimated} tokens, the limit is {exc.budget}); shorten it."
            )
            continue
        desired[entry.slug] = block
        plan.conventions.append((i, entry.slug, action))
    return [desired[slug] for slug in sorted(desired)]


def _plan_new_target(
    path: str,
    loc_tuple: tuple[int | str, ...],
    entry: SettingsTarget,
    operator: Operator,
    mistakes: list[str],
) -> tuple[dict[str, Any], TargetCreate] | None:
    fields = {key: getattr(entry, key) for key in entry.model_fields_set}
    try:
        body = TargetCreate(**fields)
    except ValidationError as exc:
        mistakes.extend(format_validation_errors(exc, loc_tuple))
        return None
    try:
        product = _canonicalise_and_validate_product(body.product)
        _validate_preferred_impl_id(body.preferred_impl_id, product)
        secret_ref = _resolve_create_secret_ref(body, operator)
    except HTTPException as exc:
        mistakes.append(_detail_sentence(path, exc.detail))
        return None
    values = body.model_dump(mode="json")
    values["product"] = product
    values["secret_ref"] = secret_ref
    return _canonical_target(operator.tenant_id, values), body


def _plan_existing_target(
    path: str,
    loc_tuple: tuple[int | str, ...],
    entry: SettingsTarget,
    row: TargetORM,
    operator: Operator,
    mistakes: list[str],
) -> tuple[dict[str, Any], TargetUpdate | None] | None:
    fields: dict[str, Any] = {
        key: getattr(entry, key) for key in entry.model_fields_set if key != "name"
    }
    if fields.get("secret_field_map") == {}:
        fields["secret_field_map"] = None  # the PATCH's way to clear the map
    try:
        body = TargetUpdate(**fields)
    except ValidationError as exc:
        mistakes.extend(format_validation_errors(exc, loc_tuple))
        return None
    if body.secret_field_map:
        # The file holds the whole map, but the PATCH merges, so stored entries
        # the file leaves out are sent as removals. They go in after validation:
        # the field's entry limit would count them, while the limit that matters
        # is on the final map, which merge_secret_field_map checks below.
        stale = {k: None for k in row.secret_field_map or {} if k not in body.secret_field_map}
        body.secret_field_map = {**stale, **body.secret_field_map}
    updates = body.model_dump(exclude_unset=True)
    try:
        _enforce_tls_trust_exclusion(
            updates.get("tls_ca_pin", row.tls_ca_pin),
            updates.get("verify_tls", row.verify_tls),
        )
        _validate_update_body_fields(dict(updates), row, operator)
    except HTTPException as exc:
        mistakes.append(_detail_sentence(path, exc.detail))
        return None
    current = _to_full(row).model_dump(mode="json")
    merged = {key: current[key] for key in TARGET_KEYS}
    merged.update(body.model_dump(exclude_unset=True, mode="json"))
    if "secret_field_map" in updates:
        merged["secret_field_map"] = (
            merge_secret_field_map(row.secret_field_map, updates["secret_field_map"]) or {}
        )
    desired = _canonical_target(row.tenant_id, merged)
    if desired == _target_block(row):
        return desired, None
    return desired, body


def _plan_targets(
    ti: int,
    entries: list[SettingsTarget],
    rows: list[TargetORM],
    operator: Operator,
    plan: _TenantPlan,
    mistakes: list[str],
) -> list[dict[str, Any]]:
    by_name = {row.name: row for row in rows}
    desired = {row.name: _target_block(row) for row in rows}
    for i, entry in enumerate(entries):
        loc_tuple: tuple[int | str, ...] = ("spec", "tenants", ti, "targets", i)
        path = f"spec.tenants[{ti}].targets[{i}]"
        row = by_name.get(entry.name)
        if row is None:
            planned_new = _plan_new_target(path, loc_tuple, entry, operator, mistakes)
            if planned_new is not None:
                desired[entry.name], create_body = planned_new
                plan.targets.append((i, entry.name, create_body))
            continue
        planned = _plan_existing_target(path, loc_tuple, entry, row, operator, mistakes)
        if planned is None:
            continue
        desired[entry.name], update_body = planned
        if update_body is not None:
            plan.targets.append((i, entry.name, update_body))
    return [desired[name] for name in sorted(desired)]


async def build_plan(
    session: AsyncSession,
    operator: Operator,
    content: bytes,
) -> ImportPlan:
    """Check *content* and work out the diff. Reads the database; writes nothing.

    Raises :class:`SettingsForbiddenError` when the caller may not import the
    tenants the file names (the preview has the same rules as the apply).
    """
    plan = ImportPlan(file_sha256=hashlib.sha256(content).hexdigest())
    # In a worker thread: parsing a large file must not hold up other requests.
    parsed = await asyncio.to_thread(parse_settings_file, content)
    plan.mistakes.extend(parsed.mistakes)
    model = parsed.model
    if model is None:
        return plan
    plan.api_version = model.apiVersion
    if model.metadata and model.metadata.exportedFrom:
        plan.exported_from = model.metadata.exportedFrom.model_dump()

    tenant_ids = [t.id or operator.tenant_id for t in model.spec.tenants]
    _authorize(operator, tenant_ids)

    warning = _newer_export_warning(model, await _migration_head(session))
    if warning:
        plan.warnings.append(warning)

    tenants, conventions, targets = await _load_state(session, tenant_ids)
    plan.before = {
        str(tid): {
            "id": str(tid),
            "policies": _policies_block(tenants[tid]),
            "conventions": [_convention_block(c) for c in conventions[tid]],
            "targets": [_target_block(t) for t in targets[tid]],
        }
        for tid in tenant_ids
        if tid in tenants
    }
    stored = stored_secret_mistakes(plan.before)
    if stored:
        # A diff shows "before" values and a revision keeps them for good, so a
        # stored secret must stop here, as the export does (no diff, no write).
        plan.mistakes.extend([STORED_SECRET_LEAD, *stored])
        plan.before = {}
        return plan

    desired_view: dict[str, dict[str, Any]] = {}
    in_file: dict[str, dict[str, set[str]]] = {}
    for ti, (entry, tid) in enumerate(zip(model.spec.tenants, tenant_ids, strict=True)):
        tenant_plan = _TenantPlan(tenant_id=tid, index=ti, exists=tid in tenants)
        tenant_operator = _operator_for(operator, tid)
        current = plan.before.get(str(tid)) or {
            "id": str(tid),
            "policies": copy.deepcopy(DEFAULT_POLICIES),
            "conventions": [],
            "targets": [],
        }
        policies, tenant_plan.policies = _plan_policies(entry, current["policies"])
        if tenant_plan.policies == current["policies"]:
            tenant_plan.policies = None
        desired_view[str(tid)] = {
            "id": str(tid),
            "policies": policies,
            "conventions": _plan_conventions(
                f"spec.tenants[{ti}]",
                entry.conventions or [],
                {c["slug"]: c for c in current["conventions"]},
                tenant_plan,
                plan.mistakes,
            ),
            "targets": _plan_targets(
                ti,
                entry.targets or [],
                targets.get(tid, []),
                tenant_operator,
                tenant_plan,
                plan.mistakes,
            ),
        }
        in_file[str(tid)] = {
            "conventions": {c.slug for c in entry.conventions or []},
            "targets": {t.name for t in entry.targets or []},
        }
        plan.tenants.append(tenant_plan)

    if plan.mistakes:
        return plan

    tenants_not_in_file: list[str] = []
    if operator.platform_admin:
        all_ids = (await session.execute(select(Tenant.id))).scalars()
        tenants_not_in_file = sorted(str(t) for t in all_ids if t not in tenant_ids)
    plan.diff = diff_specs(
        plan.before,
        desired_view,
        in_file=in_file,
        tenants_not_in_file=tenants_not_in_file,
    )
    return plan


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """What an applied import did."""

    plan: ImportPlan
    applied: bool = False
    revision_id: uuid.UUID | None = None
    diff: dict[str, Any] | None = None
    failed: list[str] = field(default_factory=list)


async def _apply_tenant_rows(
    session: AsyncSession, plan: ImportPlan, failed: list[str]
) -> set[uuid.UUID]:
    """Create missing tenants; return the ids that could not be created."""
    broken: set[uuid.UUID] = set()
    for tenant_plan in plan.tenants:
        if tenant_plan.exists:
            continue
        try:
            async with session.begin_nested():
                await ensure_tenant(tenant_plan.tenant_id, session)
        except Exception as exc:
            _log.warning(
                "settings_import_tenant_failed",
                tenant_id=str(tenant_plan.tenant_id),
                error=str(exc),
            )
            failed.append(
                f"`spec.tenants[{tenant_plan.index}]` could not be created; "
                "nothing for it was written."
            )
            broken.add(tenant_plan.tenant_id)
    return broken


async def _apply_policies(
    session: AsyncSession, tenant_plan: _TenantPlan, failed: list[str]
) -> None:
    policies = tenant_plan.policies
    if policies is None:
        return
    try:
        async with session.begin_nested():
            tenant = await session.get(Tenant, tenant_plan.tenant_id)
            if tenant is None:
                raise LookupError("tenant row missing")
            set_announce_gate_enabled(tenant, policies["announce_gate_enabled"])
            recorder = policies["flight_recorder"]
            set_flight_recorder_policy(
                tenant,
                {
                    "flight_recorder_enabled": recorder["enabled"],
                    "flight_recorder_retention_days": recorder["retention_days"],
                    "flight_recorder_agent_readable": recorder["agent_readable"],
                },
            )
            set_mail_recipient_allowlist(tenant, policies["mail_recipient_allowlist"])
            set_run_approval_target_kinds(tenant, policies["run_approval_target_kinds"])
    except Exception as exc:
        _log.warning(
            "settings_import_policies_failed", tenant_id=str(tenant_plan.tenant_id), error=str(exc)
        )
        failed.append(f"`spec.tenants[{tenant_plan.index}].policies` could not be written.")


async def _apply_conventions(
    session: AsyncSession,
    operator: Operator,
    tenant_plan: _TenantPlan,
    failed: list[str],
) -> None:
    # Each history row records ``request_id`` (#3747), so the rows of one
    # import link to its request; they no longer carry an ``audit_id``.
    for i, slug, action in tenant_plan.conventions:
        path = f"spec.tenants[{tenant_plan.index}].conventions[{i}]"
        try:
            async with session.begin_nested():
                if isinstance(action, ConventionCreate):
                    await _conventions.create_convention(
                        session=session, operator=operator, body=action
                    )
                else:
                    await _conventions.update_convention(
                        session=session,
                        operator=operator,
                        slug=slug,
                        body=action,
                    )
        except Exception as exc:
            _log.warning("settings_import_convention_failed", slug=slug, error=str(exc))
            reason = "" if contains_secret(str(exc)) else f": {exc}"
            failed.append(f"`{path}` ({slug}) could not be written{reason}.")


async def _apply_targets(
    session: AsyncSession,
    operator: Operator,
    tenant_plan: _TenantPlan,
    failed: list[str],
) -> None:
    for i, name, body in tenant_plan.targets:
        path = f"spec.tenants[{tenant_plan.index}].targets[{i}]"
        try:
            async with session.begin_nested():
                if isinstance(body, TargetCreate):
                    await create_target(body=body, operator=operator, session=session)
                else:
                    await update_target(name=name, body=body, operator=operator, session=session)
        except HTTPException as exc:
            failed.append(_detail_sentence(path, exc.detail))
        except Exception as exc:
            _log.warning("settings_import_target_failed", name=name, error=str(exc))
            failed.append(f"`{path}` ({name}) could not be written.")


async def apply_import(
    session: AsyncSession,
    operator: Operator,
    content: bytes,
) -> ImportResult:
    """Apply *content*: add or update, never delete. See the module docstring.

    Writes nothing when the file has mistakes (``result.plan.mistakes``) or
    changes nothing (empty diff, no revision). The caller's session dependency
    commits the one transaction after the route returns.
    """
    plan = await build_plan(session, operator, content)
    result = ImportResult(plan=plan)
    if plan.mistakes or plan.diff is None or is_empty(plan.diff):
        result.diff = plan.diff
        return result

    audit_id = uuid.uuid4()
    bind_preallocated_audit_id(audit_id)
    structlog.contextvars.bind_contextvars(audit_op_class="write")
    tenant_ids = [tp.tenant_id for tp in plan.tenants]

    broken = await _apply_tenant_rows(session, plan, result.failed)
    live = [tp for tp in plan.tenants if tp.tenant_id not in broken]
    for tenant_plan in live:
        await _apply_policies(session, tenant_plan, result.failed)
    for tenant_plan in live:
        await _apply_conventions(
            session,
            _operator_for(operator, tenant_plan.tenant_id),
            tenant_plan,
            result.failed,
        )
    for tenant_plan in live:
        await _apply_targets(
            session, _operator_for(operator, tenant_plan.tenant_id), tenant_plan, result.failed
        )

    await session.flush()
    after = await load_tenant_blocks(session, tenant_ids)
    actual = diff_specs(plan.before, after)
    result.diff = actual
    result.applied = not is_empty(actual)
    summary = summarise(actual)
    if result.applied:
        revision = InstanceSettingsRevision(
            id=uuid.uuid4(),
            ts=datetime.now(UTC),
            actor_sub=operator.sub,
            audit_id=audit_id,
            file_sha256=plan.file_sha256,
            api_version=plan.api_version,
            exported_from=plan.exported_from,
            spec_before={"tenants": list(plan.before.values())},
            spec_after={"tenants": list(after.values())},
            summary=summary,
        )
        session.add(revision)
        await session.flush()
        result.revision_id = revision.id

    structlog.contextvars.bind_contextvars(
        audit_settings_import=True,
        audit_settings_file_sha256=plan.file_sha256,
        audit_settings_revision_id=str(result.revision_id) if result.revision_id else "",
        audit_settings_tenant_ids=[str(t) for t in tenant_ids],
        audit_settings_summary=summary,
        audit_settings_failed=len(result.failed),
    )
    _log.info(
        "settings_imported",
        revision_id=str(result.revision_id) if result.revision_id else None,
        tenants=len(tenant_ids),
        failed=len(result.failed),
        summary=summary,
    )
    return result


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _view(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {block["id"]: block for block in spec.get("tenants", [])}


async def list_history(
    session: AsyncSession,
    operator: Operator,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """The revisions the caller may see, newest first, each with its diff.

    A platform_admin who is a person sees every revision. Everyone else, also
    a machine principal whose token carries the platform-admin claim, sees the
    revisions that touched only their own tenant (a revision's spec holds every
    tenant it touched, so a multi-tenant one would show other tenants'
    settings).
    """
    every_tenant = operator.platform_admin and is_human_principal(operator)
    rows = (
        await session.execute(
            select(InstanceSettingsRevision).order_by(
                InstanceSettingsRevision.ts.desc(), InstanceSettingsRevision.id.desc()
            )
        )
    ).scalars()
    own = str(operator.tenant_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        before, after = _view(row.spec_before), _view(row.spec_after)
        if not every_tenant and set(before) | set(after) != {own}:
            continue
        out.append(
            {
                "id": str(row.id),
                "ts": row.ts,
                "actor_sub": row.actor_sub,
                "audit_id": str(row.audit_id),
                "file_sha256": row.file_sha256,
                "api_version": row.api_version,
                "exported_from": row.exported_from,
                "summary": row.summary,
                "diff": diff_specs(before, after),
            }
        )
        if len(out) >= limit:
            break
    return out

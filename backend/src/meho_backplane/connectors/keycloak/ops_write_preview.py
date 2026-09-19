# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builders for the Keycloak write ops.

G3.13 follow-up (#1857). Wires bespoke, redaction-correct preview builders
for the three Keycloak write ops whose blast radius a reviewer most needs
to see at approval-park time onto the per-op builder hook shipped by #1437
(:mod:`meho_backplane.operations._preview`):

================================  ===================================================
op_id                             preview stored in ``ApprovalRequest.proposed_effect``
================================  ===================================================
``keycloak.realm.create``         ``{realm, representation}`` (realm body, scrubbed)
``keycloak.user.create``          ``{username, realm, representation}`` (user, scrubbed)
``keycloak.role_mapping.assign``  ``{username, id, realm, granted_roles}`` (role names)
================================  ===================================================

Why bespoke builders on top of the generic params-echo default (#1856)
======================================================================

#1856 gives every approval-gated op a generic params-echo default — the
requested params echoed, redacted by key-name + value-shape. That already
gives keycloak param-level legibility. These builders layer a *richer,
resource-centric* view on top: they hoist the human-meaningful identity of
the resource being created/modified (the realm name, the username, the
granted role names) to top-level keys so the reviewer reads "creating user
``svc-meho`` in realm ``meho``" rather than having to dig the username out
of a nested ``representation`` dict. The credential material rides along in
the echoed representation only as the ``***REDACTED***`` sentinel.

Redaction discipline (critical)
===============================

Two of these ops carry secret material in their request params:

* ``keycloak.user.create`` — an *inline* password can ride in
  ``representation.credentials`` (a list of CredentialRepresentation, each
  with a raw ``value``). (The connector's preferred path sources the
  password from Vault via ``password_secret_ref`` so it is never an inline
  param at all — but a caller *may* pass ``credentials`` inline, and the
  preview must scrub it either way.)
* A client/user representation may also carry a ``secret`` field.

Each builder runs the representation through
:func:`~meho_backplane.connectors.keycloak.redaction.redact_secret_fields`
— the *same* single-sourced scrub the read ops use — which replaces the
Keycloak representation secret fields (``secret`` / ``credentials`` /
``value`` / ``secretData`` / ``credentialData``) **and** the generic
credential param spellings (``password`` / ``client_secret`` / ``token`` /
… — the same set the generic params-echo default scrubs) with
``***REDACTED***`` wherever they appear, recursively and
case-insensitively. The ``password`` coverage matters because a
``RealmRepresentation.smtpServer`` is a ``Map<String,String>`` carrying the
SMTP relay password under the ``password`` key, and a representation is
``additionalProperties: true``. So the durable approval row surfaces the
resource shape (username, email, enabled, attributes, realm config)
without any secret value ever landing in it — and the bespoke builders are
at least as strict as the generic echo they bypass.

Because ``keycloak.user.create`` classifies as ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`), the
generic params-echo default is suppressed for it — only a *bespoke* builder
that owns its own field discipline is trusted to run for a credential-class
op (the same trust model the permission-preflight hook already relies on,
G0.20-T4 #1504). ``keycloak.realm.create`` and ``keycloak.role_mapping.assign``
classify as plain ``write`` (not sensitive), so they would already get the
generic echo; the bespoke builder simply gives a cleaner resource view.

Fail-soft
=========

Most builders here are pure (no connector I/O) — they read only
``ctx.params`` and echo a scrubbed view, so they cannot fault on a network
call. The one exception is ``_group_update_attributes_preview`` (#3280),
which fetches the group's **current** attributes so the approver can see the
before→after delta (a tenant-claim write must show what it removes). That
fetch is the seam's sanctioned before/after builder I/O (the same shape
``k8s.apply``'s dry-run uses) and is itself fail-soft: it catches every fault
and degrades to the incoming-only view (``current_attributes_available:
false``) rather than dropping the preview. Should any builder raise anyway,
:func:`~meho_backplane.operations._preview.build_proposed_effect` swallows
it into the explicit ``preview_unavailable`` marker (#1628) rather than
blocking the park, matching the existing builder contract.

References
----------

* Task: https://github.com/evoila/meho/issues/1857
* Parent initiative: https://github.com/evoila/meho/issues/1853
* Builder seam: G11.7 #1437; generic params-echo default: #1856.
* Keycloak read-op secret scrub (reused here): G3.13-T2 #1394.
* Keycloak write ops: G3.13-T4 #1406.
* Keycloak group-lifecycle write ops + the update_attributes before/after
  preview: https://github.com/evoila/meho/issues/3280.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak._paths import _GROUP_PATH, fill_path
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)


def _opt_str(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for absent/blank input."""
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _representation(ctx: PreviewContext) -> dict[str, Any]:
    """Return the dispatch's ``representation`` param as a dict (possibly empty).

    A non-dict / absent ``representation`` yields ``{}`` so a malformed
    param shape previews an empty resource rather than raising.
    """
    representation = ctx.params.get("representation")
    return representation if isinstance(representation, dict) else {}


def _managed_realm(ctx: PreviewContext) -> str | None:
    """Resolve the realm the write targets, for the reviewer-facing label.

    Prefers an explicit ``realm`` param, then the target's managed realm.
    Returns ``None`` when neither is resolvable (a fresh, unconfigured
    target) so the preview omits the key rather than guessing.
    """
    explicit = ctx.params.get("realm")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if ctx.target is None:
        return None
    try:
        return resolve_realm_config(ctx.target).managed_realm
    except Exception:
        # Target lacks a resolvable realm config (unfingerprinted); the
        # realm label is a convenience, not load-bearing — omit it.
        return None


async def _realm_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``keycloak.realm.create`` — surface the realm name + scrubbed body.

    The realm name is the human-meaningful identity of the resource being
    created (``RealmRepresentation.realm``); it is hoisted to a top-level
    key. The full RealmRepresentation is echoed with secret material
    scrubbed so the reviewer can read the realm's config (its smtp / login
    settings etc.) without any embedded secret leaking.
    """
    representation = _representation(ctx)
    realm_name = str(representation.get("realm") or ctx.params.get("realm") or "").strip()
    return {
        "resource": "realm",
        "realm": realm_name or None,
        "representation": redact_secret_fields(representation),
    }


async def _user_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``keycloak.user.create`` — username visible, password scrubbed.

    Hoists the username (``UserRepresentation.username``) and the target
    realm to top-level keys so the reviewer reads who is being created and
    where. The UserRepresentation is echoed with
    :func:`~meho_backplane.connectors.keycloak.redaction.redact_secret_fields`
    applied, so an inline password (carried in
    ``representation.credentials[].value``) shows as ``***REDACTED***`` while
    the visible identity fields (username / email / enabled / attributes)
    pass through. A ``password_secret_ref`` (the Vault-sourced path the
    connector prefers) carries no secret value itself, but is surfaced so
    the reviewer knows the password will be set from Vault.
    """
    representation = _representation(ctx)
    username = str(representation.get("username") or ctx.params.get("username") or "").strip()
    preview: dict[str, Any] = {
        "resource": "user",
        "username": username or None,
        "realm": _managed_realm(ctx),
        "representation": redact_secret_fields(representation),
    }
    secret_ref = ctx.params.get("password_secret_ref")
    if isinstance(secret_ref, str) and secret_ref.strip():
        # A path, not a secret — safe to surface so the reviewer sees the
        # password origin without ever seeing the value.
        preview["password_source"] = "vault"
        preview["password_secret_ref"] = secret_ref.strip()
    return preview


async def _role_mapping_assign_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``keycloak.role_mapping.assign`` — surface the granted roles.

    A privilege grant: the blast radius is *which roles* are being granted
    to *which user*. The role names (``params.roles``) and the user
    identity (``username`` and/or explicit ``id``) are hoisted so the
    reviewer reads the grant directly. Role names carry no secret material.
    """
    raw_roles = ctx.params.get("roles")
    roles = (
        [str(role).strip() for role in raw_roles if str(role).strip()]
        if isinstance(raw_roles, list)
        else []
    )
    return {
        "resource": "role_mapping",
        "username": _opt_str(ctx.params.get("username")),
        "id": _opt_str(ctx.params.get("id")),
        "realm": _managed_realm(ctx),
        "granted_roles": roles,
    }


def _attribute_keys(ctx: PreviewContext) -> list[str]:
    """Sorted attribute keys the group write will set (never the values).

    Reads ``ctx.params['attributes']`` (a ``Map<String, List<String>>``) and
    surfaces only its **keys** so the reviewer sees *which* attributes change
    (e.g. ``tenant_id`` / ``tenant_role``) without the durable row carrying a
    value. A non-dict / absent map yields ``[]``.
    """
    attributes = ctx.params.get("attributes")
    if not isinstance(attributes, dict):
        return []
    return sorted(str(key) for key in attributes)


async def _group_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``keycloak.group.create`` — group name + parent + attribute keys.

    Hoists the group name, the parent (if nested), and the target realm so
    the reviewer reads "creating group ``role-tenant-2`` in realm ``meho``".
    The full attribute map is echoed through
    :func:`~meho_backplane.connectors.keycloak.redaction.redact_secret_fields`
    (attributes are not secrets, but a stray ``password``-keyed attribute is
    scrubbed for defence in depth), plus a keys-only ``attribute_keys`` list
    for a quick read of which attributes are set.
    """
    return {
        "resource": "group",
        "name": _opt_str(ctx.params.get("name")),
        "parent_id": _opt_str(ctx.params.get("parent_id")),
        "realm": _managed_realm(ctx),
        "attribute_keys": _attribute_keys(ctx),
        "attributes": redact_secret_fields(ctx.params.get("attributes") or {}),
    }


def _norm_attrs(raw: Any) -> dict[str, list[str]]:
    """Coerce an attribute map to Keycloak's ``{key: [values]}`` shape for a diff.

    Mirrors ``ops_write_groups._normalise_attributes`` (a plain string is
    wrapped into a single-element list) so the before/after value comparison
    in :func:`_group_update_attributes_preview` compares like for like.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, list):
            out[key] = [str(item) for item in value]
        elif value is None:
            out[key] = []
        else:
            out[key] = [str(value)]
    return out


async def _fetch_current_group_attributes(ctx: PreviewContext) -> dict[str, Any] | None:
    """Read the group's **current** attributes for the before/after preview.

    Fail-soft, sanctioned builder I/O (the seam's before/after-spec contract,
    the same shape ``k8s.apply``'s dry-run uses): resolves the group by ``id``
    or by ``name`` (+ optional ``parent_id``) and GETs its representation via
    the connector's admin-auth helper. Returns the current ``attributes`` map
    (``{}`` when the group has none), or ``None`` when the current state
    cannot be read — no connector instance / target (unit-context preview),
    an unresolvable name, or any transport fault. A ``None`` degrades the
    preview to the incoming-only view rather than blocking the park.
    """
    connector = ctx.connector_instance
    target = ctx.target
    if connector is None or target is None:
        return None
    # Lazy import: the operations package must not import a specific connector
    # at module load, and this only runs at park time (all modules loaded).
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    if not isinstance(connector, KeycloakConnector):
        return None
    try:
        realm = resolve_realm_config(target).managed_realm
        group_id = _opt_str(ctx.params.get("id"))
        if group_id is None:
            name = _opt_str(ctx.params.get("name"))
            if name is None:
                return None
            parent_id = _opt_str(ctx.params.get("parent_id"))
            row = await connector._find_group(target, realm, name, parent_id, operator=ctx.operator)
            resolved = row.get("id") if isinstance(row, dict) else None
            if not isinstance(resolved, str) or not resolved:
                return None
            group_id = resolved
        current = await connector._get_admin_json(
            target,
            fill_path(_GROUP_PATH, {"realm": realm, "group-id": quote_segment(group_id)}),
            operator=ctx.operator,
        )
    except Exception:
        # Any resolution / transport fault degrades to the incoming-only view;
        # the seam's own guard still covers a raise, but degrading here keeps
        # the (safe, incoming) preview rather than dropping it wholesale.
        return None
    attrs = current.get("attributes") if isinstance(current, dict) else None
    return attrs if isinstance(attrs, dict) else {}


async def _group_update_attributes_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``keycloak.group.update_attributes`` — the before→after delta.

    A group-attribute write is a **tenant-claim write** (F07: ``tenant_id`` /
    ``tenant_role`` group attributes drive the membership-derived claims), so
    the approver must see not just the incoming payload but what it *removes*
    — a ``replace`` that drops ``tenant_id`` / ``tenant_role`` denies every
    member their claims. This builder fetches the group's **current**
    attributes (fail-soft, sanctioned builder I/O) and surfaces the delta:

    * ``current_attribute_keys`` — keys on the group now (before).
    * ``added_keys`` / ``removed_keys`` / ``changed_keys`` — the delta. A
      merge never removes a key (``removed_keys`` is empty); a ``replace``
      removes every current key absent from the payload.
    * ``warning`` — an explicit line, present only when ``replace=true`` drops
      keys, naming them (so a dropped ``tenant_id`` / ``tenant_role`` is
      loud, not silent).

    The diff is surfaced **keys-only** (``*_keys`` lists): the security signal
    for a tenant-claim write is the *presence/absence* of a key — a member
    fails closed the instant ``tenant_id`` disappears — and keys keep the
    durable approval row bounded. The actual (non-secret) values remain
    visible as scrubbed key→value maps: ``current_attributes`` (before) and
    ``attributes`` (the incoming payload / after values), both through
    ``redact_secret_fields`` so a stray ``password``-keyed attribute is
    ``***REDACTED***``. When the current state cannot be read,
    ``current_attributes_available`` is ``false`` and the preview degrades to
    the incoming-only view.
    """
    replace = bool(ctx.params.get("replace", False))
    incoming = _norm_attrs(ctx.params.get("attributes"))
    preview: dict[str, Any] = {
        "resource": "group_attributes",
        "id": _opt_str(ctx.params.get("id")),
        "name": _opt_str(ctx.params.get("name")),
        "realm": _managed_realm(ctx),
        "replace": replace,
        "attribute_keys": _attribute_keys(ctx),
        "attributes": redact_secret_fields(ctx.params.get("attributes") or {}),
    }

    current = await _fetch_current_group_attributes(ctx)
    if current is None:
        preview["current_attributes_available"] = False
        return preview

    current_norm = _norm_attrs(current)
    current_keys = set(current_norm)
    incoming_keys = set(incoming)
    added_keys = incoming_keys - current_keys
    changed_keys = {k for k in incoming_keys & current_keys if current_norm[k] != incoming[k]}
    if replace:
        removed_keys = current_keys - incoming_keys
        resulting_keys = incoming_keys
    else:
        # A merge only adds/overwrites — a current key never disappears.
        removed_keys = set()
        resulting_keys = current_keys | incoming_keys

    preview["current_attributes_available"] = True
    preview["current_attributes"] = redact_secret_fields(current)
    preview["current_attribute_keys"] = sorted(current_keys)
    preview["resulting_attribute_keys"] = sorted(resulting_keys)
    preview["added_keys"] = sorted(added_keys)
    preview["removed_keys"] = sorted(removed_keys)
    preview["changed_keys"] = sorted(changed_keys)
    if replace and removed_keys:
        preview["warning"] = (
            "replace=true will REMOVE these existing attribute keys not in the "
            f"payload: {', '.join(sorted(removed_keys))}. If this includes "
            "tenant_id / tenant_role, the group's members lose the "
            "membership-derived tenant claims (F07)."
        )
    return preview


def _group_membership_preview(action: str) -> Any:
    """Build a membership preview builder for the add/remove op (shared shape).

    A membership mutation's blast radius is *which user* joins/leaves *which
    group* — a tenant-access change. The builder hoists the user identity
    (``user_id`` and/or ``username``) and the group identity (``group_id``
    and/or ``group_name``) plus the realm. No secret material is present.
    """

    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        return {
            "resource": "group_membership",
            "action": action,
            "user_id": _opt_str(ctx.params.get("user_id")),
            "username": _opt_str(ctx.params.get("username")),
            "group_id": _opt_str(ctx.params.get("group_id")),
            "group_name": _opt_str(ctx.params.get("group_name")),
            "realm": _managed_realm(ctx),
        }

    return _builder


def _register_keycloak_preview_builders() -> None:
    """Wire the Keycloak park-time preview builders. Called at import time.

    The ops whose resource identity a reviewer most needs at park time
    register a bespoke builder; the remaining write ops fall through to the
    generic params-echo default (#1856). The group writes (#3280) each get a
    resource-centric builder that hoists the group / user identity and the
    attribute keys (values scrubbed).
    """
    register_preview_builder("keycloak.realm.create", _realm_create_preview)
    register_preview_builder("keycloak.user.create", _user_create_preview)
    register_preview_builder("keycloak.role_mapping.assign", _role_mapping_assign_preview)
    register_preview_builder("keycloak.group.create", _group_create_preview)
    register_preview_builder("keycloak.group.update_attributes", _group_update_attributes_preview)
    register_preview_builder("keycloak.group.member.add", _group_membership_preview("add"))
    register_preview_builder("keycloak.group.member.remove", _group_membership_preview("remove"))


_register_keycloak_preview_builders()

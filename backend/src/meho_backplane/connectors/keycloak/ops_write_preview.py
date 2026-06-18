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
— the *same* single-sourced scrub the read ops use — which replaces
``secret`` / ``credentials`` / ``value`` / ``secretData`` /
``credentialData`` (scalar or subtree) with ``***REDACTED***`` wherever they
appear, recursively. So the durable approval row surfaces the resource
shape (username, email, enabled, attributes, realm config) without any
secret value ever landing in it.

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

Every builder is pure (no connector I/O) — it reads only ``ctx.params`` and
echoes a scrubbed view — so it cannot fault on a network call. Should a
malformed param shape raise anyway,
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
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import resolve_realm_config
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


def _register_keycloak_preview_builders() -> None:
    """Wire the Keycloak park-time preview builders. Called at import time.

    Only the three ops whose resource identity a reviewer most needs at
    park time register a bespoke builder; the remaining write ops fall
    through to the generic params-echo default (#1856).
    """
    register_preview_builder("keycloak.realm.create", _realm_create_preview)
    register_preview_builder("keycloak.user.create", _user_create_preview)
    register_preview_builder("keycloak.role_mapping.assign", _role_mapping_assign_preview)


_register_keycloak_preview_builders()

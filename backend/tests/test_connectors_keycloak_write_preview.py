# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Keycloak park-time ``proposed_effect`` preview builders.

G3.13 follow-up (#1857). The builders in
:mod:`meho_backplane.connectors.keycloak.ops_write_preview` give the
approval reviewer a resource-centric, redaction-correct view of the realm /
user / role-grant a parked keycloak write would create.

Acceptance criteria (Issue #1857):

* Parking ``keycloak.user.create`` with an inline password shows the
  username (visible) + the password as ``***REDACTED***``.
* Parking ``keycloak.realm.create`` shows the realm name; a role-grant
  shows the granted roles.
* No secret material lands in the durable approval row.
* The builders are fail-soft, matching the existing builder contract.

The builders are pure (they read only ``ctx.params`` and echo a scrubbed
view, no connector I/O), so these tests need no network mock and run in
every CI lane. Importing the connector package wires the builders via the
``register_preview_builder`` import side-effect, so the tests exercise the
real registration path through :func:`build_proposed_effect`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

# Importing the package runs ops_write_preview's register_preview_builder
# calls (the import side-effect under test).
import meho_backplane.connectors.keycloak  # noqa: F401
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak.redaction import REDACTED
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
)


@dataclass
class _FakeDescriptor:
    """Minimal stand-in -- the hook only reads ``op_id``."""

    op_id: str


@dataclass
class _FakeTarget:
    """Stand-in keycloak target -- ``resolve_realm_config`` reads ``extras``."""

    extras: dict[str, Any]


def _operator() -> Operator:
    return Operator(
        sub="op-keycloak-preview-test",
        name="Keycloak Preview Test Operator",
        email=None,
        raw_jwt="op.keycloak.preview.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _ctx(op_id: str, params: dict[str, Any], *, managed_realm: str = "meho") -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(extras={"managed_realm": managed_realm}),
        params=params,
        connector_id="keycloak-1.x",
    )


def _no_secret_anywhere(effect: dict[str, Any], *secrets: str) -> None:
    """Assert none of *secrets* appears anywhere in the serialised effect."""
    blob = json.dumps(effect)
    for secret in secrets:
        assert secret not in blob, f"secret material {secret!r} leaked into the durable row"


# ---------------------------------------------------------------------------
# keycloak.user.create -- username visible, inline password scrubbed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_create_preview_shows_username_redacts_inline_password() -> None:
    """The headline criterion: username visible, inline password redacted."""
    ctx = _ctx(
        "keycloak.user.create",
        {
            "representation": {
                "username": "svc-meho",
                "email": "svc@example.test",
                "enabled": True,
                "credentials": [
                    {"type": "password", "value": "hunter2-super-secret", "temporary": False}
                ],
            }
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    # A bespoke builder runs even though keycloak.user.create classifies as
    # credential_write -- the credential-class gate suppresses only the
    # generic echo, not a trusted bespoke builder (#1857).
    assert effect["op_class"] == "credential_write"
    preview = effect["preview"]
    assert preview["resource"] == "user"
    assert preview["username"] == "svc-meho", "username must be visible to the reviewer"
    # The email + enabled identity fields pass through untouched.
    assert preview["representation"]["email"] == "svc@example.test"
    assert preview["representation"]["enabled"] is True
    # The whole credentials subtree is replaced wholesale with the sentinel.
    assert preview["representation"]["credentials"] == REDACTED
    _no_secret_anywhere(effect, "hunter2-super-secret")


@pytest.mark.asyncio
async def test_user_create_preview_surfaces_vault_password_ref_without_value() -> None:
    """A Vault-sourced password ref is surfaced (it is a path, not a secret)."""
    ctx = _ctx(
        "keycloak.user.create",
        {
            "representation": {"username": "svc-meho", "enabled": True},
            "password_secret_ref": "kv/data/keycloak/svc-meho",
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["username"] == "svc-meho"
    assert preview["password_source"] == "vault"
    assert preview["password_secret_ref"] == "kv/data/keycloak/svc-meho"


@pytest.mark.asyncio
async def test_user_create_preview_realm_from_target() -> None:
    """The user-create preview labels the managed realm from the target."""
    ctx = _ctx(
        "keycloak.user.create",
        {"representation": {"username": "alice"}},
        managed_realm="evba",
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["realm"] == "evba"


# ---------------------------------------------------------------------------
# keycloak.realm.create -- realm name visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realm_create_preview_shows_realm_name() -> None:
    """Parking realm.create surfaces the realm name + a scrubbed representation."""
    ctx = _ctx(
        "keycloak.realm.create",
        {
            "representation": {
                "realm": "meho-prod",
                "enabled": True,
                "smtpServer": {"host": "smtp.test", "password": "smtp-pass"},
            }
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    # realm.create classifies as plain `write` (not credential-class).
    assert effect["op_class"] == "write"
    preview = effect["preview"]
    assert preview["resource"] == "realm"
    assert preview["realm"] == "meho-prod", "realm name must be visible"
    assert preview["representation"]["enabled"] is True
    # A `RealmRepresentation.smtpServer` is a Map<String,String> that stores
    # the SMTP relay password under the `password` key, and the
    # representation is `additionalProperties: true`. The bespoke builder
    # MUST scrub it -- the durable approval row never holds a cleartext
    # secret (#1857 acceptance criterion / B1 fix). Non-secret smtp fields
    # (host) pass through so the reviewer still reads the smtp config.
    smtp = preview["representation"]["smtpServer"]
    assert smtp["host"] == "smtp.test"
    assert smtp["password"] == REDACTED, "smtpServer.password must be redacted"
    _no_secret_anywhere(effect, "smtp-pass")


@pytest.mark.asyncio
async def test_realm_create_preview_redacts_nested_secret_field() -> None:
    """A `secret` nested in the realm representation is scrubbed wholesale."""
    ctx = _ctx(
        "keycloak.realm.create",
        {
            "representation": {
                "realm": "meho",
                "clients": [{"clientId": "web", "secret": "client-secret-value"}],
            }
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["representation"]["clients"][0]["clientId"] == "web"
    assert preview["representation"]["clients"][0]["secret"] == REDACTED
    _no_secret_anywhere(effect, "client-secret-value")


@pytest.mark.asyncio
async def test_realm_create_preview_redacts_generic_credential_keys() -> None:
    """`password`-class keys land anywhere in the additionalProperties body.

    A representation is ``additionalProperties: true``, so a credential can
    arrive under any of the generic credential spellings and under any
    casing. The bespoke builder must be at least as strict as the generic
    params-echo default it bypasses, so every such key is scrubbed.
    """
    ctx = _ctx(
        "keycloak.realm.create",
        {
            "representation": {
                "realm": "meho",
                "enabled": True,
                # camelCase smtp map password (already covered) + a few of
                # the generic credential spellings, including mixed casing
                # to prove the match is case-insensitive.
                "smtpServer": {"host": "smtp.test", "Password": "smtp-pass-mixed"},
                "attributes": {
                    "token": "tok-leak",
                    "client_secret": "cs-leak",
                    "private_key": "pk-leak",
                },
            }
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["representation"]["smtpServer"]["host"] == "smtp.test"
    assert preview["representation"]["smtpServer"]["Password"] == REDACTED
    attrs = preview["representation"]["attributes"]
    assert attrs["token"] == REDACTED
    assert attrs["client_secret"] == REDACTED
    assert attrs["private_key"] == REDACTED
    _no_secret_anywhere(effect, "smtp-pass-mixed", "tok-leak", "cs-leak", "pk-leak")


# ---------------------------------------------------------------------------
# keycloak.role_mapping.assign -- granted roles visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_mapping_assign_preview_shows_granted_roles() -> None:
    """A role-grant surfaces the granted realm role names + the user."""
    ctx = _ctx(
        "keycloak.role_mapping.assign",
        {"username": "svc-meho", "roles": ["realm-admin", "manage-users", "  "]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["op_class"] == "write"
    preview = effect["preview"]
    assert preview["resource"] == "role_mapping"
    assert preview["username"] == "svc-meho"
    # Blank role names are dropped; real ones are surfaced verbatim.
    assert preview["granted_roles"] == ["realm-admin", "manage-users"]


@pytest.mark.asyncio
async def test_role_mapping_assign_preview_by_uuid() -> None:
    """A role-grant keyed on the user UUID surfaces the id."""
    user_uuid = "11111111-2222-3333-4444-555555555555"
    ctx = _ctx(
        "keycloak.role_mapping.assign",
        {"id": user_uuid, "roles": ["operator"]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["id"] == user_uuid
    assert preview["username"] is None
    assert preview["granted_roles"] == ["operator"]


# ---------------------------------------------------------------------------
# Fail-soft + robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_create_preview_tolerates_missing_representation() -> None:
    """A malformed (absent) representation previews an empty resource, no raise."""
    ctx = _ctx("keycloak.user.create", {"username": "fallback-user"})
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["username"] == "fallback-user"
    assert preview["representation"] == {}

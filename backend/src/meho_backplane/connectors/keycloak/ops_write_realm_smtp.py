# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The mail server (SMTP) password of ``keycloak.realm.update``.

A realm needs a mail server so Keycloak can send "forgot password" and
"set up your account" mails. The settings sit in the realm's
``smtpServer`` map (``host``, ``port``, ``from``, ``user``, ``password``,
...). The password follows the rule of the user ops: it is never a
param, only a reference to where it is stored.

* ``smtp_password_secret_ref`` (with ``smtp_password_secret_mount`` and
  ``smtp_password_secret_key``, the same rules as ``password_secret_ref``)
  names the stored password. The handler reads it in-process under the
  operator's identity and puts it into ``smtpServer.password`` only in the
  body MEHO sends to Keycloak.
* A call with a ``password`` key inside ``representation.smtpServer`` is
  refused before it waits for approval (:func:`register_park_check`), so
  no approval request is stored for it. The handler refuses it too.

The refusal text names the reference field and never a value.

The op is a credential write (``op_classes.py``), so MEHO shows no
generic params echo for it. :func:`_realm_update_preview` is its own
approval card instead: the realm, the requested fields with secret values
hidden, and where the SMTP password comes from. The card hides a value by
the Keycloak list of secret fields and by the same name rule as the
generic params echo, so ``clientSecret``, ``authTokenClientSecret`` or
``someApiKey`` at any depth show as hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import register_park_check
from meho_backplane.operations._preview import (
    _scrub_secret_param_keys,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "REALM_UPDATE_OP_ID",
    "SMTP_PASSWORD_PREFIX",
    "SMTP_PASSWORD_REF_PARAM",
    "SMTP_PASSWORD_SECRET_PROPS",
    "has_inline_smtp_password",
    "inline_smtp_password_reason",
    "with_smtp_password",
]

REALM_UPDATE_OP_ID = "keycloak.realm.update"

#: Prefix of the three reference params (``<prefix>_secret_ref`` / ``_mount`` / ``_key``).
SMTP_PASSWORD_PREFIX = "smtp_password"
SMTP_PASSWORD_REF_PARAM = f"{SMTP_PASSWORD_PREFIX}_secret_ref"

SMTP_PASSWORD_SECRET_PROPS: dict[str, Any] = {
    SMTP_PASSWORD_REF_PARAM: {
        "type": "string",
        "description": (
            "Reference to the realm's mail server (SMTP) password: a Vault KV-v2 "
            "path, or a '<kind>:' reference. MEHO reads it under your identity and "
            "sets it as smtpServer.password. Never put the password itself in "
            "representation.smtpServer."
        ),
    },
    f"{SMTP_PASSWORD_PREFIX}_secret_mount": {
        "type": "string",
        "description": "Vault KV-v2 mount point of the SMTP password (default 'secret').",
    },
    f"{SMTP_PASSWORD_PREFIX}_secret_key": {
        "type": "string",
        "description": "Field of the stored secret that holds the password (default 'password').",
    },
}


def has_inline_smtp_password(params: dict[str, Any]) -> bool:
    """Whether ``representation.smtpServer`` holds a ``password`` key (any case)."""
    representation = params.get("representation")
    if not isinstance(representation, dict):
        return False
    smtp = representation.get("smtpServer")
    if not isinstance(smtp, dict):
        return False
    return any(isinstance(key, str) and key.lower() == "password" for key in smtp)


def inline_smtp_password_reason() -> str:
    """The refusal text for an inline SMTP password. It names no value."""
    return (
        "representation.smtpServer.password is a secret; give it as "
        f"{SMTP_PASSWORD_REF_PARAM} (a reference to the stored password), "
        "not as a plain value"
    )


def with_smtp_password(smtp: Any, password: str) -> dict[str, Any]:
    """Return the ``smtpServer`` map with *password* set.

    *smtp* is the caller's ``representation.smtpServer``: absent (``None``)
    or a map. Any other shape raises :exc:`ValueError`.
    """
    if smtp is None:
        return {"password": password}
    if not isinstance(smtp, dict):
        raise ValueError(
            f"keycloak.realm.update: representation.smtpServer must be an object when "
            f"{SMTP_PASSWORD_REF_PARAM} is set"
        )
    return {**smtp, "password": password}


async def _refuse_inline_smtp_password(ctx: PreviewContext) -> str | None:
    """Park check: refuse an inline SMTP password before the wait for approval."""
    if has_inline_smtp_password(ctx.params):
        return inline_smtp_password_reason()
    return None


def _realm_label(ctx: PreviewContext) -> str | None:
    """The realm the update targets: the ``realm`` param, else the managed realm."""
    explicit = ctx.params.get("realm")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if ctx.target is None:
        return None
    try:
        return resolve_realm_config(ctx.target).managed_realm
    except Exception:
        # The label is a convenience; a target without a realm config omits it.
        return None


async def _realm_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card for ``keycloak.realm.update``: realm, fields, password source."""
    representation = ctx.params.get("representation")
    # Two passes: the Keycloak list of secret fields, then the key-name
    # scrub of the generic params echo (``is_secret_param_name``), which
    # also catches camelCase names such as ``authTokenClientSecret``. Both
    # write the same ``***REDACTED***`` marker.
    hidden = _scrub_secret_param_keys(
        redact_secret_fields(representation if isinstance(representation, dict) else {})
    )
    preview: dict[str, Any] = {
        "resource": "realm",
        "realm": _realm_label(ctx),
        "representation": hidden,
    }
    secret_ref = ctx.params.get(SMTP_PASSWORD_REF_PARAM)
    if isinstance(secret_ref, str) and secret_ref.strip():
        # A reference, not a secret: the approver sees where the password comes from.
        preview["smtp_password_source"] = "secret_ref"
        preview[SMTP_PASSWORD_REF_PARAM] = secret_ref.strip()
    return preview


register_park_check(REALM_UPDATE_OP_ID, _refuse_inline_smtp_password)
register_preview_builder(REALM_UPDATE_OP_ID, _realm_update_preview)

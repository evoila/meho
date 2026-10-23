# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad password-age read — ``msad.user.password-expiry`` (safe).

Reads when one account's password was last set and when it expires. The
script asks ``Get-ADUser`` for the raw attributes (``pwdLastSet``,
``PasswordNeverExpires``, ``PasswordExpired`` and the DC-computed
``msDS-UserPasswordExpiryTimeComputed``) and returns them as numbers; the
date math happens here in Python so the two special values of the computed
attribute are handled before any conversion:

* ``0x7FFFFFFFFFFFFFFF`` — the password never expires (the flag is set, a
  smart card is required, or the password rule has no maximum age). The
  value lies after the year 9999, so a naive conversion would overflow.
* ``0`` — ``pwdLastSet`` is 0: the password must be changed at the next logon.

Both timestamps are FILETIME values: 100-nanosecond intervals since
1601-01-01 UTC. Spec:
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-adts/f9e9b7e2-c7ac-4db6-ba38-71d9696981e9
and https://learn.microsoft.com/en-us/windows/win32/adschema/a-pwdlastset.

The op returns no password and no hash, and takes no secret parameter. The
identity is interpolated only inside a single-quoted literal via
``ps_single_quote`` (the module-wide injection rule).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.msad.ops import SSH_TRANSPORT_NOTE, MsadOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "NEVER_EXPIRES_FILETIME",
    "PASSWORD_OPS",
    "msad_user_password_expiry",
    "password_expiry_fields",
]

#: ``msDS-UserPasswordExpiryTimeComputed`` when the password never expires.
NEVER_EXPIRES_FILETIME: int = 0x7FFFFFFFFFFFFFFF

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

_PASSWORD_PROPS: str = (
    "'pwdLastSet','PasswordNeverExpires','PasswordExpired','msDS-UserPasswordExpiryTimeComputed'"
)


async def msad_user_password_expiry(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.password-expiry`` — one account's password age and expiry."""
    identity = params["identity"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$u = Get-ADUser -Identity {ps_single_quote(identity)} -Properties {_PASSWORD_PROPS}; "
        "ConvertTo-Json -Compress -InputObject @{ "
        "SamAccountName = $u.SamAccountName; "
        "Enabled = $u.Enabled; "
        "PwdLastSet = $u.pwdLastSet; "
        "PasswordNeverExpires = $u.PasswordNeverExpires; "
        "PasswordExpired = $u.PasswordExpired; "
        "ExpiryComputed = $u.'msDS-UserPasswordExpiryTimeComputed' }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {"identity": identity, **password_expiry_fields(data, now=datetime.now(UTC))}


def password_expiry_fields(data: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Turn the raw script payload into the documented response fields.

    *now* is passed in so the day counts are testable. ``password_age_days``
    and ``days_to_expiry`` are whole days rounded down; ``days_to_expiry`` is
    below 0 once the password has expired, ``0`` when it must be changed at
    the next logon, and ``None`` when it never expires (or the DC returned no
    value).
    """
    last_set_at = _filetime_to_datetime(_filetime(data.get("PwdLastSet")))
    expiry_raw = _filetime(data.get("ExpiryComputed"))
    expires_at: datetime | None = None
    days_to_expiry: int | None = None
    if expiry_raw == 0:
        days_to_expiry = 0
    elif expiry_raw is not None and expiry_raw != NEVER_EXPIRES_FILETIME:
        expires_at = _filetime_to_datetime(expiry_raw)
        days_to_expiry = (expires_at - now).days if expires_at else None
    return {
        "sam_account_name": data.get("SamAccountName"),
        "enabled": bool(data.get("Enabled")),
        "password_last_set": _iso(last_set_at),
        "password_age_days": (now - last_set_at).days if last_set_at else None,
        "password_never_expires": bool(data.get("PasswordNeverExpires")),
        "password_expired": bool(data.get("PasswordExpired")),
        "password_expires_at": _iso(expires_at),
        "days_to_expiry": days_to_expiry,
    }


def _filetime(value: Any) -> int | None:
    """Return *value* as a non-negative int, or ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _filetime_to_datetime(value: int | None) -> datetime | None:
    """Convert a FILETIME to an aware UTC datetime; ``0`` / ``None`` give ``None``."""
    if not value:
        return None
    return _FILETIME_EPOCH + timedelta(microseconds=value // 10)


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


_IDENTITY_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The user identity — sAMAccountName, distinguished name, GUID, or SID "
        "(the ``-Identity`` operand, e.g. ``svc-sql``)."
    ),
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string", "description": "The identity you asked for."},
        "sam_account_name": {"type": ["string", "null"]},
        "enabled": {"type": "boolean"},
        "password_last_set": {
            "type": ["string", "null"],
            "description": (
                "ISO 8601 UTC. Null when the password must be changed at the next logon."
            ),
        },
        "password_age_days": {
            "type": ["integer", "null"],
            "description": "Whole days since the password was last set.",
        },
        "password_never_expires": {"type": "boolean"},
        "password_expired": {"type": "boolean"},
        "password_expires_at": {
            "type": ["string", "null"],
            "description": "ISO 8601 UTC. Null when the password never expires.",
        },
        "days_to_expiry": {
            "type": ["integer", "null"],
            "description": (
                "Whole days left, rounded down; below 0 once expired; 0 when the "
                "password must be changed at the next logon; null when it never expires."
            ),
        },
    },
    "required": [
        "identity",
        "sam_account_name",
        "enabled",
        "password_last_set",
        "password_age_days",
        "password_never_expires",
        "password_expired",
        "password_expires_at",
        "days_to_expiry",
    ],
    "additionalProperties": False,
}

PASSWORD_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.user.password-expiry",
        handler_attr="msad_user_password_expiry",
        summary="Read when one AD account's password was last set and when it expires.",
        description=(
            "Runs ``Get-ADUser -Identity <id> -Properties pwdLastSet, "
            "PasswordNeverExpires, PasswordExpired, "
            "msDS-UserPasswordExpiryTimeComputed`` and returns one flat object: "
            "when the password was last set, its age in days, the never-expires "
            "and expired flags, when it expires, and the whole days left "
            "(``days_to_expiry``). The domain controller computes the expiry "
            "with the password rule that applies to the account. Covers users "
            "and service accounts. Read-only; returns no password or hash."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_RESPONSE_SCHEMA,
        group_key="users",
        tags=("read-only", "user", "password-expiry"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to find out how old an AD account's password is and when it "
                "expires — for example before a service account's SSH login stops "
                "working. Point a threshold sensor at ``days_to_expiry`` to warn "
                "ahead of time. Read-only; no password material. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. sAMAccountName / DN / GUID / SID."},
            "output_shape": (
                "{'identity', 'sam_account_name', 'enabled', 'password_last_set', "
                "'password_age_days', 'password_never_expires', 'password_expired', "
                "'password_expires_at', 'days_to_expiry'} — flat; days_to_expiry is "
                "null when the password never expires."
            ),
        },
    ),
)

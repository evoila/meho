# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad password ops — the password-age read and the password set.

* ``msad.user.password-expiry`` (safe) — when one account's password was last
  set and when it expires.
* ``msad.user.set-password`` (caution, waits for approval) — set a new
  password on one account from the secret store, and enable the account.

The password-expiry read
------------------------

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

The password set
----------------

The op never takes a password as an input. It takes ``password_ref``, a
``"<vault-path>#<field>"`` reference into the secret store (the same form as
``secret_env`` of ``linux.script.run``). The handler reads the value only
when the approved call runs, under the identity of the operator, through the
connector's one Vault seam (``SshConnector._resolve_secret``). So the
approval row, the audit row and the broadcast carry the reference, never the
value.

The value then travels to PowerShell on standard input
(``pwsh_run(stdin=...)``), not in the script. The script text rides on the
remote command line (``-EncodedCommand``); standard input does not. The value
is base64-encoded first, so the console code page of the server cannot change
non-ASCII characters, and a line break in the value cannot split it. The
script reads it with ``$input``, turns it into a ``SecureString`` and calls
``Set-ADAccountPassword -Reset``. An admin reset does not check password
history or minimum age, so the same call can run twice with the same result.

References:
https://learn.microsoft.com/en-us/powershell/module/activedirectory/set-adaccountpassword
and ``$input`` with ``powershell.exe -Command`` in
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_automatic_variables
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors._shared.vault_creds import (
    CredentialsReadError,
    check_param_secret_ref,
    strip_credential_value,
)
from meho_backplane.connectors.msad.ops import SSH_TRANSPORT_NOTE, MsadOp
from meho_backplane.operations import register_plain_fields
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "NEVER_EXPIRES_FILETIME",
    "PASSWORD_OPS",
    "SET_PASSWORD_OP_ID",
    "msad_user_password_expiry",
    "msad_user_set_password",
    "parse_password_ref",
    "password_expiry_fields",
    "set_password_script",
]

#: The op id of the password set.
SET_PASSWORD_OP_ID: str = "msad.user.set-password"
PASSWORD_EXPIRY_OP_ID: str = "msad.user.password-expiry"

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
    """Convert a FILETIME to an aware UTC datetime.

    ``0`` / ``None`` give ``None``. So does any value past the year 9999
    (``datetime`` cannot hold it); only the documented sentinel should get
    that far, but a surprise value must not turn a read into an error.
    """
    if not value:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except OverflowError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


@dataclass(frozen=True)
class _VaultRefTarget:
    """The target shape ``SshConnector._resolve_secret`` needs for one read.

    It carries the operator's vault path as ``secret_ref`` and borrows the
    real target's ``name`` / ``host`` for the log line, like
    ``linux.script.run`` does for ``secret_env``.
    """

    name: str
    host: str
    secret_ref: str


def parse_password_ref(raw: Any) -> tuple[str, str]:
    """Check ``password_ref`` and return ``(vault_path, field)``.

    The form is ``"<vault-path>#<field>"``: exactly one ``#``, a non-empty
    path and field, no control characters. Raises :class:`ValueError` before
    any SSH or Vault call. The message names the reference, never a value.
    """
    if not isinstance(raw, str) or raw.count("#") != 1:
        raise ValueError("password_ref must be one '<vault-path>#<field>' reference")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError("password_ref contains a control character")
    path, _, field = raw.partition("#")
    path, field = path.strip(), field.strip()
    if not path or not field:
        raise ValueError("password_ref must give a vault path and a field: '<vault-path>#<field>'")
    return path, field


async def _read_password(
    connector: MsadConnector,
    target: Any,
    operator: Operator | None,
    ref: tuple[str, str],
) -> str:
    """Read the password at *ref* from the secret store, under *operator*.

    Errors name the reference, never a value, and stop the op before anything
    runs on the server. The field must hold a non-empty string: the type is
    checked on the raw value, because ``strip_credential_value`` turns any
    value into text (``None`` would become ``"None"``).
    """
    path, field = ref
    host = getattr(target, "host", None)
    stub = _VaultRefTarget(
        name=str(getattr(target, "name", None) or "unknown"),
        host=host if isinstance(host, str) and host else "unknown",
        secret_ref=path,
    )
    # The path comes from the params: check it before the read. A refused
    # path raises SecretRefScopeError unchanged, so the dispatcher answers
    # vault_credentials_read_error with only the path's first segment.
    check_param_secret_ref(operator, path, param="password_ref")
    try:
        data = await connector._resolve_secret(stub, operator)
    except (VaultClientError, CredentialsReadError) as exc:
        raise ValueError(f"could not read password_ref {path}#{field}: {exc}") from exc
    if not isinstance(data, dict) or field not in data:
        raise ValueError(f"password_ref: the secret at {path!r} has no field {field!r}")
    raw = data[field]
    if not isinstance(raw, str):
        raise ValueError(f"password_ref: the field {field!r} at {path!r} is not a string")
    value = strip_credential_value(raw)
    if not value:
        raise ValueError(f"password_ref: the field {field!r} at {path!r} is empty")
    return value


def set_password_script(identity: str, *, enable: bool) -> str:
    """Return the fixed ``msad.user.set-password`` script for *identity*.

    The password is not in the script: the script reads it, base64-encoded,
    from standard input. *identity* is a single-quoted literal.
    """
    quoted = ps_single_quote(identity)
    enable_step = f"Enable-ADAccount -Identity {quoted}; " if enable else ""
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$b = @($input)[0]; "
        "if (-not $b) { throw 'No password arrived on standard input.' }; "
        "$p = ConvertTo-SecureString -AsPlainText -Force -String "
        "([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b))); "
        "Remove-Variable -Name b; "
        f"Set-ADAccountPassword -Identity {quoted} -Reset -NewPassword $p; "
        + enable_step
        + f"$u = Get-ADUser -Identity {quoted} -Properties 'pwdLastSet'; "
        "ConvertTo-Json -Compress -InputObject @{ "
        "SamAccountName = $u.SamAccountName; "
        "Enabled = $u.Enabled; "
        "PwdLastSet = $u.pwdLastSet }"
    )


async def msad_user_set_password(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.set-password`` (caution, waits for approval).

    Runs only on the approved path. Reads the password from the secret store
    now, sends it on standard input, and returns the account state. The
    result holds no password.
    """
    identity = params.get("identity")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("identity must be a non-empty string")
    enable = params.get("enable", True)
    if not isinstance(enable, bool):
        raise ValueError("enable must be true or false")
    ref = parse_password_ref(params.get("password_ref"))
    password = await _read_password(connector, target, operator, ref)
    stdin = base64.b64encode(password.encode("utf-8")).decode("ascii") + "\n"
    script = set_password_script(identity, enable=enable)
    payload = await pwsh_run(connector, target, script, operator=operator, stdin=stdin)
    data = payload if isinstance(payload, dict) else {}
    last_set = _filetime_to_datetime(_filetime(data.get("PwdLastSet")))
    return {
        "identity": identity,
        "sam_account_name": data.get("SamAccountName"),
        "enabled": bool(data.get("Enabled")),
        "password_last_set": _iso(last_set),
        "op_class": "write",
    }


async def _set_password_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview for ``msad.user.set-password``: the account and the reference.

    The op is a credential write, so the default preview is suppressed. The
    reference is a pointer into the secret store, not the value, and no value
    exists before the approved call runs, so the approver can see which
    secret will be used. Declines (``None``) on malformed input.
    """
    identity = ctx.params.get("identity")
    try:
        path, field = parse_password_ref(ctx.params.get("password_ref"))
    except ValueError:
        return None
    if not isinstance(identity, str) or not identity.strip():
        return None
    return {
        "target": getattr(ctx.target, "name", None),
        "identity": identity,
        "password_ref": f"{path}#{field}",
        "enable": ctx.params.get("enable", True) is not False,
    }


register_preview_builder(SET_PASSWORD_OP_ID, _set_password_preview)


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
        op_id=PASSWORD_EXPIRY_OP_ID,
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
    MsadOp(
        op_id=SET_PASSWORD_OP_ID,
        handler_attr="msad_user_set_password",
        summary="Set a new password on one AD account from the secret store, then enable it.",
        description=(
            "Reads the password from the secret store when the approved call runs, "
            "sends it to Set-ADAccountPassword -Reset on standard input, then runs "
            "Enable-ADAccount. Never takes or returns a password. Safe to run twice. "
            "A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "identity": _IDENTITY_PROP,
                "password_ref": {
                    "type": "string",
                    "minLength": 3,
                    "pattern": "^[^#]+#[^#]+$",
                    "description": (
                        "Where the new password is in the secret store: "
                        "'<vault-path>#<field>'. Never the password itself."
                    ),
                },
                "enable": {
                    "type": "boolean",
                    "description": "Enable the account after the password is set. Default true.",
                },
            },
            "required": ["identity", "password_ref"],
            "additionalProperties": False,
            "examples": [
                {"identity": "svc-backup", "password_ref": "lab/ad/svc-backup#password"},
                {
                    "identity": "jdoe",
                    "password_ref": "lab/ad/jdoe#password",
                    "enable": False,
                },
            ],
        },
        response_schema={
            "type": "object",
            "properties": {
                "identity": {"type": "string", "description": "The identity you asked for."},
                "sam_account_name": {"type": ["string", "null"]},
                "enabled": {"type": "boolean"},
                "password_last_set": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 UTC time the password was set.",
                },
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": [
                "identity",
                "sam_account_name",
                "enabled",
                "password_last_set",
                "op_class",
            ],
            "additionalProperties": False,
        },
        group_key="users",
        tags=("write", "user", "password", "credential"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call after msad.user.create, which makes a disabled account without a "
                "password, to give the account its password and enable it. Also call to "
                "reset a password. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. Put the password into the secret store "
                "first and pass its place as password_ref. Never pass a password in any "
                "parameter. The password must meet the domain's password rules "
                "(see msad.domain.settings)."
            ),
            "parameter_hints": {
                "identity": "Required. sAMAccountName / DN / GUID / SID.",
                "password_ref": (
                    "Required. '<vault-path>#<field>', for example 'lab/ad/jdoe#password'."
                ),
                "enable": "Optional. Default true. Set false to keep the account disabled.",
            },
            "output_shape": (
                "{'identity', 'sam_account_name', 'enabled', 'password_last_set', "
                "'op_class': 'write'} — no password."
            ),
            "next_step": (
                "Check that enabled is true and password_last_set is now. "
                "msad.user.password-expiry shows when the password expires."
            ),
        },
    ),
)


# A date and a day count about the password, never the password.
register_plain_fields(SET_PASSWORD_OP_ID, ("password_last_set",))
register_plain_fields(PASSWORD_EXPIRY_OP_ID, ("password_last_set", "password_age_days"))

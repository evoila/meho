# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Local user password-expiry read verb for :class:`LinuxSshConnector` (#6239).

``linux.user.password_status`` (group ``system``) -- a ``safe`` read of the
password ageing data of one local user, from ``LC_ALL=C chage -l -- <user>``.
``chage -l`` prints dates and day counts only, never the password hash, and
the verb never reads ``/etc/shadow`` itself. It answers how many days are
left until the password expires, so a Sensor can warn before an SSH password
login is forced to change the password.

The user name is checked against a strict POSIX name pattern before any
command is built, so nothing else can reach the command line. A login that
reads its own account runs ``chage`` directly. Any other login reads through
sudo, through the shared sudo path
(:mod:`~meho_backplane.connectors.linux._sudo`), like ``linux.firewall.show``.
A read that fails raises :class:`LinuxPasswordStatusError`; it never answers
with an empty or made-up status. The error text holds no output of the host,
so no secret can leak through it.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.linux._sudo import run_remote_bash_with_sudo
from meho_backplane.connectors.linux.ops import SSH_TRANSPORT_NOTE, LinuxOp

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "USER_NAME_PATTERN",
    "USER_OPS",
    "LinuxPasswordStatusError",
    "build_password_status_command",
    "linux_user_password_status",
    "parse_password_status_output",
    "validate_user_name",
]

#: POSIX portable user name: starts with a letter or ``_``, then letters,
#: digits, ``_`` and ``-``, at most 32 characters. No leading ``-`` (it could
#: be read as an option), no space, no shell character.
USER_NAME_PATTERN: str = r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$"
_USER_NAME_RE: re.Pattern[str] = re.compile(USER_NAME_PATTERN)

_MUST_CHANGE = "password must be changed"

_MONTHS: dict[str, int] = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}

_DATE_RE: re.Pattern[str] = re.compile(r"^([A-Za-z]{3}) (\d{1,2}), (\d{4})$")

#: ``chage -l`` label (lower case, up to the colon) -> field name.
_LABELS: dict[str, str] = {
    "last password change": "last_change",
    "password expires": "expires",
    "password inactive": "inactive",
    "account expires": "account_expires",
    "minimum number of days between password change": "min_days",
    "maximum number of days between password change": "max_days",
    "number of days of warning before password expires": "warn_days",
}

_DATE_FIELDS = ("last_change", "expires", "inactive", "account_expires")


class LinuxPasswordStatusError(RuntimeError):
    """The password status could not be read.

    The dispatcher turns it into a connector error, so a failed read never
    looks like a password that does not expire. The text names the user and
    the kind of failure only; it never repeats output of the host.
    """


def validate_user_name(user: Any) -> str:
    """Return *user* if it matches the POSIX user-name pattern, else raise.

    Fail-closed re-validation ahead of command construction: the schema
    ``pattern`` is advisory, this check is authoritative.
    """
    if not isinstance(user, str) or not _USER_NAME_RE.fullmatch(user):
        raise ValueError(f"user {user!r} is not a valid POSIX user name")
    return user


def build_password_status_command(user: str) -> str:
    """Build the fixed ``LC_ALL=C chage -l -- <user>`` command.

    Re-validates *user* itself, so an unvalidated value can never reach the
    command line. ``--`` ends the options, and the name is ``shlex.quote``d.
    """
    return f"LC_ALL=C chage -l -- {shlex.quote(validate_user_name(user))}"


def _parse_date(raw: str) -> str | None:
    """Return the ISO date of a ``chage`` date (``Nov 01, 2026``), else ``None``."""
    match = _DATE_RE.fullmatch(raw)
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def _read_labelled_lines(user: str, stdout: str) -> dict[str, str]:
    """Return the seven ``chage -l`` values by field name, or raise when one is missing."""
    raw: dict[str, str] = {}
    for line in stdout.splitlines():
        label, sep, value = line.partition(":")
        field = _LABELS.get(label.strip().lower())
        if sep and field is not None:
            raw[field] = value.strip()
    missing = [field for field in _LABELS.values() if field not in raw]
    if missing:
        raise LinuxPasswordStatusError(
            f"could not read the password status of {user!r}: chage output lacks {missing}"
        )
    return raw


def _convert_field(user: str, field: str, value: str) -> str | int:
    """Convert one ``chage`` value: a date to ISO, a day count to an int."""
    lowered = value.lower()
    if field in _DATE_FIELDS and lowered in ("never", _MUST_CHANGE):
        return lowered
    parsed: str | int | None
    if field in _DATE_FIELDS:
        parsed = _parse_date(value)
    else:
        try:
            parsed = int(value)
        except ValueError:
            parsed = None
    if parsed is None:
        raise LinuxPasswordStatusError(
            f"could not read the password status of {user!r}: unknown value in {field}"
        )
    return parsed


def parse_password_status_output(
    user: str, stdout: str, *, today: date | None = None
) -> dict[str, Any]:
    """Parse ``chage -l`` output into the password status of *user*.

    Date fields are ISO dates, ``never``, or ``password must be changed``
    (``last_change`` and ``expires`` of an account whose password was reset).
    ``must_change`` is true in that last case. ``days_until_expiry`` is the
    number of days from *today* (UTC) to ``expires``: ``null`` for ``never``,
    ``0`` when the password must be changed now. ``expired`` is true when the
    date has passed or the password must be changed. Output that lacks any
    of the seven lines, or holds a value of an unknown shape, raises
    :class:`LinuxPasswordStatusError`: a partial read is never an answer.
    """
    raw = _read_labelled_lines(user, stdout)
    result: dict[str, Any] = {"user": user}
    for field, value in raw.items():
        result[field] = _convert_field(user, field, value)

    must_change = _MUST_CHANGE in (result["last_change"], result["expires"])
    days: int | None
    if must_change:
        days = 0
    elif result["expires"] == "never":
        days = None
    else:
        reference = today or datetime.now(UTC).date()
        days = (date.fromisoformat(result["expires"]) - reference).days
    result["must_change"] = must_change
    result["days_until_expiry"] = days
    result["expired"] = must_change or (days is not None and days < 0)
    return result


async def linux_user_password_status(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.user.password_status`` -- read one user's password ageing."""
    user = validate_user_name(params["user"])
    cmd = build_password_status_command(user)
    secret = await connector._resolve_secret(target, operator)
    login = strip_credential_value(secret.get("username", "root"))
    if login == user:
        proc = await connector._run_command(target, cmd, operator=operator)
    else:
        # No password in the secret: send an empty one. sudo does not read it
        # when the login may use sudo without a password; else it refuses.
        sudo_password = secret.get("sudo_password") or secret.get("password") or ""
        proc = await run_remote_bash_with_sudo(
            connector,
            target,
            cmd,
            operator=operator,
            sudo_password=strip_credential_value(sudo_password),
        )
    exit_status = getattr(proc, "exit_status", None)
    stdout = _text(proc, "stdout")
    if exit_status != 0:
        raise LinuxPasswordStatusError(_failure_message(user, _text(proc, "stderr"), exit_status))
    return parse_password_status_output(user, stdout)


def _text(proc: Any, field: str) -> str:
    raw = getattr(proc, field, "") or ""
    return raw if isinstance(raw, str) else ""


def _failure_message(user: str, stderr: str, exit_status: Any) -> str:
    """Name the kind of failure without repeating any host output."""
    lowered = stderr.lower()
    if "does not exist" in lowered:
        return f"unknown_user: the host has no local user {user!r}"
    if any(word in lowered for word in ("permission denied", "password is required", "sudo:")):
        return (
            f"permission_denied: could not read the password status of {user!r}. "
            "The login needs sudo rights for chage."
        )
    return (
        f"read_failed: could not read the password status of {user!r} "
        f"(chage ended with exit status {exit_status})."
    )


_USER_PASSWORD_STATUS_OP = LinuxOp(
    op_id="linux.user.password_status",
    handler_attr="user_password_status",
    summary="Read how many days are left until a local user's password expires.",
    description=(
        "Runs chage -l for one local user. Returns the password dates, the day "
        "counts, days_until_expiry (null when it never expires) and expired. "
        "Never returns a password or hash. A login that is not the user reads "
        "through sudo. A missing user or a failed read is an error."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "user": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "pattern": USER_NAME_PATTERN,
                "description": (
                    "Local user name, for example svc-deploy. Letters, digits, '_' and "
                    "'-' only; must start with a letter or '_'."
                ),
            },
        },
        "required": ["user"],
        "additionalProperties": False,
        "examples": [{"user": "svc-deploy"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "user": {"type": "string"},
            "last_change": {
                "type": "string",
                "description": "ISO date, or 'password must be changed'.",
            },
            "expires": {
                "type": "string",
                "description": "ISO date, 'never', or 'password must be changed'.",
            },
            "inactive": {"type": "string", "description": "ISO date or 'never'."},
            "account_expires": {"type": "string", "description": "ISO date or 'never'."},
            "min_days": {"type": "integer"},
            "max_days": {"type": "integer"},
            "warn_days": {"type": "integer"},
            "must_change": {"type": "boolean"},
            "days_until_expiry": {
                "type": ["integer", "null"],
                "description": "Days until the password expires; null when it never does.",
            },
            "expired": {"type": "boolean"},
        },
        "required": [
            "user",
            "last_change",
            "expires",
            "inactive",
            "account_expires",
            "min_days",
            "max_days",
            "warn_days",
            "must_change",
            "days_until_expiry",
            "expired",
        ],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "user", "password", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see how many days are left until a local Linux user's password "
            "expires, for example a service login that MEHO uses over SSH. When that "
            "password expires, an SSH password login must change it first, and every "
            "call to the host fails. Safe, so a Sensor can run it on a schedule: "
            "$.days_until_expiry with a threshold of lt 14 (degraded) and lt 3 "
            "(critical). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {"user": "One local user name. No domain, no space, no leading '-'."},
        "output_shape": (
            "{user, last_change, expires, inactive, account_expires, min_days, max_days, "
            "warn_days, must_change, days_until_expiry, expired}. days_until_expiry is null "
            "when expires is 'never'. It is 0 and expired is true when the password must "
            "be changed at the next login."
        ),
        "next_step": (
            "If the password expires soon, change it through your normal process for that "
            "account. This operation only reads."
        ),
    },
)


#: The user-account read tier composed onto ``LINUX_OPS``.
USER_OPS: tuple[LinuxOp, ...] = (_USER_PASSWORD_STATUS_OP,)

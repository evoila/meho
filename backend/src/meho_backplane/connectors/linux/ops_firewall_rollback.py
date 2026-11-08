# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A timed way back for ``linux.firewall.load``, and ``linux.firewall.confirm``.

A firewall rule with good syntax can still cut the SSH path to the host.
Then only a console login can fix it. So ``linux.firewall.load`` can start a
timer on the host before it loads new rules (``rollback_after_seconds``):

1. take the host lock and refuse when another timed change still waits;
2. save the live rules to a root-only folder, and check the saved file;
3. check the new rules;
4. start a ``systemd-run`` timer that loads the saved rules again;
5. load the new rules.

If the save or the timer fails, the script stops before any change. A host
without systemd or ``flock`` is refused. If the new rules do not load, the
script loads the saved rules at once; only when that works does it stop the
timer.

One lock file (``.lock`` in the state folder, held with ``flock``) keeps the
load, the confirm and the timer's restore apart, so they never run at the
same time.

``linux.firewall.confirm`` stops the timer, so the new rules stay. It opens a
new SSH connection for this: a pooled connection stays open under the new
rules and proves nothing. With ``install_path`` it also writes the new rules
to the file the host loads at boot. It refuses when the timer ends in less
than :data:`CONFIRM_MIN_SECONDS_LEFT` seconds. Before it stops the timer it
writes a ``keep`` mark; the restore checks for that mark under the lock, so a
restore that the timer started just before the stop does nothing.

The host keeps the state of one change in
``/var/lib/meho/firewall-rollback/<rollback_id>/``: ``saved.rules`` (the old
rules), ``new.rules``, ``backend``, ``due`` (the end of the timer, in Unix
seconds of the host clock), ``restored`` (written by the timer after it put
the old rules back) and ``keep`` (written by the confirm).
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.adapters.ssh import SshHostKeyUnpinnedError, _dial, run_capped
from meho_backplane.connectors.linux._sudo import (
    build_sudo_bash_remote_cmd,
    run_remote_bash_with_sudo,
)
from meho_backplane.connectors.linux.ops import SSH_TRANSPORT_NOTE, LinuxOp
from meho_backplane.connectors.linux.ops_write import (
    LinuxWriteError,
    LinuxWriteSafetyError,
    _b64,
    _resolve_sudo_password,
    _stderr,
    _stdout,
    confine_write_path,
)

if TYPE_CHECKING:
    import asyncssh

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "CONFIRM_MIN_SECONDS_LEFT",
    "FIREWALL_ROLLBACK_OPS",
    "LOCK_WAIT_SECONDS",
    "ROLLBACK_MAX_SECONDS",
    "ROLLBACK_MIN_SECONDS",
    "LinuxFirewallConfirmError",
    "bound_rollback_seconds",
    "build_firewall_confirm_script",
    "build_firewall_load_with_rollback_script",
    "build_restore_command",
    "linux_firewall_confirm",
    "load_firewall_with_rollback",
    "validate_rollback_id",
]

#: Allowed range of ``rollback_after_seconds``.
ROLLBACK_MIN_SECONDS: int = 60
ROLLBACK_MAX_SECONDS: int = 1800

#: ``linux.firewall.confirm`` refuses when the timer ends sooner than this.
CONFIRM_MIN_SECONDS_LEFT: int = 20

#: Root-only folder on the host that holds the state of each timed change.
ROLLBACK_STATE_ROOT: str = "/var/lib/meho/firewall-rollback"

#: How long the load and the confirm wait for the host lock.
LOCK_WAIT_SECONDS: int = 10

#: How long the timer's restore waits for the host lock before it runs anyway.
_RESTORE_LOCK_WAIT_SECONDS: int = 300

#: A change counts as pending until this many seconds after its timer ends,
#: so a timer that is about to start its restore still blocks a new load.
_PENDING_GRACE_SECONDS: int = 30

#: The built-in chains of each iptables table. A table that the new rules add
#: goes into the saved copy as an empty table, so the restore clears it.
_IPTABLES_BUILTIN_CHAINS: dict[str, tuple[str, ...]] = {
    "filter": ("INPUT", "FORWARD", "OUTPUT"),
    "nat": ("PREROUTING", "INPUT", "OUTPUT", "POSTROUTING"),
    "mangle": ("PREROUTING", "INPUT", "FORWARD", "OUTPUT", "POSTROUTING"),
    "raw": ("PREROUTING", "OUTPUT"),
    "security": ("INPUT", "FORWARD", "OUTPUT"),
}

_UNIT_PREFIX: str = "meho-fw-rollback-"
_ROLLBACK_ID_PATTERN: str = r"^[0-9a-f]{16}$"
_ROLLBACK_ID_RE: re.Pattern[str] = re.compile(_ROLLBACK_ID_PATTERN)

# Output markers of the load script.
_LOAD_OK = "===MEHO_FW_OK==="
_LOAD_VALIDATE_FAILED = "===MEHO_FW_VALIDATE_FAILED==="
_LOAD_NO_SYSTEMD = "===MEHO_FW_NO_SYSTEMD==="
_LOAD_DUE_PREFIX = "MEHO_FW_DUE="
_LOAD_ERR_PREFIX = "===MEHO_FW_ERR:"

# Output prefix of the confirm script; one state per line.
_CONFIRM_PREFIX = "MEHO_FWC="


class LinuxFirewallConfirmError(LinuxWriteError):
    """``linux.firewall.confirm`` did not stop the timer.

    The message says what happens next: in every case but an unknown id the
    old rules are back, or are coming back when the timer ends.
    """


def validate_rollback_id(raw: Any) -> str:
    """Return *raw* when it has the shape of a rollback id, else raise."""
    if not isinstance(raw, str) or not _ROLLBACK_ID_RE.match(raw):
        raise LinuxWriteSafetyError("rollback_id must be the 16 hex characters load returned")
    return raw


def bound_rollback_seconds(raw: Any) -> int:
    """Return *raw* when it is a whole number of seconds in the allowed range."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LinuxWriteSafetyError("rollback_after_seconds must be a whole number")
    if not ROLLBACK_MIN_SECONDS <= raw <= ROLLBACK_MAX_SECONDS:
        raise LinuxWriteSafetyError(
            f"rollback_after_seconds must be from {ROLLBACK_MIN_SECONDS} to {ROLLBACK_MAX_SECONDS}"
        )
    return raw


def _state_dir(rollback_id: str) -> str:
    return f"{ROLLBACK_STATE_ROOT}/{rollback_id}"


def _lock_file() -> str:
    return f"{ROLLBACK_STATE_ROOT}/.lock"


def _iptables_tables(ruleset: str) -> list[str]:
    """Return the tables that *ruleset* names (``*filter`` lines), in order."""
    tables: list[str] = []
    for line in ruleset.splitlines():
        line = line.strip()
        if line.startswith("*"):
            name = line[1:].strip()
            if name not in _IPTABLES_BUILTIN_CHAINS:
                raise LinuxWriteSafetyError(
                    f"iptables table {name!r} is not one MEHO can put back with the timer"
                )
            if name not in tables:
                tables.append(name)
    return tables


def _empty_iptables_table(table: str) -> str:
    chains = "".join(f":{chain} ACCEPT [0:0]\n" for chain in _IPTABLES_BUILTIN_CHAINS[table])
    return f"*{table}\n{chains}COMMIT\n"


def build_restore_command(backend: str, rollback_id: str) -> str:
    """Render the shell command the timer runs to load the saved rules again.

    It waits for the host lock, so it never runs while a load or a confirm
    does. It does nothing when the confirm wrote ``keep`` or removed the
    state folder.
    """
    _, _, load_cmd = _backend_commands(backend)
    state = _state_dir(rollback_id)
    return (
        f"exec 9>>{_lock_file()}; flock -w {_RESTORE_LOCK_WAIT_SECONDS} 9; "
        f"if [ -f {state}/saved.rules ] && [ ! -e {state}/keep ]; then "
        f"{load_cmd} {state}/saved.rules && touch {state}/restored; fi"
    )


def _backend_commands(backend: str) -> tuple[str, str, str]:
    """Return the (save, check, load) shell parts for *backend*.

    ``save`` writes the live rules to ``$saved``. ``check`` and ``load`` take
    a file name after them. The nft save starts with ``flush ruleset``,
    because the output of ``nft list ruleset`` alone would add to the rules
    on load instead of replacing them.
    """
    if backend == "iptables":
        return (
            'iptables-save > "$saved"',
            "iptables-restore --test <",
            "iptables-restore <",
        )
    return (
        '{ echo "flush ruleset" && nft list ruleset; } > "$saved"',
        "nft -c -f",
        "nft -f",
    )


def build_firewall_load_with_rollback_script(
    ruleset: str, backend: str, *, rollback_id: str, seconds: int
) -> str:
    """Render the root script: save, check, start the timer, then load.

    Every value is fixed by MEHO (*rollback_id* is hex, *seconds* an int,
    *backend* a member of the allow-list, iptables table names from
    :data:`_IPTABLES_BUILTIN_CHAINS`) or base64-carried (*ruleset*). Any
    failure before the load removes the state folder and stops the script.
    When the load fails, the script loads the saved rules; it stops the
    timer only when that works.
    """
    save_cmd, check_cmd, load_cmd = _backend_commands(backend)
    new_tables = _iptables_tables(ruleset) if backend == "iptables" else []
    state = _state_dir(rollback_id)
    unit = f"{_UNIT_PREFIX}{rollback_id}"
    restore = build_restore_command(backend, rollback_id)
    err = _LOAD_ERR_PREFIX
    lines = [
        "set -u",
        "umask 077",
        f"root={shlex.quote(ROLLBACK_STATE_ROOT)}",
        f"dir={shlex.quote(state)}",
        f"unit={shlex.quote(unit)}",
        'saved="$dir/saved.rules"',
        'new="$dir/new.rules"',
        f'fail() {{ rm -rf -- "$dir"; echo "{err}$1===" >&2; exit 4; }}',
        "if [ ! -d /run/systemd/system ] || ! command -v systemd-run >/dev/null 2>&1; then",
        f'  echo "{_LOAD_NO_SYSTEMD}" >&2; exit 5',
        "fi",
        f'command -v flock >/dev/null 2>&1 || {{ echo "{err}no_flock===" >&2; exit 4; }}',
        f'mkdir -p -m 0700 "$root" || {{ echo "{err}mkdir===" >&2; exit 4; }}',
        f'exec 9>>"$root/.lock" || {{ echo "{err}busy===" >&2; exit 4; }}',
        f'flock -w {LOCK_WAIT_SECONDS} 9 || {{ echo "{err}busy===" >&2; exit 4; }}',
        "now=$(date +%s)",
        'for d in "$root"/*/; do',
        '  [ -f "${d}due" ] && [ ! -f "${d}restored" ] || continue',
        f'  if [ "$now" -le $(( $(cat "${{d}}due") + {_PENDING_GRACE_SECONDS} )) ] || '
        f'systemctl is-active --quiet "{_UNIT_PREFIX}$(basename "$d").service"; then',
        f'    echo "{err}pending===" >&2; exit 4',
        "  fi",
        "done",
        f'mkdir -m 0700 "$dir" || {{ echo "{err}mkdir===" >&2; exit 4; }}',
        f"{save_cmd} || fail save",
    ]
    for table in new_tables:
        lines.append(
            f'grep -qx {shlex.quote("*" + table)} "$saved" || '
            f'printf %s {shlex.quote(_empty_iptables_table(table))} >> "$saved" || fail save'
        )
    lines += [
        f'{check_cmd} "$saved" || fail save_check',
        f'printf %s {shlex.quote(_b64(ruleset))} | base64 -d > "$new" || fail decode',
        f'if ! {check_cmd} "$new"; then rm -rf -- "$dir"; echo "{_LOAD_VALIDATE_FAILED}" >&2; exit 3; fi',  # noqa: E501
        f'printf %s {shlex.quote(backend)} > "$dir/backend" || fail state',
        f"due=$(( $(date +%s) + {seconds} ))",
        'printf %s "$due" > "$dir/due" || fail state',
        f'systemd-run --quiet --unit="$unit" --on-active={seconds} '
        f"--timer-property=AccuracySec=1s --collect -- /bin/sh -c {shlex.quote(restore)} "
        "|| fail timer",
        f'if ! {load_cmd} "$new"; then',
        f'  if {load_cmd} "$saved"; then',
        '    systemctl stop "$unit.timer" >/dev/null 2>&1',
        '    rm -rf -- "$dir"',
        f'    echo "{err}apply===" >&2; exit 4',
        "  fi",
        f'  echo "{err}apply_restore_failed===" >&2; exit 4',
        "fi",
        f'echo "{_LOAD_DUE_PREFIX}$due"',
        f'echo "{_LOAD_OK}"',
    ]
    return "\n".join(lines) + "\n"


def _failed_stage(stderr: str) -> str | None:
    start = stderr.find(_LOAD_ERR_PREFIX)
    if start < 0:
        return None
    rest = stderr[start + len(_LOAD_ERR_PREFIX) :]
    return rest.split("===", 1)[0] or None


_STAGE_TEXT: dict[str, str] = {
    "mkdir": "could not make the root-only folder for the saved rules; nothing was changed",
    "save": "could not save the live rules; nothing was changed",
    "save_check": (
        "the saved copy of the live rules failed the syntax check, so it could not "
        "bring them back; nothing was changed"
    ),
    "decode": "could not write the new rules to the host; nothing was changed",
    "state": "could not write the timer state; nothing was changed",
    "timer": "could not start the timer with systemd-run; nothing was changed",
    "no_flock": "the host has no flock tool, which the timer needs; nothing was changed",
    "busy": (
        "another firewall step on this host holds the lock; nothing was changed. Try again "
        "in a moment."
    ),
    "pending": (
        "another timed firewall change on this host still waits for linux.firewall.confirm "
        "or for its timer. Confirm it, or wait until its timer has put the old rules back, "
        "then load again; nothing was changed"
    ),
}


def parse_load_with_rollback_output(proc: Any, backend: str, rollback_id: str) -> dict[str, Any]:
    """Turn the load script's output into the answer, or raise.

    Raises :class:`LinuxWriteError` for a host without systemd and for a
    failure before the load (nothing changed then). A failed syntax check of
    the new rules answers ``validated=false``, as a load without a timer does.
    """
    stdout = _stdout(proc)
    stderr = _stderr(proc)
    if _LOAD_NO_SYSTEMD in stderr:
        raise LinuxWriteError(
            "the host has no systemd, so MEHO cannot start the timer that puts the old "
            "rules back; nothing was changed"
        )
    if _LOAD_VALIDATE_FAILED in stderr or _LOAD_VALIDATE_FAILED in stdout:
        return {
            "applied": False,
            "backend": backend,
            "validated": False,
            "error": "ruleset failed validation; the live firewall was not changed",
        }
    stage = _failed_stage(stderr)
    if stage is not None and stage in _STAGE_TEXT:
        raise LinuxWriteError(_STAGE_TEXT[stage])
    due: int | None = None
    for line in stdout.splitlines():
        if line.startswith(_LOAD_DUE_PREFIX):
            value = line[len(_LOAD_DUE_PREFIX) :].strip()
            due = int(value) if value.isdigit() else None
    if _LOAD_OK in stdout and getattr(proc, "exit_status", None) == 0 and due is not None:
        return {
            "applied": True,
            "backend": backend,
            "validated": True,
            "rollback_id": rollback_id,
            "rollback_due_at": datetime.fromtimestamp(due, UTC).isoformat(),
        }
    if stage == "apply":
        return {
            "applied": False,
            "backend": backend,
            "validated": True,
            "error": (
                "the new rules did not load; MEHO loaded the saved rules again and stopped "
                "the timer"
            ),
        }
    if stage == "apply_restore_failed":
        raise LinuxWriteError(
            "the new rules did not load, and loading the saved rules again also failed. The "
            f"timer still runs and tries once more when it ends (rollback_id {rollback_id}). "
            "Check the live rules with linux.firewall.show."
        )
    raise LinuxWriteError(
        "the firewall load did not finish, and MEHO cannot tell how far it got. If it "
        f"started the timer, the old rules come back within {ROLLBACK_MAX_SECONDS} seconds. "
        f"Check with linux.firewall.show. {stderr}".strip()
    )


async def load_firewall_with_rollback(
    connector: LinuxSshConnector,
    target: Target,
    ruleset: str,
    backend: str,
    seconds: int,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run the timed load of ``linux.firewall.load`` (``rollback_after_seconds`` set)."""
    rollback_id = secrets.token_hex(8)
    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_firewall_load_with_rollback_script(
        ruleset, backend, rollback_id=rollback_id, seconds=seconds
    )
    proc = await run_remote_bash_with_sudo(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    return parse_load_with_rollback_output(proc, backend, rollback_id)


# ===========================================================================
# linux.firewall.confirm
# ===========================================================================


def build_firewall_confirm_script(rollback_id: str, install_path: str | None) -> str:
    """Render the root script that stops the timer and maybe installs the rules.

    All steps run under the host lock, so the timer's restore cannot run
    at the same time. Order: refuse an unknown id, a timer that already ran,
    or a timer that ends in less than :data:`CONFIRM_MIN_SECONDS_LEFT`
    seconds; check the new rules when they are to be installed; check the
    time left again; write ``keep``; stop the timer; install. Each step
    prints one ``MEHO_FWC=<state>`` line.
    """
    state = _state_dir(rollback_id)
    unit = f"{_UNIT_PREFIX}{rollback_id}"
    p = _CONFIRM_PREFIX
    too_late = (
        f'left=$(( $(cat "$dir/due") - $(date +%s) )); '
        f'if [ "$left" -lt {CONFIRM_MIN_SECONDS_LEFT} ]; then echo "{p}too_late:$left"; exit 0; fi'
    )
    lines = [
        "set -u",
        "umask 077",
        f"dir={shlex.quote(state)}",
        f"unit={shlex.quote(unit)}",
        f'if [ ! -f "$dir/due" ]; then echo "{p}unknown"; exit 0; fi',
        f'exec 9>>{shlex.quote(_lock_file())} || {{ echo "{p}busy"; exit 4; }}',
        f'flock -w {LOCK_WAIT_SECONDS} 9 || {{ echo "{p}busy"; exit 4; }}',
        f'if [ ! -f "$dir/due" ]; then echo "{p}unknown"; exit 0; fi',
        f'if [ -f "$dir/restored" ]; then echo "{p}expired_restored"; rm -rf -- "$dir"; exit 0; fi',
        'if [ $(( $(cat "$dir/due") - $(date +%s) )) -lt 0 ] && '
        '! systemctl is-active --quiet "$unit.service"; then',
        f'  echo "{p}expired_unclear"; exit 0',
        "fi",
        too_late,
    ]
    if install_path is not None:
        lines += [
            f"path={shlex.quote(install_path)}",
            'backend="$(cat "$dir/backend")"',
            'check() { if [ "$backend" = iptables ]; then iptables-restore --test < "$1"; '
            'else nft -c -f "$1"; fi; }',
            f'if [ -d "$path" ] || ! check "$dir/new.rules"; then echo "{p}install_check_failed"; '
            "exit 0; fi",
            too_late,
        ]
    lines += [
        f'touch "$dir/keep" || {{ echo "{p}stop_failed"; exit 4; }}',
        'if ! systemctl stop "$unit.timer"; then',
        f'  rm -f -- "$dir/keep"; echo "{p}stop_failed"; exit 4',
        "fi",
        f'echo "{p}stopped"',
    ]
    if install_path is not None:
        lines += [
            'tmp="$(mktemp "$(dirname -- "$path")/.meho-fw.XXXXXX")" '
            f'|| {{ echo "{p}install_failed"; exit 4; }}',
            'if ! cat "$dir/new.rules" > "$tmp"; then rm -f "$tmp"; '
            f'echo "{p}install_failed"; exit 4; fi',
            'if [ -e "$path" ]; then chmod --reference="$path" "$tmp"; else chmod 0600 "$tmp"; fi',
            f'if ! mv -f "$tmp" "$path"; then rm -f "$tmp"; echo "{p}install_failed"; exit 4; fi',
            f'echo "{p}installed"',
        ]
    lines += ['rm -rf -- "$dir"', f'echo "{p}done"']
    return "\n".join(lines) + "\n"


async def _run_sudo_on_new_connection(
    connector: LinuxSshConnector,
    target: Target,
    script: str,
    *,
    operator: Operator | None,
    sudo_password: str,
    timeout: float = 60.0,
) -> asyncssh.SSHCompletedProcess:
    """Run *script* under sudo on a new SSH connection, then close it.

    The same wire shape as
    :func:`~meho_backplane.connectors.linux._sudo.run_remote_bash_with_sudo`
    (the password rides on stdin after the script bytes), but the connection
    does not come from the pool and does not go into it. Only a new login
    proves that the new rules let MEHO in.
    """
    if any(ch in sudo_password for ch in ("\n", "\r", "\x00")):
        raise ValueError("sudo_password must be a single line")
    auth_kwargs = await connector._auth_config(target, operator)
    if "known_hosts" not in auth_kwargs:
        raise SshHostKeyUnpinnedError(getattr(target, "name", "?"))
    conn = await _dial(target, auth_kwargs)
    try:
        cmd = build_sudo_bash_remote_cmd(len(script.encode("utf-8")))
        return await asyncio.wait_for(
            run_capped(
                conn,
                cmd,
                target_name=getattr(target, "name", "?"),
                stdin=f"{script}{sudo_password}\n",
            ),
            timeout=timeout,
        )
    finally:
        conn.close()
        await conn.wait_closed()


def parse_confirm_output(proc: Any, rollback_id: str, install_path: str | None) -> dict[str, Any]:
    """Turn the confirm script's output into the answer, or raise."""
    states = [
        line[len(_CONFIRM_PREFIX) :].strip()
        for line in _stdout(proc).splitlines()
        if line.startswith(_CONFIRM_PREFIX)
    ]
    first = states[0] if states else ""
    if first == "unknown":
        raise LinuxFirewallConfirmError(
            f"no timed firewall change with rollback_id {rollback_id!r} on this host"
        )
    if first == "expired_restored":
        raise LinuxFirewallConfirmError(
            "the timer already ran, and the old rules are back. Load the new rules again "
            "to try once more."
        )
    if first == "expired_unclear":
        raise LinuxFirewallConfirmError(
            "the timer already ran, but MEHO cannot see that the old rules came back. "
            "Check the live rules with linux.firewall.show."
        )
    if first.startswith("too_late"):
        raise LinuxFirewallConfirmError(
            f"the timer ends in less than {CONFIRM_MIN_SECONDS_LEFT} seconds, so MEHO did "
            "not stop it. The old rules are coming back."
        )
    if first == "busy":
        raise LinuxFirewallConfirmError(
            "another firewall step on this host holds the lock, so MEHO did not stop the "
            "timer. Try again before the timer ends, or the old rules come back."
        )
    if first == "install_check_failed":
        raise LinuxFirewallConfirmError(
            "the new rules failed the check before install, or install_path is a folder. "
            "MEHO did not stop the timer, so the old rules are coming back."
        )
    if "stopped" not in states:
        raise LinuxFirewallConfirmError(
            "MEHO could not stop the timer, so the old rules are coming back. "
            f"{_stderr(proc)}".strip()
        )
    result: dict[str, Any] = {
        "confirmed": True,
        "rollback_id": rollback_id,
        "installed": "installed" in states,
        "install_path": install_path,
    }
    if install_path is not None and not result["installed"]:
        result["error"] = (
            "the timer is stopped and the new rules stay live, but they were not "
            "installed, so a reboot brings back the rules of the boot file"
        )
    return result


async def linux_firewall_confirm(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.firewall.confirm`` -- keep the new rules of a timed load."""
    rollback_id = validate_rollback_id(params.get("rollback_id"))
    raw_path = params.get("install_path")
    install_path = confine_write_path(raw_path) if raw_path is not None else None
    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_firewall_confirm_script(rollback_id, install_path)
    proc = await _run_sudo_on_new_connection(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    return parse_confirm_output(proc, rollback_id, install_path)


_FIREWALL_CONFIRM_OP = LinuxOp(
    op_id="linux.firewall.confirm",
    handler_attr="firewall_confirm",
    summary="Keep firewall rules loaded with a timer: stop the timer, optionally install them.",
    description=(
        "Stops the timer that linux.firewall.load started, so the new rules stay. "
        "Uses a new SSH login, which proves the new rules let MEHO in. With "
        "install_path it also writes the rules to the file the host loads at boot. "
        "Returns {confirmed, rollback_id, installed, install_path}."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "rollback_id": {
                "type": "string",
                "pattern": _ROLLBACK_ID_PATTERN,
                "description": "The rollback_id that linux.firewall.load returned.",
            },
            "install_path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional boot file to write the new rules to, for example "
                    "/etc/nftables.conf. Must be under /etc, /var/lib, /run, /opt, "
                    "/srv or /usr/local."
                ),
            },
        },
        "required": ["rollback_id"],
        "additionalProperties": False,
        "examples": [
            {"rollback_id": "0123456789abcdef"},
            {"rollback_id": "0123456789abcdef", "install_path": "/etc/nftables.conf"},
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "confirmed": {"type": "boolean"},
            "rollback_id": {"type": "string"},
            "installed": {"type": "boolean"},
            "install_path": {"type": ["string", "null"]},
            "error": {"type": "string"},
        },
        "required": ["confirmed", "rollback_id", "installed"],
        "additionalProperties": False,
    },
    group_key="firewall",
    tags=("write", "firewall", "linux"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after linux.firewall.load with rollback_after_seconds, once your "
            "checks over new connections pass. Without this call the host puts the "
            "old rules back when the timer ends. " + SSH_TRANSPORT_NOTE
        ),
        "preconditions": (
            "A rollback_id from linux.firewall.load, and more than "
            f"{CONFIRM_MIN_SECONDS_LEFT} seconds left on its timer. This step waits "
            "for a person to approve it, like every change made with a token, so the "
            "timer must leave time for that approval."
        ),
        "parameter_hints": {
            "rollback_id": "Copy it from the answer of linux.firewall.load.",
            "install_path": (
                "Set it to make the rules survive a reboot. It must be the file the "
                "host loads at boot, for example /etc/nftables.conf."
            ),
        },
        "output_shape": (
            "{confirmed, rollback_id, installed, install_path}. An error means the timer "
            "was not stopped; the message says whether the old rules are back."
        ),
        "next_step": (
            "Read the live rules with linux.firewall.show. After an error, load the "
            "rules again if you still want them."
        ),
    },
)


#: The firewall confirm step, composed onto ``LINUX_OPS``.
FIREWALL_ROLLBACK_OPS: tuple[LinuxOp, ...] = (_FIREWALL_CONFIRM_OP,)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed write verbs for :class:`LinuxSshConnector` (T2 write surface).

The day-2 configuration and remediation tier for the generic ``linux-ssh``
connector (Initiative branch (b) of the guest-ops fork). Five state-changing
verbs turn the remediation moves an operator performs today over unaudited
bare SSH -- writing a missing config file, controlling a dead service,
re-running an aborted first-boot script, correcting a kernel parameter,
reloading the firewall -- into governed, previewable, audited operations.

Tier table:

* ``linux.file.write`` -- ``dangerous`` + approval (group ``file``). Atomic
  backup -> write (``umask 077`` temp + rename) -> optional
  ``validate_command`` -> rollback-on-fail of a path-confined, allow-listed
  config file. Its ``content`` may carry secrets, so it is pinned
  ``credential_write`` in the broadcast clamp and previews only at park time
  via its bespoke builder (never the content).
* ``linux.service.control`` -- ``caution``, no approval (group ``service``).
  One op with an ``action`` enum for a named unit. Recoverable, so it follows
  the estate service.* precedent: it runs immediately, not parked.
* ``linux.script.run`` -- ``dangerous`` + approval (group ``exec``). The
  intentional arbitrary-code surface: upload an operator-declared script to a
  ``umask 077`` temp path and execute it (optionally under sudo), capturing
  stdout / stderr / exit. Its ``arguments`` / ``env`` may carry secrets, so it
  is pinned ``credential_write`` and previews only at park time via its
  bespoke builder (never the body / argument values / env values).
* ``linux.sysctl.write`` -- ``dangerous`` + approval (group ``system``). Set a
  kernel parameter (runtime ``sysctl -w`` + a persistent drop-in). Non-secret,
  so it previews via ``preview_operation`` on the generic params-echo default.
* ``linux.firewall.load`` -- ``dangerous`` + approval (group ``firewall``).
  Apply / replace an ``nft`` (or iptables) ruleset, validated
  (``nft -c -f``) before it is applied. Non-secret, so it previews via
  ``preview_operation`` on the generic params-echo default -- the reviewer
  sees the ruleset, which is the point.

**No op is registered ``destructive``**, so the destructive-tier blast-radius
builder gate does not apply here.

**No approval logic lives in any handler.** The dispatcher parks a
``dangerous`` + ``requires_approval`` op and the handler body runs only on the
``_approved=True`` resume path (the estate write mold). Approval is a
human-only decision; there is no MCP decision path.

**Root elevation** copies the proven byte-identical sudo primitive
(:mod:`meho_backplane.connectors.linux._sudo`): the sudo password is streamed
on stdin, never in the remote ``argv`` / history / a log event. Every
operator-supplied value is ``shlex.quote``-wrapped or base64-carried into a
fixed command shape, and every operator-named path is confined under the
write-root allow-list *before* any SSH command is constructed.
"""

from __future__ import annotations

import base64
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.linux._sudo import run_remote_bash_with_sudo
from meho_backplane.connectors.linux.ops import (
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    ensure_path_under_root,
)
from meho_backplane.connectors.linux.ops_host import (
    SYSCTL_KEY_PATTERN,
    UNIT_NAME_PATTERN,
    validate_sysctl_key,
    validate_unit_name,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "FIREWALL_BACKENDS",
    "LINUX_WRITE_ROOTS",
    "SERVICE_ACTIONS",
    "WRITE_OPS",
    "LinuxWriteError",
    "LinuxWriteSafetyError",
    "build_file_write_script",
    "build_firewall_load_script",
    "build_script_run_wrapper",
    "build_service_control_script",
    "build_sysctl_write_script",
    "confine_write_path",
    "linux_file_write",
    "linux_firewall_load",
    "linux_script_run",
    "linux_service_control",
    "linux_sysctl_write",
    "register_linux_write_previews",
]


# ===========================================================================
# Errors + shared machinery
# ===========================================================================


class LinuxWriteError(ValueError):
    """A write op could not resolve a precondition (e.g. no sudo credential).

    A :class:`ValueError` subclass so the dispatcher's handler-exception catch
    maps it to a ``connector_error`` result rather than an unhandled crash.
    """


class LinuxWriteSafetyError(ValueError):
    """An operator-supplied write value failed a fail-closed safety re-check.

    Raised *before* any SSH command is constructed, so a rejected value never
    reaches the host. A :class:`ValueError` subclass (same dispatcher mapping
    as :class:`LinuxWriteError`).
    """


#: The narrow allow-list of absolute POSIX roots ``linux.file.write`` confines
#: its target path under -- the config / runtime-state trees a day-2
#: remediation writes. Deliberately narrower than the *read* roots
#: (:data:`~meho_backplane.connectors.linux.ops.LINUX_READ_ROOTS`): the read
#: floor reads ``/proc`` / ``/sys`` / ``/var/log``, but writing there is a
#: footgun (``/proc`` / ``/sys`` are the sysctl op's job; ``/var/log`` is not a
#: config surface), so those are excluded here. Confinement is lexical (the
#: bind9 mold): a resolved path must descend one of these roots.
LINUX_WRITE_ROOTS: tuple[str, ...] = (
    "/etc",
    "/var/lib",
    "/run",
    "/opt",
    "/srv",
    "/usr/local",
)

#: The recoverable systemd actions ``linux.service.control`` accepts. No
#: destructive verb (no ``mask`` / ``kill``); every action here is reversible.
SERVICE_ACTIONS: tuple[str, ...] = (
    "start",
    "stop",
    "restart",
    "reload",
    "enable",
    "disable",
)

#: The firewall backends ``linux.firewall.load`` can drive.
FIREWALL_BACKENDS: tuple[str, ...] = ("nft", "iptables")

#: The default interpreter for ``linux.script.run`` when none is declared.
_DEFAULT_INTERPRETER: str = "/bin/bash"

#: Suffix of the sibling backup ``linux.file.write`` writes before replacing an
#: existing file, so the park-time preview can echo a deterministic path and
#: the rollback-on-validate-failure restores from a known location.
_BACKUP_SUFFIX: str = ".meho.bak"

#: Persistent-drop-in directory + filename prefix for ``linux.sysctl.write``.
_SYSCTL_DROPIN_DIR: str = "/etc/sysctl.d"
_SYSCTL_DROPIN_PREFIX: str = "60-meho-"

#: sysctl value charset: digits / letters / dot / dash / slash / comma / colon
#: / space (space-separated scalar lists like ``4096 87380 6291456`` are
#: legitimate kernel values). Admits no shell metacharacter; the value is
#: ``shlex.quote``d regardless.
_SYSCTL_VALUE_CHARSET: frozenset[str] = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-/:, "
)

#: env-var NAME charset for ``linux.script.run`` (POSIX name rules). Values are
#: unconstrained (base64-carried); only the names are validated.
_ENV_NAME_CHARSET: frozenset[str] = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)

#: Hard cap + default for ``linux.script.run``'s wall-clock timeout.
_DEFAULT_TIMEOUT_S: int = 120
_MAX_TIMEOUT_S: int = 3600

#: Truncation bound for a captured stderr string returned inline.
_STDERR_CAP: int = 4096

# Output markers each fixed command prints so the handler can attribute the
# result honestly (present / absent / rolled-back) without parsing free text.
_FILE_WRITE_OK = "===MEHO_WRITE_OK==="
_FILE_WRITE_VALIDATE_FAILED = "===MEHO_WRITE_VALIDATE_FAILED==="
_SYSCTL_OK = "===MEHO_SYSCTL_OK==="
_FW_OK = "===MEHO_FW_OK==="
_FW_VALIDATE_FAILED = "===MEHO_FW_VALIDATE_FAILED==="


def _target_label(target: Any) -> str:
    """Human-facing target label for a result / preview -- name, else host."""
    name = getattr(target, "name", None)
    if isinstance(name, str) and name.strip():
        return name
    host = getattr(target, "host", None)
    return host if isinstance(host, str) and host.strip() else "unknown"


def confine_write_path(requested: str, roots: tuple[str, ...] = LINUX_WRITE_ROOTS) -> str:
    """Return *requested* confined under the first matching write root, or raise.

    The write-side analogue of
    :func:`~meho_backplane.connectors.linux.ops.confine_read_path`: an
    operator-named path must be absolute and land inside one of
    :data:`LINUX_WRITE_ROOTS`; ``..`` traversal is collapsed and rejected if it
    escapes, control bytes are refused, and a trailing-slash sentinel stops a
    sibling-prefix match. Raises
    :class:`~meho_backplane.connectors.linux.ops.PathConfinementError` before
    any SSH command is constructed.
    """
    from meho_backplane.connectors.linux.ops import PathConfinementError

    if not isinstance(requested, str) or not requested.startswith("/"):
        raise PathConfinementError(
            f"write path {requested!r} must be an absolute POSIX path (starting with '/')"
        )
    for root in roots:
        try:
            return ensure_path_under_root(requested, root)
        except PathConfinementError:
            continue
    raise PathConfinementError(
        f"path {requested!r} is outside every allowed write root {list(roots)!r}"
    )


async def _resolve_sudo_password(
    connector: LinuxSshConnector, target: Any, operator: Operator | None
) -> str:
    """Resolve the sudo password from the target's Vault secret.

    Keys on a dedicated ``sudo_password`` field first, falling back to the SSH
    ``password`` (the estate convention). Raises :class:`LinuxWriteError` when
    neither is set -- a root write cannot legitimately proceed without a sudo
    credential. The value is whitespace-stripped (a trailing-newline artifact
    is the common storage case); the copied sudo primitive additionally rejects
    an *internal* control character before opening the connection.
    """
    secret = await connector._resolve_secret(target, operator)
    password = secret.get("sudo_password") or secret.get("password")
    if not password:
        raise LinuxWriteError(
            "the target's Vault secret carries no sudo_password / password; a "
            "governed root write needs a sudo credential"
        )
    return strip_credential_value(password)


def _stdout(proc: Any) -> str:
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    return raw if isinstance(raw, str) else ""


def _stderr(proc: Any) -> str:
    raw = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    text = raw if isinstance(raw, str) else ""
    return text[:_STDERR_CAP]


def _b64(text: str) -> str:
    """Return the base64 of *text* (safe to splice into a shell command line)."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ===========================================================================
# linux.file.write
# ===========================================================================


def build_file_write_script(
    confined_path: str, content: str, *, backup: bool, validate_command: str | None
) -> str:
    """Render the root-owned atomic file-write bash script.

    Backup (optional) -> atomic write (``umask 077``, base64 -> same-dir temp ->
    rename) -> optional ``validate_command`` -> rollback-on-fail. The content
    and the validate command are base64-carried (never spliced raw); every path
    is ``shlex.quote``d. ``set -e`` is deliberately *not* used so a non-zero
    validate exit routes to the rollback branch rather than aborting the script
    before the original is restored. The validate command runs with the written
    path exported as ``MEHO_FILE`` so a validator can reference it.
    """
    q_path = shlex.quote(confined_path)
    q_backup = shlex.quote(confined_path + _BACKUP_SUFFIX)
    q_b64 = shlex.quote(_b64(content))
    do_backup = "1" if backup else "0"
    lines = [
        "set -u",
        "umask 077",
        f"path={q_path}",
        f"backup={q_backup}",
        f"do_backup={do_backup}",
        "had_backup=0",
        'if [ "$do_backup" = 1 ] && [ -e "$path" ]; then',
        '  cp -a -- "$path" "$backup" && had_backup=1',
        "fi",
        'dir="$(dirname -- "$path")"',
        'tmp="$(mktemp "$dir/.meho-write.XXXXXX")" || { echo "===MEHO_WRITE_ERR:mktemp===" >&2; exit 4; }',  # noqa: E501
        f'if ! printf %s {q_b64} | base64 -d > "$tmp"; then rm -f "$tmp"; echo "===MEHO_WRITE_ERR:decode===" >&2; exit 4; fi',  # noqa: E501
        'chmod 0600 "$tmp"',
        'if ! mv -f "$tmp" "$path"; then rm -f "$tmp"; echo "===MEHO_WRITE_ERR:mv===" >&2; exit 4; fi',  # noqa: E501
    ]
    if validate_command is not None:
        q_vb64 = shlex.quote(_b64(validate_command))
        lines += [
            'export MEHO_FILE="$path"',
            f'vcmd="$(printf %s {q_vb64} | base64 -d)"',
            'if ! bash -c "$vcmd"; then',
            '  if [ "$had_backup" = 1 ]; then mv -f "$backup" "$path"; else rm -f "$path"; fi',
            f'  echo "{_FILE_WRITE_VALIDATE_FAILED}"',
            "  exit 3",
            "fi",
        ]
    lines.append(f'echo "{_FILE_WRITE_OK}"')
    return "\n".join(lines) + "\n"


def parse_file_write_output(
    confined_path: str,
    stdout: str,
    exit_status: int | None,
    *,
    backup: bool,
    validated: bool,
) -> dict[str, Any]:
    """Parse the ``file.write`` command output into the response dict.

    Returns ``{written, path, backup_path, validated, rolled_back}``. The
    ``===MEHO_WRITE_VALIDATE_FAILED===`` marker (exit 3) means the write was
    rolled back and the original is back in place; ``===MEHO_WRITE_OK===`` means
    the write landed (and validated, when a validate command was supplied).
    """
    backup_path = (confined_path + _BACKUP_SUFFIX) if backup else None
    if _FILE_WRITE_VALIDATE_FAILED in stdout:
        return {
            "written": False,
            "path": confined_path,
            "backup_path": backup_path,
            "validated": False,
            "rolled_back": True,
        }
    if _FILE_WRITE_OK in stdout and exit_status in (0, None):
        return {
            "written": True,
            "path": confined_path,
            "backup_path": backup_path,
            "validated": validated,
            "rolled_back": False,
        }
    return {
        "written": False,
        "path": confined_path,
        "backup_path": backup_path,
        "validated": False,
        "rolled_back": False,
        "error": "atomic file write did not complete",
    }


async def linux_file_write(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.file.write`` (approved-resume path only).

    Confines the operator-named ``path`` under the write-root allow-list
    (traversal rejected before any SSH), then runs one sudo bash script that
    backs up the existing file (unless ``backup=false``), writes the new content
    atomically, optionally runs ``validate_command``, and rolls back to the
    backup if validation fails. Never echoes the content in the result.
    """
    confined = confine_write_path(params["path"])
    content = params["content"]
    if not isinstance(content, str):
        raise LinuxWriteSafetyError("content must be a string")
    backup = bool(params.get("backup", True))
    validate_command = params.get("validate_command")
    if validate_command is not None and (
        not isinstance(validate_command, str) or not validate_command.strip()
    ):
        raise LinuxWriteSafetyError("validate_command, when set, must be a non-empty string")

    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_file_write_script(
        confined, content, backup=backup, validate_command=validate_command
    )
    proc = await run_remote_bash_with_sudo(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    return parse_file_write_output(
        confined,
        _stdout(proc),
        getattr(proc, "exit_status", None),
        backup=backup,
        validated=validate_command is not None,
    )


# ===========================================================================
# linux.service.control
# ===========================================================================


def bound_service_action(raw: Any) -> str:
    """Return the validated service action, or raise :class:`LinuxWriteSafetyError`."""
    if not isinstance(raw, str) or raw not in SERVICE_ACTIONS:
        raise LinuxWriteSafetyError(f"action must be one of {list(SERVICE_ACTIONS)}")
    return raw


def build_service_control_script(action: str, unit: str, *, daemon_reload: bool) -> str:
    """Render the ``systemctl <action> <unit>`` command (optional daemon-reload).

    *action* is a member of :data:`SERVICE_ACTIONS` (validated by the caller);
    *unit* is charset-validated and ``shlex.quote``d.
    """
    q_unit = shlex.quote(unit)
    prefix = "systemctl daemon-reload; " if daemon_reload else ""
    return f"set -e; {prefix}systemctl {action} {q_unit}\n"


async def linux_service_control(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.service.control`` -- start/stop/restart/... a unit.

    ``caution`` tier (no approval): a recoverable service action runs
    immediately. Re-validates the action + unit name before any SSH, runs the
    ``systemctl`` verb under sudo, and reports the exit status.
    """
    action = bound_service_action(params["action"])
    unit = validate_unit_name(params["unit"])
    daemon_reload = bool(params.get("daemon_reload", False))

    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_service_control_script(action, unit, daemon_reload=daemon_reload)
    proc = await run_remote_bash_with_sudo(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    ok = getattr(proc, "exit_status", None) == 0
    result: dict[str, Any] = {
        "unit": unit,
        "action": action,
        "daemon_reload": daemon_reload,
        "ok": ok,
    }
    if not ok:
        result["error"] = _stderr(proc) or "systemctl returned a non-zero status"
    return result


# ===========================================================================
# linux.script.run
# ===========================================================================


def _validate_abspath(raw: Any, field: str) -> str:
    """Return an absolute, control-char-free path, or raise."""
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise LinuxWriteSafetyError(f"{field} must be an absolute POSIX path")
    if any(ch in raw for ch in ("\x00", "\n", "\r")):
        raise LinuxWriteSafetyError(f"{field} contains a control character")
    return raw


def _validate_env(raw: Any) -> dict[str, str]:
    """Return a validated ``{name: value}`` env map (names charset-checked)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LinuxWriteSafetyError("env must be an object of name -> value")
    env: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or not set(name) <= _ENV_NAME_CHARSET:
            raise LinuxWriteSafetyError(f"env var name {name!r} is not a valid POSIX name")
        if name[0].isdigit():
            raise LinuxWriteSafetyError(f"env var name {name!r} must not start with a digit")
        env[name] = str(value)
    return env


def _bounded_timeout(raw: Any) -> int:
    """Return the validated wall-clock timeout (default + hard cap)."""
    if raw is None:
        return _DEFAULT_TIMEOUT_S
    value = int(raw)
    if value < 1:
        raise LinuxWriteSafetyError("timeout_seconds must be >= 1")
    return min(value, _MAX_TIMEOUT_S)


def build_script_run_wrapper(
    interpreter: str,
    script: str,
    *,
    arguments: str | None,
    working_directory: str | None,
    env: dict[str, str],
) -> str:
    """Render the bash wrapper that runs an operator script under an interpreter.

    The operator's script body, its argument string, and every env value are
    base64-carried (never spliced raw); the interpreter and working directory
    are ``shlex.quote``d. The wrapper writes the body to a ``umask 077`` temp
    file, exports the env, changes to the working directory, and executes
    ``<interpreter> <tmp> <arguments>`` as its final command so the interpreter's
    stdout / stderr / exit status become the wrapper's. The temp file is removed
    on exit. Setup faults print a marker to *stderr* (never stdout) and exit 4,
    so a caller can tell a setup failure from a script exit.
    """
    q_interp = shlex.quote(interpreter)
    q_b64 = shlex.quote(_b64(script))
    lines = [
        "set -u",
        "umask 077",
        'tmp="$(mktemp)" || { echo "===MEHO_RUN_ERR:mktemp===" >&2; exit 4; }',
        "trap 'rm -f \"$tmp\"' EXIT",
        f'if ! printf %s {q_b64} | base64 -d > "$tmp"; then echo "===MEHO_RUN_ERR:decode===" >&2; exit 4; fi',  # noqa: E501
        'chmod 0700 "$tmp"',
    ]
    for name, value in env.items():
        q_val = shlex.quote(_b64(value))
        lines.append(f'export {name}="$(printf %s {q_val} | base64 -d)"')
    if working_directory is not None:
        q_wd = shlex.quote(working_directory)
        lines.append(f'cd {q_wd} || {{ echo "===MEHO_RUN_ERR:chdir===" >&2; exit 4; }}')
    if arguments:
        q_args = shlex.quote(_b64(arguments))
        lines.append(f'args="$(printf %s {q_args} | base64 -d)"')
        # Unquoted expansion is intentional: the operator declares a command
        # line and expects word-splitting. This is the sanctioned
        # arbitrary-code surface, governed by approval.
        lines.append(f'{q_interp} "$tmp" $args')
    else:
        lines.append(f'{q_interp} "$tmp"')
    return "\n".join(lines) + "\n"


async def linux_script_run(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.script.run`` (approved-resume path only).

    Uploads the operator-declared ``script`` to a ``umask 077`` temp file and
    runs it under ``interpreter`` (default ``/bin/bash``), optionally under
    sudo, with the declared ``arguments`` / ``working_directory`` / ``env`` /
    ``timeout_seconds``. Returns ``{stdout, stderr, exit_code, ...}`` where
    ``stdout`` is a list of lines that spills to a ``result_query`` handle when
    large. The intentional arbitrary-code surface -- a typed verb governed by
    approval, not an interactive shell.
    """
    script = params["script"]
    if not isinstance(script, str):
        raise LinuxWriteSafetyError("script must be a string")
    interpreter = _validate_abspath(
        params.get("interpreter") or _DEFAULT_INTERPRETER, "interpreter"
    )
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, str):
        raise LinuxWriteSafetyError("arguments, when set, must be a string")
    working_directory = params.get("working_directory")
    if working_directory is not None:
        working_directory = _validate_abspath(working_directory, "working_directory")
    env = _validate_env(params.get("env"))
    timeout = _bounded_timeout(params.get("timeout_seconds"))
    use_sudo = bool(params.get("use_sudo", False))

    wrapper = build_script_run_wrapper(
        interpreter,
        script,
        arguments=arguments,
        working_directory=working_directory,
        env=env,
    )

    if use_sudo:
        sudo_password = await _resolve_sudo_password(connector, target, operator)
        proc = await run_remote_bash_with_sudo(
            connector,
            target,
            wrapper,
            operator=operator,
            sudo_password=sudo_password,
            timeout=float(timeout),
        )
    else:
        cmd = f"bash -c {shlex.quote(wrapper)}"
        proc = await connector._run_command(target, cmd, operator=operator, timeout=float(timeout))

    stdout_lines = _stdout(proc).splitlines()
    return {
        "stdout": stdout_lines,
        "total": len(stdout_lines),
        "stderr": _stderr(proc),
        "exit_code": getattr(proc, "exit_status", None),
        "interpreter": interpreter,
        "used_sudo": use_sudo,
    }


# ===========================================================================
# linux.sysctl.write
# ===========================================================================


def _validate_sysctl_value(raw: Any) -> str:
    """Return a charset-validated sysctl value, or raise."""
    if not isinstance(raw, str) or not raw:
        raise LinuxWriteSafetyError("value must be a non-empty string")
    if not set(raw) <= _SYSCTL_VALUE_CHARSET:
        raise LinuxWriteSafetyError("value contains a disallowed character")
    return raw


def _sysctl_dropin_path(key: str) -> str:
    """Return the persistent drop-in path for *key* (a deterministic slug)."""
    slug = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in key).strip("-")
    return f"{_SYSCTL_DROPIN_DIR}/{_SYSCTL_DROPIN_PREFIX}{slug or 'param'}.conf"


def build_sysctl_write_script(key: str, value: str, dropin_path: str) -> str:
    """Render the ``sysctl -w`` + persistent-drop-in bash script.

    Applies the parameter at runtime and writes a drop-in so it survives a
    reboot. The ``key=value`` pair is ``shlex.quote``d as one word (both are
    charset-validated by the caller, so quoting is defence in depth); the drop-in
    ``key`` / ``value`` and *dropin_path* are ``shlex.quote``d individually.
    """
    q_pair = shlex.quote(f"{key}={value}")
    q_key = shlex.quote(key)
    q_val = shlex.quote(value)
    q_dropin = shlex.quote(dropin_path)
    return (
        "set -u\n"
        f"if ! sysctl -w {q_pair} >/dev/null; then "
        'echo "===MEHO_SYSCTL_ERR:runtime===" >&2; exit 4; fi\n'
        "umask 022\n"
        f"if ! printf '%s = %s\\n' {q_key} {q_val} > {q_dropin}; then "
        'echo "===MEHO_SYSCTL_ERR:persist===" >&2; exit 4; fi\n'
        f'echo "{_SYSCTL_OK}"\n'
    )


async def linux_sysctl_write(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.sysctl.write`` (approved-resume path only).

    Sets a kernel parameter at runtime (``sysctl -w``) and writes a persistent
    drop-in under ``/etc/sysctl.d`` so the change survives a reboot. The key and
    value are charset-validated before any SSH.
    """
    key = validate_sysctl_key(params["key"])
    value = _validate_sysctl_value(params["value"])
    dropin = _sysctl_dropin_path(key)

    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_sysctl_write_script(key, value, dropin)
    proc = await run_remote_bash_with_sudo(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    applied = _SYSCTL_OK in _stdout(proc) and getattr(proc, "exit_status", None) in (0, None)
    result: dict[str, Any] = {
        "key": key,
        "value": value,
        "applied": applied,
        "dropin_path": dropin,
    }
    if not applied:
        result["error"] = _stderr(proc) or "sysctl write did not complete"
    return result


# ===========================================================================
# linux.firewall.load
# ===========================================================================


def bound_firewall_backend(raw: Any) -> str:
    """Return the validated firewall backend (default ``nft``)."""
    if raw is None:
        return "nft"
    if raw not in FIREWALL_BACKENDS:
        raise LinuxWriteSafetyError(f"backend must be one of {list(FIREWALL_BACKENDS)}")
    return str(raw)


def build_firewall_load_script(ruleset: str, backend: str) -> str:
    """Render the validate-before-apply firewall-load bash script.

    Writes the ruleset to a temp file (base64-carried, never spliced raw),
    **validates it before applying** (``nft -c -f`` / ``iptables-restore
    --test``), and applies it only if validation passes. A validation failure
    prints ``===MEHO_FW_VALIDATE_FAILED===`` and exits 3 without touching the
    live ruleset.
    """
    q_b64 = shlex.quote(_b64(ruleset))
    if backend == "iptables":
        check_cmd = 'iptables-restore --test < "$tmp"'
        apply_cmd = 'iptables-restore < "$tmp"'
    else:
        check_cmd = 'nft -c -f "$tmp"'
        apply_cmd = 'nft -f "$tmp"'
    return (
        "set -u\n"
        'tmp="$(mktemp)" || { echo "===MEHO_FW_ERR:mktemp===" >&2; exit 4; }\n'
        "trap 'rm -f \"$tmp\"' EXIT\n"
        f'if ! printf %s {q_b64} | base64 -d > "$tmp"; then '
        'echo "===MEHO_FW_ERR:decode===" >&2; exit 4; fi\n'
        f'if ! {check_cmd}; then echo "{_FW_VALIDATE_FAILED}" >&2; exit 3; fi\n'
        f'if ! {apply_cmd}; then echo "===MEHO_FW_ERR:apply===" >&2; exit 4; fi\n'
        f'echo "{_FW_OK}"\n'
    )


async def linux_firewall_load(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.firewall.load`` (approved-resume path only).

    Applies / replaces the host firewall ruleset atomically, **validating it
    before applying** (``nft -c -f`` / ``iptables-restore --test``): a ruleset
    that fails validation never touches the live firewall. Returns
    ``{applied, backend, validated, rolled_back?}``.
    """
    ruleset = params["ruleset"]
    if not isinstance(ruleset, str) or not ruleset.strip():
        raise LinuxWriteSafetyError("ruleset must be a non-empty string")
    backend = bound_firewall_backend(params.get("backend"))

    sudo_password = await _resolve_sudo_password(connector, target, operator)
    script = build_firewall_load_script(ruleset, backend)
    proc = await run_remote_bash_with_sudo(
        connector, target, script, operator=operator, sudo_password=sudo_password
    )
    stdout = _stdout(proc)
    stderr = _stderr(proc)
    if _FW_VALIDATE_FAILED in stderr or _FW_VALIDATE_FAILED in stdout:
        return {
            "applied": False,
            "backend": backend,
            "validated": False,
            "error": "ruleset failed validation; the live firewall was not changed",
        }
    applied = _FW_OK in stdout and getattr(proc, "exit_status", None) in (0, None)
    result: dict[str, Any] = {"applied": applied, "backend": backend, "validated": applied}
    if not applied:
        result["error"] = stderr or "firewall load did not complete"
    return result


# ===========================================================================
# Approval-park preview builders (bespoke; credential-class; never values)
# ===========================================================================


async def _linux_file_write_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``linux.file.write`` -- path + byte size, never content.

    ``linux.file.write`` classifies ``credential_write`` (its ``content`` may
    carry secrets), so ``preview_operation`` returns ``preview_unavailable`` for
    it; this bespoke builder is the deliberate exception that runs *at park
    time* (a bespoke builder owns its own field discipline). It echoes the
    target, the path, the content byte size, the deterministic backup path, and
    the validate command (not a secret) -- never the ``content``. Declines
    (``None``) on malformed params.
    """
    path = ctx.params.get("path")
    content = ctx.params.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        return None
    backup = bool(ctx.params.get("backup", True))
    return {
        "target": _target_label(ctx.target),
        "path": path,
        "content_bytes": len(content.encode("utf-8")),
        "backup_path": (path + _BACKUP_SUFFIX) if backup else None,
        "validate_command": ctx.params.get("validate_command"),
    }


async def _linux_script_run_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``linux.script.run`` -- program identity, never values.

    ``linux.script.run`` classifies ``credential_write`` (its ``arguments`` /
    ``env`` may carry secrets), so ``preview_operation`` returns
    ``preview_unavailable`` for it; this bespoke builder runs *at park time* and
    echoes only the *shape*: the target, the interpreter, the script byte size,
    the argument byte size, the env-var NAMES, the working directory, the sudo
    intent, and the timeout -- never the script body, the argument values, or
    the env values (mirrors the guest program-run precedent). Declines
    (``None``) on malformed params.
    """
    script = ctx.params.get("script")
    if not isinstance(script, str):
        return None
    interpreter = ctx.params.get("interpreter") or _DEFAULT_INTERPRETER
    preview: dict[str, Any] = {
        "target": _target_label(ctx.target),
        "interpreter": interpreter,
        "script_bytes": len(script.encode("utf-8")),
        "use_sudo": bool(ctx.params.get("use_sudo", False)),
    }
    arguments = ctx.params.get("arguments")
    if isinstance(arguments, str) and arguments:
        preview["argument_bytes"] = len(arguments.encode("utf-8"))
    env = ctx.params.get("env")
    if isinstance(env, dict) and env:
        preview["env_var_names"] = sorted(str(name) for name in env)
    working_directory = ctx.params.get("working_directory")
    if isinstance(working_directory, str) and working_directory:
        preview["working_directory"] = working_directory
    timeout = ctx.params.get("timeout_seconds")
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        preview["timeout_seconds"] = timeout
    return preview


def register_linux_write_previews() -> None:
    """Register the two credential-class bespoke preview builders (import-time).

    ``linux.file.write`` and ``linux.script.run`` are pinned ``credential_write``
    in the broadcast clamp, so the generic params-echo default is suppressed for
    them; these bespoke builders are the sanctioned exception that give the
    approver a redaction-safe park-time preview. Idempotent (registration is an
    import side-effect; a re-import overwrites with the same builder). The two
    non-secret dangerous ops (``sysctl.write`` / ``firewall.load``) register no
    bespoke builder -- they preview via the generic params-echo default.
    """
    from meho_backplane.operations._preview import register_preview_builder

    register_preview_builder("linux.file.write", _linux_file_write_preview)
    register_preview_builder("linux.script.run", _linux_script_run_preview)


# ===========================================================================
# Op definitions
# ===========================================================================


_FILE_WRITE_OP = LinuxOp(
    op_id="linux.file.write",
    handler_attr="file_write",
    summary="Atomically write an allow-listed config file with backup + optional validate.",
    description=(
        "Writes CONTENT to a single file whose path is confined under the "
        "write-root allow-list (/etc, /var/lib, /run, /opt, /srv, /usr/local); "
        "a path outside every root, or one using traversal, is rejected before "
        "any SSH command runs. Flow: back up the existing file (unless "
        "backup=false), write the new content atomically (umask 077, temp + "
        "rename) as root, then -- if validate_command is set -- run it and roll "
        "back to the backup when it fails, so a bad config never stays live. "
        "safety_level=dangerous, requires_approval=true: a dispatch parks for a "
        "human before anything changes. Returns {written, path, backup_path, "
        "validated, rolled_back}; the content is never echoed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Absolute path to write, confined under the write-root "
                    "allow-list. Traversal outside a root is rejected."
                ),
            },
            "content": {
                "type": "string",
                "description": "The file body to write. Do NOT embed bare secrets.",
            },
            "validate_command": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional shell command run after the write (with the "
                    "written path exported as $MEHO_FILE); a non-zero exit rolls "
                    "the write back."
                ),
            },
            "backup": {
                "type": "boolean",
                "description": "Back up the existing file before writing (default true).",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "written": {"type": "boolean"},
            "path": {"type": "string"},
            "backup_path": {"type": ["string", "null"]},
            "validated": {"type": ["boolean", "null"]},
            "rolled_back": {"type": "boolean"},
        },
        "required": ["written", "path"],
        "additionalProperties": True,
    },
    group_key="file",
    tags=("write", "config", "linux"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": (
            "Call to write or repair a config file on a Linux host -- the "
            "missing config the first-boot run never wrote, a corrected "
            "directive -- with a backup and an optional validate/rollback gate. "
            "Approval-gated: it parks for a human. Do NOT put bare secrets in "
            "content (it is stored verbatim for the approval re-dispatch). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "path": "Absolute path under an allowed write root.",
            "content": "The file body; no bare secrets.",
            "validate_command": "Optional post-write check; failure rolls back.",
            "backup": "Optional; defaults to true.",
        },
        "output_shape": (
            "{written, path, backup_path, validated, rolled_back}. rolled_back "
            "is true when validate failed and the original was restored."
        ),
    },
)


_SERVICE_CONTROL_OP = LinuxOp(
    op_id="linux.service.control",
    handler_attr="service_control",
    summary="Start/stop/restart/reload/enable/disable a systemd unit (recoverable).",
    description=(
        "Runs one recoverable systemd action (start / stop / restart / reload / "
        "enable / disable) for a named unit, optionally preceded by a "
        "daemon-reload. The action is enum-validated and the unit name is "
        "charset-validated + quoted before any SSH. safety_level=caution, "
        "requires_approval=false: a recoverable service action runs immediately "
        "(the estate service.* precedent), it does NOT park. Returns {unit, "
        "action, daemon_reload, ok}."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(SERVICE_ACTIONS),
                "description": "The systemd action to perform.",
            },
            "unit": {
                "type": "string",
                "minLength": 1,
                "pattern": UNIT_NAME_PATTERN,
                "description": "systemd unit name, e.g. nginx or sshd.service.",
            },
            "daemon_reload": {
                "type": "boolean",
                "description": "Run systemctl daemon-reload first (default false).",
            },
        },
        "required": ["action", "unit"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "unit": {"type": "string"},
            "action": {"type": "string"},
            "daemon_reload": {"type": "boolean"},
            "ok": {"type": "boolean"},
        },
        "required": ["unit", "action", "ok"],
        "additionalProperties": True,
    },
    group_key="service",
    tags=("write", "service", "linux"),
    safety_level="caution",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to control a systemd unit -- restart a dead service, enable a "
            "unit the first-boot run left disabled. Recoverable, so it runs "
            "immediately without an approval park (unlike the other write "
            "verbs). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "action": "One of start/stop/restart/reload/enable/disable.",
            "unit": "A single systemd unit name.",
            "daemon_reload": "Optional; run daemon-reload first.",
        },
        "output_shape": "{unit, action, daemon_reload, ok}; ok is the systemctl exit verdict.",
    },
)


_SCRIPT_RUN_OP = LinuxOp(
    op_id="linux.script.run",
    handler_attr="script_run",
    summary="Upload and run an operator-declared script under an interpreter (optionally sudo).",
    description=(
        "Uploads an operator-declared script to a umask 077 temp path and runs "
        "it under interpreter (default /bin/bash), optionally under sudo, with "
        "an argument string, working directory, env map, and a wall-clock "
        "timeout; the temp file is removed afterwards. This is the intentional "
        "arbitrary-code surface -- a typed verb governed by approval, NOT an "
        "interactive shell. safety_level=dangerous, requires_approval=true: it "
        "parks for a human. Returns {stdout, stderr, exit_code, ...}; stdout is "
        "a line list that spills to a result handle when large."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "interpreter": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute interpreter path (default /bin/bash).",
            },
            "script": {
                "type": "string",
                "description": "The script body to run. Do NOT embed bare secrets.",
            },
            "arguments": {
                "type": "string",
                "description": "Optional command-line argument string (word-split).",
            },
            "working_directory": {
                "type": "string",
                "description": "Optional absolute working directory.",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Optional env map. Do NOT put bare secrets in values.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT_S,
                "description": "Wall-clock timeout (default 120).",
            },
            "use_sudo": {
                "type": "boolean",
                "description": "Run the script as root via sudo (default false).",
            },
        },
        "required": ["script"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "stdout": {"type": "array", "items": {"type": "string"}},
            "total": {"type": "integer"},
            "stderr": {"type": "string"},
            "exit_code": {"type": ["integer", "null"]},
            "interpreter": {"type": "string"},
            "used_sudo": {"type": "boolean"},
        },
        "required": ["stdout", "exit_code"],
        "additionalProperties": True,
    },
    group_key="exec",
    tags=("write", "exec", "linux"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": (
            "Call to run an operator-declared remediation script on a Linux host "
            "-- re-run the aborted first-boot script, apply a multi-step fix. "
            "Approval-gated arbitrary code: it parks for a human. Do NOT put "
            "bare secrets in arguments/env (they are stored verbatim for the "
            "approval re-dispatch). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "script": "The script body; no bare secrets.",
            "interpreter": "Absolute path; defaults to /bin/bash.",
            "arguments": "Optional command line; word-split.",
            "env": "Optional; no bare secrets in values.",
            "use_sudo": "Optional; run as root.",
        },
        "output_shape": (
            "{stdout, total, stderr, exit_code, interpreter, used_sudo}. stdout "
            "is a line list; large output spills to a result_query handle."
        ),
    },
)


_SYSCTL_WRITE_OP = LinuxOp(
    op_id="linux.sysctl.write",
    handler_attr="sysctl_write",
    summary="Set a kernel parameter at runtime and persist it as a drop-in.",
    description=(
        "Sets a single kernel parameter at runtime (sysctl -w) and writes a "
        "persistent drop-in under /etc/sysctl.d so it survives a reboot. The key "
        "and value are charset-validated + quoted before any SSH. "
        "safety_level=dangerous, requires_approval=true: it parks for a human. "
        "Non-secret, so a human can preview it via preview_operation before "
        "approving. Returns {key, value, applied, dropin_path}."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "minLength": 1,
                "pattern": SYSCTL_KEY_PATTERN,
                "description": "Kernel parameter, e.g. net.ipv4.ip_forward.",
            },
            "value": {
                "type": "string",
                "minLength": 1,
                "description": "The value to set, e.g. 1.",
            },
        },
        "required": ["key", "value"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
            "applied": {"type": "boolean"},
            "dropin_path": {"type": "string"},
        },
        "required": ["key", "value", "applied"],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("write", "sysctl", "linux"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": (
            "Call to correct a kernel parameter the first-boot config should "
            "have set -- enable net.ipv4.ip_forward on a router VM, raise a "
            "buffer limit -- both at runtime and persistently. Approval-gated: "
            "it parks for a human. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "key": "A single sysctl key, dotted or slashed.",
            "value": "The scalar value to set.",
        },
        "output_shape": (
            "{key, value, applied, dropin_path}; dropin_path is the persistent "
            "/etc/sysctl.d file written."
        ),
    },
)


_FIREWALL_LOAD_OP = LinuxOp(
    op_id="linux.firewall.load",
    handler_attr="firewall_load",
    summary="Validate then apply/replace an nft (or iptables) firewall ruleset.",
    description=(
        "Applies / replaces the host firewall ruleset: the ruleset is written to "
        "a temp file and VALIDATED before it is applied (nft -c -f, or "
        "iptables-restore --test), so a ruleset that fails validation never "
        "touches the live firewall. safety_level=dangerous, "
        "requires_approval=true: it parks for a human. Non-secret, so a human "
        "previews the ruleset via preview_operation before approving. Returns "
        "{applied, backend, validated}."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "ruleset": {
                "type": "string",
                "minLength": 1,
                "description": "The full ruleset text to load (nft or iptables-save format).",
            },
            "backend": {
                "type": "string",
                "enum": list(FIREWALL_BACKENDS),
                "description": "Firewall backend (default nft).",
            },
        },
        "required": ["ruleset"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "applied": {"type": "boolean"},
            "backend": {"type": "string"},
            "validated": {"type": "boolean"},
        },
        "required": ["applied", "backend", "validated"],
        "additionalProperties": True,
    },
    group_key="firewall",
    tags=("write", "firewall", "linux"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": (
            "Call to load the default-deny firewall ruleset the first-boot "
            "script was meant to apply, or to replace a broken one -- validated "
            "before it goes live. Approval-gated: it parks for a human, who sees "
            "the full ruleset in the preview. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "ruleset": "The full ruleset text.",
            "backend": "Optional; nft (default) or iptables.",
        },
        "output_shape": (
            "{applied, backend, validated}; validated=false with the live "
            "firewall unchanged when the ruleset failed the pre-apply check."
        ),
    },
)


#: The governed write ops. Composed onto ``LINUX_OPS`` after the read tier in
#: :func:`meho_backplane.connectors.linux.ops._linux_ops`.
WRITE_OPS: tuple[LinuxOp, ...] = (
    _FILE_WRITE_OP,
    _SERVICE_CONTROL_OP,
    _SCRIPT_RUN_OP,
    _SYSCTL_WRITE_OP,
    _FIREWALL_LOAD_OP,
)


register_linux_write_previews()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret-broker ``linux`` store: write a secret to a file on a Linux host (sink only).

Kind ``"linux"`` is a **write-only** store for ``secret.move``. The ref is::

    <target>/<absolute path>#<owner>

* ``<target>`` -- the name (or alias) of a ``linux-ssh`` target.
* ``<absolute path>`` -- where the file goes. ``linux:seat-host/etc/app/key#app``
  and ``linux:seat-host//etc/app/key#app`` name the same path.
* ``<owner>`` -- the Linux user who will own the file.

What the adapter does
=====================

It writes the value in one atomic step (temp file, then rename). The file gets
mode 0600 and the named owner. A missing parent directory is made with mode
0700 and the same owner.

Allowed paths: the write roots of ``linux.file.write``
(:data:`~meho_backplane.connectors.linux.ops_write.LINUX_WRITE_ROOTS`), or a
place inside the home directory of ``<owner>``. MEHO reads that home directory
from the host (``getent passwd``) in the same root script that writes the file.

Two checks run before MEHO opens any SSH connection: the path is absolute,
has no ``..`` and no control character, and it is not in a system tree that
can never hold a home directory (``/proc``, ``/sys``, ``/dev``, ``/boot``,
``/bin``, ``/sbin``, ``/lib*``, ``/tmp``, and ``/usr`` and ``/var`` outside the
write roots). The root script then resolves the real path (symbolic links
included) and refuses it unless it is still under a write root or the owner's
home. So a link inside a home directory cannot lead the write elsewhere.

How the value travels
=====================

The value is base64-encoded into the root script. The script goes to the host
on the SSH channel's standard input, through the same sudo helper as
``linux.file.write`` (:mod:`meho_backplane.connectors.linux._sudo`). The remote
command line holds only the script's byte count. The adapter's log events and
every error text hold only the target name, the path and the owner. An error
names a marker from the script, never the host's error output.

Reading a secret from a ``linux:`` ref is not supported. It fails with an error
that names no value.
"""

from __future__ import annotations

import base64
import posixpath
import re
import shlex
from typing import TYPE_CHECKING, Any, cast

import structlog

from meho_backplane.connectors.linux._sudo import run_remote_bash_with_sudo
from meho_backplane.connectors.linux.connector import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import (
    LINUX_WRITE_ROOTS,
    _resolve_sudo_password,
)
from meho_backplane.connectors.secret.endpoints import SecretMaterial, register_secret_endpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.linux.connector import Target
    from meho_backplane.connectors.secret.endpoints import SecretEndpoint

__all__ = [
    "LinuxSecretRefError",
    "LinuxSecretSinkEndpoint",
    "LinuxSecretSinkError",
    "build_linux_secret_endpoint",
    "build_secret_sink_script",
    "confine_secret_sink_path",
    "parse_linux_secret_ref",
]

_log = structlog.get_logger(__name__)

#: Linux user name: lower-case letters, digits, ``_`` and ``-``, as ``useradd``
#: allows by default. It goes into a shell script, so the set is narrow.
_OWNER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

#: System trees that can never hold a home directory. A path here is refused
#: before any SSH connection, unless a write root (``/usr/local``,
#: ``/var/lib``) covers it first.
_NEVER_ROOTS: tuple[str, ...] = (
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/usr",
    "/var",
    "/tmp",
)

_SINK_OK = "===MEHO_SINK_OK==="
_SINK_ERR_PREFIX = "===MEHO_SINK_ERR:"
_ERR_MARKER = re.compile(r"===MEHO_SINK_ERR:([a-z-]+)===")


class LinuxSecretRefError(ValueError):
    """A ``linux:`` ref is malformed or names a path that is not allowed.

    The message names the ref's parts (target, path, owner), never a value.
    """


class LinuxSecretSinkError(RuntimeError):
    """The write on the host did not finish.

    The message holds a short reason from the script, never the host's error
    output and never the value.
    """


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def confine_secret_sink_path(requested: str) -> str:
    """Return the normalised *requested* path, or raise :class:`LinuxSecretRefError`.

    Pure check, no SSH. Refuses a relative path, ``..``, a control character and
    a system tree that can never hold a home directory. A path under a write
    root passes here. Any other absolute path passes only this lexical check;
    the root script then proves it is inside the owner's home.
    """
    if not isinstance(requested, str) or not requested.startswith("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must be absolute")
    if any(ch in requested for ch in ("\x00", "\n", "\r", "\\")):
        raise LinuxSecretRefError("linux secret path contains a control character")
    if ".." in requested.split("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must not contain '..'")
    if requested.endswith("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must name a file")
    path = posixpath.normpath(requested)
    if path.startswith("//"):  # normpath keeps a leading double slash
        path = "/" + path.lstrip("/")
    if path == "/":
        raise LinuxSecretRefError(f"linux secret path {requested!r} must name a file")
    if any(_is_under(path, root) for root in LINUX_WRITE_ROOTS):
        return path
    if any(_is_under(path, root) for root in _NEVER_ROOTS):
        raise LinuxSecretRefError(
            f"linux secret path {path!r} is outside the allowed places: "
            f"{list(LINUX_WRITE_ROOTS)!r} or the home directory of the owner"
        )
    return path


def parse_linux_secret_ref(ref: str) -> tuple[str, str, str]:
    """Split ``"<target>/<absolute path>#<owner>"`` into ``(target, path, owner)``.

    Split on the **last** ``#`` for the owner and on the **first** ``/`` for the
    target. The path is confined by :func:`confine_secret_sink_path`.
    """
    address, sep, owner = ref.rpartition("#")
    owner = owner.strip()
    if not sep or not address:
        raise LinuxSecretRefError(
            f"malformed linux secret ref {ref!r}: expected "
            "'<target>/<absolute path>#<owner>' (e.g. 'linux:seat-host/etc/app/key#app')"
        )
    if not _OWNER_PATTERN.match(owner):
        raise LinuxSecretRefError(
            f"linux secret ref {ref!r} has an invalid owner {owner!r}: "
            "expected a Linux user name such as 'agent'"
        )
    target_name, slash, rest = address.partition("/")
    target_name = target_name.strip()
    if not slash or not target_name or not rest.strip("/"):
        raise LinuxSecretRefError(
            f"malformed linux secret ref {ref!r}: expected a '<target>/<absolute path>' "
            "address before '#<owner>'"
        )
    path = confine_secret_sink_path("/" + rest.lstrip("/"))
    return target_name, path, owner


def build_secret_sink_script(path: str, owner: str, value: bytes) -> str:
    """Render the root bash script that writes *value* to *path* for *owner*.

    *path* and *owner* are already checked by :func:`parse_linux_secret_ref`;
    both are ``shlex.quote``d anyway. The value is base64-carried. The script
    stops with a marker and a non-zero exit when the owner is unknown, when the
    resolved path is outside the allowed places, or when a step fails. It prints
    :data:`_SINK_OK` at the end.
    """
    q_path = shlex.quote(path)
    q_owner = shlex.quote(owner)
    q_b64 = shlex.quote(base64.b64encode(value).decode("ascii"))
    roots = " ".join(shlex.quote(r) for r in LINUX_WRITE_ROOTS)
    err = _SINK_ERR_PREFIX
    lines = [
        "set -u",
        "umask 077",
        f"path={q_path}",
        f"owner={q_owner}",
        f'grp="$(id -gn -- "$owner" 2>/dev/null)" || {{ echo "{err}owner===" >&2; exit 5; }}',
        'home="$(getent passwd "$owner" | cut -d: -f6)"',
        # Resolve links in the existing part of the path, then check the result.
        f'real="$(realpath -m -- "$path")" || {{ echo "{err}path===" >&2; exit 6; }}',
        "ok=0",
        f"for r in {roots}; do",
        '  case "$real" in "$r"/*) ok=1;; esac',
        "done",
        'if [ -n "$home" ] && [ "$home" != / ]; then',
        '  realhome="$(realpath -m -- "$home")"',
        '  case "$real" in "$realhome"/*) ok=1;; esac',
        "fi",
        f'[ "$ok" = 1 ] || {{ echo "{err}place===" >&2; exit 6; }}',
        'dir="$(dirname -- "$real")"',
        # Make a missing parent directory with mode 0700 and the owner.
        'mk() { [ -d "$1" ] && return 0; mk "$(dirname -- "$1")" || return 1; '
        'install -d -m 0700 -o "$owner" -g "$grp" -- "$1"; }',
        f'mk "$dir" || {{ echo "{err}mkdir===" >&2; exit 4; }}',
        f'tmp="$(mktemp "$dir/.meho-sink.XXXXXX")" || {{ echo "{err}mktemp===" >&2; exit 4; }}',
        f'if ! printf %s {q_b64} | base64 -d > "$tmp"; then '
        f'rm -f "$tmp"; echo "{err}decode===" >&2; exit 4; fi',
        'if ! chmod 0600 "$tmp" || ! chown "$owner:$grp" "$tmp"; then '
        f'rm -f "$tmp"; echo "{err}owner-mode===" >&2; exit 4; fi',
        f'if ! mv -f -T -- "$tmp" "$real"; then rm -f "$tmp"; echo "{err}mv===" >&2; exit 4; fi',
        f'echo "{_SINK_OK}"',
    ]
    return "\n".join(lines) + "\n"


class LinuxSecretSinkEndpoint:
    """A ``linux`` **sink** addressing one file on a Linux host.

    Constructed per move from the parsed ``ref``. Sink only:
    :meth:`read_secret` raises; :meth:`write_secret` writes the file.
    """

    def __init__(self, ref: str) -> None:
        self._target_name, self._path, self._owner = parse_linux_secret_ref(ref)

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Unsupported -- a ``linux:`` ref is a move sink, not a source."""
        raise NotImplementedError(
            "linux is a write-only secret store: a secret cannot be read from a "
            "'linux:' ref, so it cannot be a move source"
        )

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Write *material* to the file on the host, mode 0600, owned by the owner."""
        connector = get_or_create_connector_instance(LinuxSshConnector)
        if not isinstance(connector, LinuxSshConnector):  # pragma: no cover -- registry invariant
            raise TypeError(
                "secret broker linux sink resolved a non-LinuxSshConnector instance; "
                "the connector instance cache is misconfigured"
            )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        target = cast("Target", row)

        sudo_password = await _resolve_sudo_password(connector, target, operator)
        script = build_secret_sink_script(self._path, self._owner, material.value)
        _log.debug(
            "secret_broker.linux.write",
            target=self._target_name,
            path=self._path,
            owner=self._owner,
        )
        proc = await run_remote_bash_with_sudo(
            connector, target, script, operator=operator, sudo_password=sudo_password
        )
        _check_result(proc, self._target_name, self._path)


def _check_result(proc: Any, target_name: str, path: str) -> None:
    """Raise :class:`LinuxSecretSinkError` unless the script reported success."""
    stdout = proc.stdout if isinstance(getattr(proc, "stdout", None), str) else ""
    stderr = proc.stderr if isinstance(getattr(proc, "stderr", None), str) else ""
    if _SINK_OK in stdout and getattr(proc, "exit_status", None) in (0, None):
        return
    match = _ERR_MARKER.search(stderr)
    reason = match.group(1) if match else "unknown"
    raise LinuxSecretSinkError(
        f"linux secret sink: the write of {path!r} on target {target_name!r} "
        f"did not finish (reason: {reason})"
    )


def build_linux_secret_endpoint(ref: str) -> SecretEndpoint:
    """Registered factory for kind ``"linux"``."""
    return LinuxSecretSinkEndpoint(ref)


# Registered under kind ``"linux"`` when the ``connectors/secret`` package is
# imported, like the ``keycloak`` and ``vault`` kinds.
register_secret_endpoint("linux", build_linux_secret_endpoint)

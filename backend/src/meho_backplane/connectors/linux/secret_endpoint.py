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
place inside the home directory of ``<owner>``.

Before MEHO opens any SSH connection, it checks that the path is absolute,
has no ``..`` and no control character, and is not in a system folder that can
never hold a home directory (``/proc``, ``/sys``, ``/dev``, ``/boot``, ``/bin``,
``/sbin``, ``/lib*``, ``/tmp``, and ``/usr`` outside the write roots). A home
under ``/var`` (for example ``/var/www``) is allowed. For a path outside the
write roots, MEHO then reads the owner's home from the host (``getent passwd``)
with a short script that carries no secret, and refuses a path outside that
home before the secret is sent. The root script that writes the file checks
the path again, as the caller wrote it, without resolving links.

Root never follows a link that the owner of the file could have made:

- A path inside the owner's home is written by the owner
  (``runuser -u <owner>``). The folders, the temp file and the rename are all
  made as the owner. Root does not resolve or follow links there.
- A path under a write root is refused (reason ``link``) when it has a
  symbolic link anywhere in it.
- Under a write root, root writes only when every folder from the write root
  down belongs to root and is not writable by group or others. When the parent
  folder belongs to the owner, or root has just made it for the owner, the
  write runs as the owner. Any other folder is refused.

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
    "build_home_lookup_script",
    "build_linux_secret_endpoint",
    "build_secret_sink_script",
    "confine_secret_sink_path",
    "parse_linux_secret_ref",
]

_log = structlog.get_logger(__name__)

#: Linux user name: lower-case letters, digits, ``_`` and ``-``, as ``useradd``
#: allows by default. It goes into a shell script, so the set is narrow.
_OWNER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

#: Runs as the owner (never as root) for a path inside the owner's home. The
#: value comes on standard input; ``$1`` is the target path. Every folder,
#: the temp file and the rename are made by the owner.
_OWNER_WRITE_SCRIPT = (
    'set -u; umask 077; real=$1; dir=$(dirname -- "$real"); '
    'mkdir -p -m 0700 -- "$dir" || exit 4; '
    'tmp=$(mktemp "$dir/.meho-sink.XXXXXX") || exit 4; '
    'if ! base64 -d > "$tmp"; then rm -f "$tmp"; exit 4; fi; '
    'if ! chmod 0600 "$tmp"; then rm -f "$tmp"; exit 4; fi; '
    'if ! mv -f -T -- "$tmp" "$real"; then rm -f "$tmp"; exit 4; fi'
)

#: Shell helpers for the write-root branch. ``ownedby DIR UID``: *DIR* is a real
#: folder (not a link) that belongs to *UID* and is not writable by group or
#: others. ``safe DIR``: that holds for *DIR* and every folder above it up to
#: ``$root``, for the user that runs the script (``$me``).
_FOLDER_CHECKS = (
    'ownedby() { [ -d "$1" ] && [ ! -L "$1" ] || return 1; '
    'set -- "$2" $(stat -c \'%u %a\' -- "$1") || return 1; '
    '[ "$1" = "$2" ] && (( (8#$3 & 8#022) == 0 )); }\n'
    'safe() { local c=$1; while :; do ownedby "$c" "$me" || return 1; '
    '[ "$c" = "$root" ] && return 0; c="$(dirname -- "$c")"; done; }'
)

_SINK_OK = "===MEHO_SINK_OK==="
_SINK_ERR_PREFIX = "===MEHO_SINK_ERR:"
_ERR_MARKER = re.compile(r"===MEHO_SINK_ERR:([a-z-]+)===")
_HOME_MARKER = re.compile(r"^===MEHO_SINK_HOME:(.*)===$", re.MULTILINE)

#: Write-root branch: pick who writes. Sets ``as_owner=1`` when the write must
#: run as the owner, and refuses (``place``) a folder that is neither root-only
#: nor a folder of the owner inside root-only folders.
_CHOOSE_WRITER = "\n".join(
    (
        'base="$dir"; missing=()',
        'while [ ! -e "$base" ] && [ ! -L "$base" ]; do',
        '  missing=("$base" ${missing[@]+"${missing[@]}"}); base="$(dirname -- "$base")"',
        "done",
        "as_owner=0",
        'if safe "$base"; then',
        # New folders are made by root inside root-only folders. Then they go
        # to the owner from the deepest one up, so each one changes hands while
        # its parent still belongs to root.
        '  for m in ${missing[@]+"${missing[@]}"}; do',
        '    mkdir -m 0700 -- "$m" || { echo "===MEHO_SINK_ERR:mkdir===" >&2; exit 4; }',
        "  done",
        "  for (( i=${#missing[@]}-1; i>=0; i-- )); do",
        '    chown -h "$owner:$grp" -- "${missing[$i]}" '
        '|| { echo "===MEHO_SINK_ERR:mkdir===" >&2; exit 4; }',
        "  done",
        '  [ "${#missing[@]}" -gt 0 ] && as_owner=1',
        "else",
        # Go up through the folders of the owner (an earlier run may have
        # made several). The first folder above them must be root-only.
        '  top="$base"',
        '  while [ "$top" != "$root" ] && ownedby "$top" "$ouid"; do',
        '    top="$(dirname -- "$top")"',
        "  done",
        '  [ "$top" != "$base" ] && safe "$top" '
        '|| { echo "===MEHO_SINK_ERR:place===" >&2; exit 6; }',
        "  as_owner=1",
        "fi",
    )
)


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


#: System folders that can never hold a home directory. A path here is refused
#: before any SSH connection, unless a write root (``/usr/local``)
#: covers it first.
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
    "/tmp",
)


def _under_write_root(path: str) -> bool:
    return any(_is_under(path, root) for root in LINUX_WRITE_ROOTS)


def confine_secret_sink_path(requested: str) -> str:
    """Return the normalised *requested* path, or raise :class:`LinuxSecretRefError`.

    Pure check, no SSH. Refuses a relative path, ``..``, a control character
    and a system folder that can never hold a home directory. A path under a
    write root passes. Any other path passes only this check: before the secret
    is sent, :meth:`LinuxSecretSinkEndpoint.write_secret` reads the owner's home
    from the host and refuses a path outside it.
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
    if _under_write_root(path):
        return path
    if any(_is_under(path, root) for root in _NEVER_ROOTS):
        raise LinuxSecretRefError(
            f"linux secret path {path!r} is outside the allowed places: "
            f"the write roots {list(LINUX_WRITE_ROOTS)!r} or the owner's home"
        )
    return path


def build_home_lookup_script(owner: str) -> str:
    """Render the script that prints the home of *owner*. It carries no secret.

    It prints the home as ``getent passwd`` gives it and with links resolved,
    each on a :data:`_HOME_MARKER` line, or stops with the ``owner`` marker.
    """
    err = _SINK_ERR_PREFIX
    return (
        f"set -u\nowner={shlex.quote(owner)}\n"
        'home="$(getent passwd "$owner" | cut -d: -f6)"\n'
        f'[ -n "$home" ] || {{ echo "{err}owner===" >&2; exit 5; }}\n'
        'printf \'===MEHO_SINK_HOME:%s===\\n\' "$home" "$(realpath -m -- "$home")"\n'
    )


def _homes_from(proc: Any, target_name: str) -> tuple[str, ...]:
    """The owner's home folders from the lookup's output, or raise."""
    stdout = proc.stdout if isinstance(getattr(proc, "stdout", None), str) else ""
    homes = tuple(h for h in _HOME_MARKER.findall(stdout) if h.startswith("/") and h != "/")
    if homes and getattr(proc, "exit_status", None) in (0, None):
        return homes
    stderr = proc.stderr if isinstance(getattr(proc, "stderr", None), str) else ""
    match = _ERR_MARKER.search(stderr)
    raise LinuxSecretSinkError(
        f"linux secret sink: the home lookup on target {target_name!r} did not finish "
        f"(reason: {match.group(1) if match else 'home'})"
    )


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


def build_secret_sink_script(
    path: str, owner: str, value: bytes, *, roots: tuple[str, ...] = LINUX_WRITE_ROOTS
) -> str:
    """Render the root bash script that writes *value* to *path* for *owner*.

    *path* and *owner* are already checked by :func:`parse_linux_secret_ref`;
    both are ``shlex.quote``d anyway. The value is base64-carried. The script
    stops with a marker and a non-zero exit when the owner is unknown, when the
    path is outside the allowed places, when a path under a write root has a
    symbolic link in it, or when a step fails. It prints
    :data:`_SINK_OK` at the end. *roots* is the list of write roots; tests set
    it to a temporary folder.
    """
    q_path = shlex.quote(path)
    q_owner = shlex.quote(owner)
    q_b64 = shlex.quote(base64.b64encode(value).decode("ascii"))
    q_inner = shlex.quote(_OWNER_WRITE_SCRIPT)
    q_roots = " ".join(shlex.quote(r) for r in roots)
    err = _SINK_ERR_PREFIX
    lines = [
        "set -u",
        "umask 077",
        f"path={q_path}",
        f"owner={q_owner}",
        f'grp="$(id -gn -- "$owner" 2>/dev/null)" || {{ echo "{err}owner===" >&2; exit 5; }}',
        'home="$(getent passwd "$owner" | cut -d: -f6)"',
        # Decide on the path as the caller gave it. Root never resolves or
        # follows a link for a path inside the owner's home: the owner could
        # have made that link.
        'realhome=""',
        'if [ -n "$home" ] && [ "$home" != / ]; then',
        '  realhome="$(realpath -m -- "$home")"',
        '  case "$path" in "$home"/*|"$realhome"/*)',
        f'    printf %s {q_b64} | runuser -u "$owner" -- bash -c {q_inner} meho-sink "$path" '
        f'|| {{ echo "{err}write===" >&2; exit 4; }}',
        f'    echo "{_SINK_OK}"; exit 0;;',
        "  esac",
        "fi",
        "ok=0",
        f"for r in {q_roots}; do",
        '  case "$path" in "$r"/*) ok=1;; esac',
        "done",
        f'[ "$ok" = 1 ] || {{ echo "{err}place===" >&2; exit 6; }}',
        # Under a write root a link anywhere in the path is refused.
        f'[ "$(realpath -m -- "$path")" = "$path" ] || {{ echo "{err}link===" >&2; exit 6; }}',
        'dir="$(dirname -- "$path")"',
        # Under a write root. Root writes only in folders that no one else can
        # change: each folder from the write root down to "$dir" belongs to the
        # user that runs this script (root) and is not writable by group or
        # others. A folder of the owner gets the write as the owner. Any other
        # folder is refused.
        "me=$(id -u)",
        f'ouid="$(id -u -- "$owner")" || {{ echo "{err}owner===" >&2; exit 5; }}',
        _FOLDER_CHECKS,
        'root=""',
        f"for r in {q_roots}; do",
        '  case "$path" in "$r"/*) root=$r;; esac',
        "done",
        f'[ -d "$root" ] || {{ echo "{err}mkdir===" >&2; exit 4; }}',
        _CHOOSE_WRITER,
        'if [ "$as_owner" = 1 ]; then',
        f'  printf %s {q_b64} | runuser -u "$owner" -- bash -c {q_inner} meho-sink "$path" '
        f'|| {{ echo "{err}write===" >&2; exit 4; }}',
        f'  echo "{_SINK_OK}"; exit 0',
        "fi",
        # "$dir" and every folder above it up to the write root belong to root.
        f'tmp="$(mktemp "$dir/.meho-sink.XXXXXX")" || {{ echo "{err}mktemp===" >&2; exit 4; }}',
        f'if ! printf %s {q_b64} | base64 -d > "$tmp"; then '
        f'rm -f "$tmp"; echo "{err}decode===" >&2; exit 4; fi',
        'if ! chmod 0600 "$tmp" || ! chown "$owner:$grp" "$tmp"; then '
        f'rm -f "$tmp"; echo "{err}owner-mode===" >&2; exit 4; fi',
        f'if ! mv -f -T -- "$tmp" "$path"; then rm -f "$tmp"; echo "{err}mv===" >&2; exit 4; fi',
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
        if not _under_write_root(self._path):
            await self._check_home(connector, target, operator, sudo_password)
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

    async def _check_home(
        self, connector: LinuxSshConnector, target: Target, operator: Operator, sudo_password: str
    ) -> None:
        """Refuse a path outside the owner's home before the secret is sent.

        The home comes from the host (``getent passwd``). The lookup script
        carries no secret.
        """
        proc = await run_remote_bash_with_sudo(
            connector,
            target,
            build_home_lookup_script(self._owner),
            operator=operator,
            sudo_password=sudo_password,
        )
        homes = _homes_from(proc, self._target_name)
        if not any(self._path.startswith(h.rstrip("/") + "/") for h in homes):
            raise LinuxSecretRefError(
                f"linux secret path {self._path!r} is outside the allowed places: "
                f"the write roots {list(LINUX_WRITE_ROOTS)!r} or the home of "
                f"{self._owner!r} on target {self._target_name!r}"
            )


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

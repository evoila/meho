# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret-broker ``linux`` store: write a secret to a file on a Linux host (sink only).

Kind ``"linux"`` is a **write-only** store for ``secret.move``. The ref is::

    <target>/<absolute path>#<owner>

* ``<target>`` -- the name (or alias) of a ``linux-ssh`` target. A target of
  another kind is refused before MEHO reads any credential.
* ``<absolute path>`` -- where the file goes. It must be inside the home
  directory of ``<owner>``. ``linux:seat-host/home/app/.config/app/key#app``
  and ``linux:seat-host//home/app/.config/app/key#app`` name the same path.
* ``<owner>`` -- the Linux user who will own the file. Never ``root``, and
  never a user with user id 0.

What the adapter does
=====================

It writes the value in one atomic step (temp file, then rename), as the owner
(``runuser -u <owner>``). The file gets mode 0600. A missing parent directory
is made with mode 0700. Root never writes the file and never makes a folder,
so root never follows a link the owner could have made.

The only allowed place is the home directory of ``<owner>``. A list of
allowed places stays short and complete; a list of refused places never is.

Before MEHO opens any SSH connection, it checks that the path is absolute,
has no ``..`` and only printable characters (no backslash), and is not in a
place that is never a home (:data:`_NOT_A_HOME`: ``/etc``, ``/run``,
``/usr``, ``/var/lib``, ``/opt``, ``/srv`` and the system folders). Inside a
home it refuses a place the system runs or reads for logins
(:data:`_SYSTEM_RUNS`): a ``.ssh`` folder, ``.config/systemd``,
``.config/autostart``, SSH key files and shell start files. A home under
``/var`` (for example ``/var/www``) is allowed.

MEHO then reads the owner's home from the host (``getent passwd``) with a
short script that carries no secret. That script refuses an owner with user
id 0. MEHO refuses a path outside the home before the secret is sent. The
script that writes the file checks the owner and the home again. Both scripts
first check that ``realpath -m`` works on the host, and stop with the reason
``realpath`` when it does not.

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
from meho_backplane.connectors.linux.ops_write import _resolve_sudo_password
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

_SINK_OK = "===MEHO_SINK_OK==="
_SINK_ERR_PREFIX = "===MEHO_SINK_ERR:"
#: Both scripts start with this check. Without a working ``realpath -m`` the
#: home check would compare against an empty string.
_REALPATH_CHECK = (
    f'realpath -m -- / >/dev/null 2>&1 || {{ echo "{_SINK_ERR_PREFIX}realpath===" >&2; exit 7; }}'
)
#: Both scripts check the owner: a known user whose user id is not 0.
_OWNER_CHECK = (
    'ouid="$(id -u -- "$owner" 2>/dev/null)" '
    f'|| {{ echo "{_SINK_ERR_PREFIX}owner===" >&2; exit 5; }}\n'
    f'[ "$ouid" != 0 ] || {{ echo "{_SINK_ERR_PREFIX}root-owner===" >&2; exit 5; }}'
)
_ERR_MARKER = re.compile(r"===MEHO_SINK_ERR:([a-z-]+)===")
_HOME_MARKER = re.compile(r"^===MEHO_SINK_HOME:(.*)===$", re.MULTILINE)


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


#: Places that are never a home. A path here is refused before any SSH
#: connection. The store writes only inside the owner's home.
_NOT_A_HOME: tuple[str, ...] = (
    "/etc",
    "/run",
    "/usr",
    "/var/lib",
    "/opt",
    "/srv",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/tmp",
)


#: Places inside a home that the system runs or reads for logins. A secret
#: written here could become a command that runs or a login key, and the
#: approval card shows only the path, not the content. This is the one list:
#: each entry is checked before any SSH connection.
#:
#: * ``folders`` -- folder names, as parts that follow each other anywhere in
#:   the path: SSH (``.ssh``), user services (``.config/systemd``) and
#:   programs started at login (``.config/autostart``).
#: * ``names`` -- file names refused in any folder: SSH login keys, shell
#:   start and end files, login settings and mail forwarding.
_SYSTEM_RUNS: dict[str, tuple[tuple[str, ...], ...]] = {
    "folders": ((".ssh",), (".config", "systemd"), (".config", "autostart")),
    "names": (
        ("authorized_keys",),
        ("authorized_keys2",),
        (".bashrc",),
        (".bash_profile",),
        (".bash_login",),
        (".bash_logout",),
        (".profile",),
        (".zshrc",),
        (".zshenv",),
        (".zprofile",),
        (".zlogin",),
        (".zlogout",),
        (".pam_environment",),
        (".forward",),
        (".rhosts",),
        (".shosts",),
        (".k5login",),
    ),
}


def _system_runs(path: str) -> bool:
    """``True`` when *path* (normalised, absolute) is in :data:`_SYSTEM_RUNS`."""
    parts = path.split("/")
    if (parts[-1],) in _SYSTEM_RUNS["names"]:
        return True
    return any(
        tuple(parts[i : i + len(folder)]) == folder
        for folder in _SYSTEM_RUNS["folders"]
        for i in range(len(parts))
    )


def confine_secret_sink_path(requested: str) -> str:
    """Return the normalised *requested* path, or raise :class:`LinuxSecretRefError`.

    Pure check, no SSH. Refuses a relative path, ``..``, a character that is
    not printable or a backslash, a place in :data:`_NOT_A_HOME` and a place in
    :data:`_SYSTEM_RUNS`. Any other path passes only this check: before the
    secret is sent, :meth:`LinuxSecretSinkEndpoint.write_secret` reads the
    owner's home from the host and refuses a path outside it.
    """
    if not isinstance(requested, str) or not requested.startswith("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must be absolute")
    if not requested.isprintable() or "\\" in requested:
        raise LinuxSecretRefError(
            "linux secret path contains a character that is not printable or a backslash"
        )
    if ".." in requested.split("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must not contain '..'")
    if requested.endswith("/"):
        raise LinuxSecretRefError(f"linux secret path {requested!r} must name a file")
    path = posixpath.normpath(requested)
    if path.startswith("//"):  # normpath keeps a leading double slash
        path = "/" + path.lstrip("/")
    if path == "/":
        raise LinuxSecretRefError(f"linux secret path {requested!r} must name a file")
    if any(_is_under(path, place) for place in _NOT_A_HOME):
        raise LinuxSecretRefError(
            f"linux secret path {path!r} is outside the owner's home; "
            "the linux store writes only inside the home of the owner"
        )
    if _system_runs(path):
        raise LinuxSecretRefError(
            f"linux secret path {path!r} is a place the system runs or reads for "
            "logins; a secret is never written there"
        )
    return path


def build_home_lookup_script(owner: str) -> str:
    """Render the script that prints the home of *owner*. It carries no secret.

    It prints the home as ``getent passwd`` gives it and with links resolved,
    each on a :data:`_HOME_MARKER` line. It stops with the ``owner`` marker
    for an unknown owner, and with ``root-owner`` for an owner with user id 0.
    """
    err = _SINK_ERR_PREFIX
    return (
        f"set -u\n{_REALPATH_CHECK}\nowner={shlex.quote(owner)}\n"
        f"{_OWNER_CHECK}\n"
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
            "'<target>/<absolute path>#<owner>' (e.g. 'seat-host/home/app/.config/app/key#app')"
        )
    if not _OWNER_PATTERN.match(owner):
        raise LinuxSecretRefError(
            f"linux secret ref {ref!r} has an invalid owner {owner!r}: "
            "expected a Linux user name such as 'agent'"
        )
    if owner == "root":
        raise LinuxSecretRefError(
            f"linux secret ref {ref!r} names the owner 'root'; the linux store writes "
            "only as a user that is not root, inside that user's home"
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
    """Render the bash script that writes *value* to *path* as *owner*.

    It runs with sudo, but root only checks: the owner is known and not user
    id 0, and *path* is inside the owner's home (as the caller wrote it, and
    with the home's links resolved). Then the owner makes the folders and the
    file (``runuser -u``). *path* and *owner* are already checked by
    :func:`parse_linux_secret_ref`; both are ``shlex.quote``d anyway. The value
    is base64-carried. The script stops with a marker and a non-zero exit when
    a check or the write fails. It prints :data:`_SINK_OK` at the end.
    """
    q_path = shlex.quote(path)
    q_owner = shlex.quote(owner)
    q_b64 = shlex.quote(base64.b64encode(value).decode("ascii"))
    q_inner = shlex.quote(_OWNER_WRITE_SCRIPT)
    err = _SINK_ERR_PREFIX
    lines = [
        "set -u",
        "umask 077",
        _REALPATH_CHECK,
        f"path={q_path}",
        f"owner={q_owner}",
        _OWNER_CHECK,
        'home="$(getent passwd "$owner" | cut -d: -f6)"',
        # Decide on the path as the caller gave it. Root never resolves or
        # follows a link inside the home: the owner could have made it.
        "inhome=0",
        'if [ -n "$home" ] && [ "$home" != / ]; then',
        '  case "$path" in "$home"/*) inhome=1;; esac',
        f'  realhome="$(realpath -m -- "$home")" || {{ echo "{err}realpath===" >&2; exit 7; }}',
        # An empty "$realhome" would make the pattern "/*", which every
        # absolute path matches.
        '  if [ -n "$realhome" ] && [ "$realhome" != / ]; then',
        '    case "$path" in "$realhome"/*) inhome=1;; esac',
        "  fi",
        "fi",
        f'[ "$inhome" = 1 ] || {{ echo "{err}place===" >&2; exit 6; }}',
        f'printf %s {q_b64} | runuser -u "$owner" -- bash -c {q_inner} meho-sink "$path" '
        f'|| {{ echo "{err}write===" >&2; exit 4; }}',
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
        """Write *material* to the file on the host as the owner, mode 0600."""
        connector = get_or_create_connector_instance(LinuxSshConnector)
        if not isinstance(connector, LinuxSshConnector):  # pragma: no cover -- registry invariant
            raise TypeError(
                "secret broker linux sink resolved a non-LinuxSshConnector instance; "
                "the connector instance cache is misconfigured"
            )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        # Only a Linux SSH target gets its credentials read and the script.
        product = getattr(row, "product", None)
        if product != LinuxSshConnector.product:
            raise LinuxSecretRefError(
                f"linux secret ref names target {self._target_name!r} of kind {product!r}; "
                f"the linux store writes only to a {LinuxSshConnector.product!r} target"
            )
        target = cast("Target", row)

        sudo_password = await _resolve_sudo_password(connector, target, operator)
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
                f"linux secret path {self._path!r} is outside the home of "
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

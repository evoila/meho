# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`LinuxSshConnector` (T1 read floor, #3360).

The keystone read surface for the generic ``linux-ssh`` connector
(Initiative #3359, branch (b) of the guest-ops fork #3100): the identity
canary plus a floor of six ``safe`` read verbs that turn "the run
*declared* the host ready" into "the run *observed* it ready".

This module ships the shared scaffolding every op module reuses:

* :class:`LinuxOp` -- the frozen op-metadata dataclass, mirroring
  :class:`~meho_backplane.connectors.rke2.ops.Rke2Op`. Its fields are the
  keyword arguments
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`
  accepts, so :meth:`LinuxSshConnector.register_operations` can splat one
  row into the helper without per-op boilerplate.
* :data:`SSH_TRANSPORT_NOTE` -- the transport reminder copied verbatim
  into every op's ``when_to_use``.
* :func:`normalise_json_rows` -- the ``{rows, total}`` envelope helper the
  set-shaped verbs (``log.tail`` / ``firewall.show`` / ``mount.list``)
  return so the shared :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
  detects the single-real-list collection and spills over threshold.
* Path confinement (:func:`ensure_path_under_root` / :func:`confine_read_path`
  / :data:`LINUX_READ_ROOTS` / :data:`LINUX_READ_FILES` /
  :data:`LINUX_READ_DENY` / :class:`PathConfinementError`) -- the bind9
  ``ensure_path_under_root`` mold, generalised to an allow-list of read
  roots plus named files, and a list of key and password files that are
  never read, for the operator-named paths ``file.read`` and ``log.tail``
  accept. The read handlers run the same check again on the real path
  the host reports.
* :data:`LINUX_WHEN_TO_USE_BY_GROUP` -- the curated per-group blurbs;
  :meth:`LinuxSshConnector.register_operations` fails closed
  (:class:`ValueError`) if a declared ``group_key`` lacks an entry here.
* :data:`LINUX_OPS` -- the merged registration tuple.

The identity canary ``linux.about`` (its handler wraps
:meth:`LinuxSshConnector.fingerprint`) lives here; the six read verbs and
their handlers live in the per-domain op modules composed onto
:data:`LINUX_OPS` by :func:`_linux_ops`.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "DEFAULT_FILE_READ_BYTES",
    "DEFAULT_TAIL_LINES",
    "LINUX_OPS",
    "LINUX_READ_DENY",
    "LINUX_READ_FILES",
    "LINUX_READ_ROOTS",
    "LINUX_WHEN_TO_USE_BY_GROUP",
    "MAX_FILE_READ_BYTES",
    "MAX_TAIL_LINES",
    "SSH_TRANSPORT_NOTE",
    "LinuxOp",
    "PathConfinementError",
    "confine_read_path",
    "ensure_path_under_root",
    "normalise_json_rows",
]


#: Canonical plain-SSH transport reminder copied verbatim into every op's
#: ``llm_instructions.when_to_use``. A generic Linux host exposes no MEHO
#: REST surface; the transport is plain SSH to the host OS over the shared
#: SSH adapter. The read floor never reads secret material into a parameter
#: and never mutates state.
SSH_TRANSPORT_NOTE: str = (
    "A generic Linux host exposes no MEHO REST API; the transport is plain "
    "SSH to the host OS over the shared SSH adapter, with the host's own "
    "per-target credential. Every read op here is safe and non-mutating."
)

#: Default and hard-cap byte budget for ``linux.file.read``. The op caps
#: the content itself (``head -c``) rather than relying on the JSONFlux
#: reducer, which only spills set-shaped (list) payloads -- a flat
#: ``{path, content, ...}`` scalar dict is never reduced, so an uncapped
#: read would ship an arbitrarily large blob into agent context.
DEFAULT_FILE_READ_BYTES: int = 65536
MAX_FILE_READ_BYTES: int = 1048576

#: Default and hard-cap line budget for ``linux.log.tail``.
DEFAULT_TAIL_LINES: int = 200
MAX_TAIL_LINES: int = 10000

#: The allow-list of absolute POSIX roots the operator-named read paths
#: (``file.read`` / ``log.tail``) are confined under. These are the
#: config / log / runtime-state trees a day-0 verification recipe reads --
#: the first-boot completion sentinel, the first-boot log, a declared
#: unit's drop-in -- never an arbitrary path. A path must equal or descend
#: one of these roots, both as typed and as the real path the host reports
#: after it follows every symlink.
LINUX_READ_ROOTS: tuple[str, ...] = (
    "/etc",
    "/var/log",
    "/var/lib",
    "/run",
)

#: Single files outside :data:`LINUX_READ_ROOTS` the read ops may also
#: read, mostly under ``/proc`` and ``/sys``. Only these files: the rest of
#: ``/proc`` and ``/sys`` holds per-process state (environment, memory,
#: open files) that is not a host check. ``/sys/class/dmi/id`` is a symlink
#: to ``/sys/devices/virtual/dmi/id``, so both forms are listed, and
#: ``/etc/os-release`` is a symlink to ``/usr/lib/os-release`` on most
#: systemd hosts. Kernel parameters have their own op,
#: ``linux.sysctl.read``.
LINUX_READ_FILES: frozenset[str] = frozenset(
    {
        "/usr/lib/os-release",
        "/proc/cmdline",
        "/proc/cpuinfo",
        "/proc/loadavg",
        "/proc/meminfo",
        "/proc/partitions",
        "/proc/uptime",
        "/proc/version",
        "/sys/devices/system/cpu/online",
        "/sys/firmware/efi",
        "/sys/class/dmi/id/product_name",
        "/sys/class/dmi/id/product_uuid",
        "/sys/class/dmi/id/sys_vendor",
        "/sys/devices/virtual/dmi/id/product_name",
        "/sys/devices/virtual/dmi/id/product_uuid",
        "/sys/devices/virtual/dmi/id/sys_vendor",
    }
)

#: Files the read ops never return, even under an allowed root: password
#: hashes, private keys, cluster tokens and kubeconfigs with client keys.
#: Each entry is an :func:`fnmatch.fnmatchcase` pattern on the full path
#: (``*`` also matches ``/``). The check runs on the typed path and on the
#: real path.
LINUX_READ_DENY: tuple[str, ...] = (
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/gshadow",
    "/etc/gshadow-",
    "/etc/security/opasswd",
    "/etc/krb5.keytab",
    "/etc/ssh/ssh_host_*_key",
    "/etc/ssl/private/*",
    "/etc/pki/tls/private/*",
    "/etc/letsencrypt/*",
    "/etc/kubernetes/*.conf",
    "/etc/kubernetes/pki/*",
    "/etc/rancher/*",
    "/etc/NetworkManager/system-connections/*",
    "/var/lib/kubelet/pki/*",
    "/var/lib/rancher/*/server/token",
    "/var/lib/rancher/*/server/node-token",
    "/var/lib/rancher/*/server/agent-token",
    "/var/lib/rancher/*/server/cred/*",
    "/var/lib/rancher/*/server/tls/*",
    "/var/lib/rancher/*/server/db/*",
    "/var/lib/rancher/*/agent/*.kubeconfig",
    "/var/lib/rancher/*/agent/*.key",
    "/run/secrets/*",
    # Key files by name, wherever they are.
    "*.key",
    "*.keytab",
    "*.p12",
    "*.pfx",
    "*/id_rsa",
    "*/id_dsa",
    "*/id_ecdsa",
    "*/id_ed25519",
)


class PathConfinementError(ValueError):
    """An operator-named read path resolved outside every allowed root.

    A :class:`ValueError` subclass so the dispatcher's handler-exception
    catch maps it to a ``connector_error``
    :class:`~meho_backplane.connectors.schemas.OperationResult`
    (``status="error"``) rather than letting the traversal attempt escape
    as an unhandled exception. Raised *before* any SSH command is
    constructed, so a rejected path never reaches the host.
    """


def ensure_path_under_root(requested: str, allowed_root: str) -> str:
    """Return the canonical absolute path of *requested* under *allowed_root*, or raise.

    The bind9 ``ensure_path_under_root`` lexical-confinement mold: the
    candidate is ``posixpath.normpath``-collapsed (so ``..`` / ``.``
    segments cannot escape) and must equal *allowed_root* or descend it
    (a trailing-slash sentinel stops ``/etc-evil`` matching ``/etc``).
    Control bytes are refused outright -- ``shlex.quote`` would survive a
    NUL / newline via shell-string rules in some shells, so they are
    rejected here before quoting. Raises :class:`PathConfinementError` on
    any violation.

    POSIX-only: the resolution is for the *remote* host's filesystem, so
    :mod:`posixpath` is used directly rather than :mod:`pathlib` (which
    would auto-detect the local platform).
    """
    if not allowed_root or not allowed_root.startswith("/"):
        raise PathConfinementError(
            f"allowed_root {allowed_root!r} must be an absolute POSIX path; refusing to filter"
        )
    if not isinstance(requested, str) or not requested.strip():
        raise PathConfinementError("requested path is empty")
    if any(ch in requested for ch in ("\x00", "\n", "\r")):
        raise PathConfinementError("requested path contains a control character; refusing")

    candidate = posixpath.normpath(posixpath.join(allowed_root, requested))
    canonical_root = posixpath.normpath(allowed_root)
    if candidate != canonical_root and not candidate.startswith(canonical_root + "/"):
        raise PathConfinementError(
            f"path {candidate!r} is outside the allowed read root {canonical_root!r}"
        )
    return candidate


def _is_denied(path: str) -> bool:
    """Return ``True`` when *path* matches an entry of :data:`LINUX_READ_DENY`."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in LINUX_READ_DENY)


def confine_read_path(requested: str, roots: tuple[str, ...] = LINUX_READ_ROOTS) -> str:
    """Return the canonical form of *requested* when the read ops may read it, or raise.

    The path must be absolute (a relative path would resolve under
    whichever root happened to be tried first, which is ambiguous). Its
    canonical form must land under one of *roots* (see
    :func:`ensure_path_under_root`) or be one of :data:`LINUX_READ_FILES`,
    and must not match :data:`LINUX_READ_DENY`. Raises
    :class:`PathConfinementError` otherwise.

    The check is lexical. The read handlers call it once on the typed path
    (before any SSH command) and once more on the real path the host
    reports, so a symlink cannot point the read somewhere else.
    """
    if not isinstance(requested, str) or not requested.startswith("/"):
        raise PathConfinementError(
            f"read path {requested!r} must be an absolute POSIX path (starting with '/')"
        )
    confined: str | None = None
    for root in roots:
        try:
            confined = ensure_path_under_root(requested, root)
            break
        except PathConfinementError:
            continue
    if confined is None:
        if any(ch in requested for ch in ("\x00", "\n", "\r")):
            raise PathConfinementError("requested path contains a control character; refusing")
        canonical = posixpath.normpath(requested)
        if canonical not in LINUX_READ_FILES:
            raise PathConfinementError(
                f"path {requested!r} is outside every allowed read root {list(roots)!r} "
                "and is not one of the named single files"
            )
        confined = canonical
    if _is_denied(confined):
        raise PathConfinementError(
            f"path {confined!r} holds keys, tokens or password hashes; "
            "linux.file.read and linux.log.tail do not return it"
        )
    return confined


def normalise_json_rows(rows: list[Any]) -> dict[str, Any]:
    """Return the ``{rows, total}`` envelope the set-shaped read verbs emit.

    Handlers hand back a plain list of rows (log lines, ruleset lines,
    mount/export entries); this wraps them in the canonical envelope the
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    detects as the single real list-valued collection (``total`` is a
    scalar sibling, preserved verbatim). The reducer spills to a
    ``result_query`` handle above the 50-row / 4 KB threshold; below it
    the envelope passes through unchanged.
    """
    materialised = list(rows)
    return {"rows": materialised, "total": len(materialised)}


@dataclass(frozen=True)
class LinuxOp:
    """Metadata for one Linux-host op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :meth:`LinuxSshConnector.register_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.linux.connector.LinuxSshConnector`
    that exposes the async handler.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The identity canary. ``linux.about`` wraps :meth:`LinuxSshConnector.fingerprint`.
_LINUX_ABOUT_OP = LinuxOp(
    op_id="linux.about",
    handler_attr="about",
    summary="Return the Linux host's vendor, product, distro version, and kernel release.",
    description=(
        "Connects to the host over SSH and runs a single fixed round-trip "
        "(``hostname`` + ``cat /etc/os-release`` + ``uname -r`` + "
        "init-system detection) to identify it. Returns a flat dict with "
        "the vendor (the distro ID family, e.g. ``debian``), product "
        "(always ``linux``), the distro ``VERSION_ID`` (or ``null`` when "
        "``/etc/os-release`` omits it), the kernel release, the "
        "``PRETTY_NAME``, the hostname, and the detected init system. No "
        "params; safe to call on any reachable host. Call this first to "
        "confirm the host is reachable via SSH before issuing the read ops."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "kernel": {"type": ["string", "null"]},
            "os_pretty": {"type": ["string", "null"]},
            "hostname": {"type": ["string", "null"]},
            "init_system": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "identity", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the Linux host behind "
            "a target -- which distro and version it runs, on which kernel, "
            "with which init system -- or to confirm the host is reachable "
            "via SSH before issuing the read ops. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the distro ``VERSION_ID`` "
            "(``null`` when absent), ``kernel`` the ``uname -r`` release, "
            "``os_pretty`` the ``/etc/os-release`` PRETTY_NAME."
        ),
    },
)


def _linux_ops() -> tuple[LinuxOp, ...]:
    """Return the merged registration tuple.

    Composition: ``linux.about`` (identity canary) + the per-domain read
    tiers -- ``FILE_OPS`` (``file.read`` / ``log.tail``), ``HOST_OPS``
    (``service.status`` / ``sysctl.read`` / ``dns.lookup``), ``FIREWALL_OPS``
    (``firewall.show``), ``STORAGE_OPS`` (``mount.list``) -- plus the T2
    governed ``WRITE_OPS`` (``file.write`` / ``service.control`` /
    ``script.run`` / ``sysctl.write`` / ``firewall.load``). Implemented as a
    function call rather than a module-level literal so the import order
    stays linear (this module defines :class:`LinuxOp` + the shared helpers
    first, then imports the per-domain op tuples from their siblings; the
    read tiers are imported before ``ops_write``, which imports validators
    from ``ops_host``), the rke2 mold.
    """
    from meho_backplane.connectors.linux.ops_file import FILE_OPS
    from meho_backplane.connectors.linux.ops_firewall import FIREWALL_OPS
    from meho_backplane.connectors.linux.ops_host import HOST_OPS
    from meho_backplane.connectors.linux.ops_storage import STORAGE_OPS
    from meho_backplane.connectors.linux.ops_write import WRITE_OPS

    return (
        _LINUX_ABOUT_OP,
        *FILE_OPS,
        *HOST_OPS,
        *FIREWALL_OPS,
        *STORAGE_OPS,
        *WRITE_OPS,
    )


#: Curated ``when_to_use`` strings per group key, indexed by
#: :meth:`LinuxSshConnector.register_operations`. Each entry covers a
#: ``group_key`` declared in :data:`LINUX_OPS`; the registration walk fails
#: closed with a :class:`ValueError` if a declared ``group_key`` lacks a
#: curated entry (the bind9 / rke2 precedent). Eight groups: the read
#: verbs' groups (``network`` carries ``dns.lookup`` alone) plus the ``exec``
#: group the T2 write verb ``script.run`` adds; ``system`` carries the
#: identity canary + ``sysctl.read`` + ``sysctl.write``, and ``file`` /
#: ``service`` / ``firewall`` each carry a read verb beside their governed
#: write verb.
LINUX_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "system": (
        "Use for host identity and kernel-parameter reads plus the "
        "kernel-parameter write: ``linux.about`` returns vendor / product / "
        "distro version / kernel / init system (call it first to confirm SSH "
        "reachability), ``linux.sysctl.read`` returns the live value of a "
        "single named kernel parameter (e.g. ``net.ipv4.ip_forward`` -- did "
        "the first-boot script actually enable IP forwarding?), and "
        "``linux.sysctl.write`` sets one (runtime + a persistent drop-in), "
        "approval-gated. " + SSH_TRANSPORT_NOTE
    ),
    "file": (
        "Use to read or write the CONTENT of an allow-listed config / log / "
        "sentinel file: ``linux.file.read`` ``head -c``-caps and returns the "
        "bytes of a path confined under the read-root allow-list (``/etc``, "
        "``/var/log``, ``/var/lib``, ``/run``, plus a few named ``/proc`` and "
        "``/sys`` files; key and password files are refused) -- the "
        "decisive day-0 signal is the first-boot completion sentinel a "
        "Tools-less appliance writes -- and ``linux.file.write`` atomically "
        "writes an allow-listed config file with a backup and an optional "
        "validate/rollback gate, approval-gated. " + SSH_TRANSPORT_NOTE
    ),
    "log": (
        "Use to tail the tail end of an allow-listed log file: "
        "``linux.log.tail`` runs ``tail -n <lines>`` over a path confined "
        "under the read-root allow-list and returns the lines as rows. The "
        "day-0 signal: tail the first-boot log to see WHY a "
        "``set -euo pipefail`` first-boot script aborted (a missing NIC, an "
        "unresolvable package mirror, a red config-validate). Large tails "
        "spill to a result handle paged with "
        "``result_query(handle_id, offset, limit)``. " + SSH_TRANSPORT_NOTE
    ),
    "service": (
        "Use to check or control a named systemd unit: "
        "``linux.service.status`` reports ``systemctl is-active`` / "
        "``is-enabled`` / the sub-state for one unit WITHOUT changing it (the "
        "day-0 signal: confirm each unit the first-boot config declared -- "
        "DNS, DHCP, NTP -- is actually active and enabled, not merely "
        "installed), and ``linux.service.control`` performs one recoverable "
        "action (start / stop / restart / reload / enable / disable). The "
        "control verb is ``caution`` and runs immediately -- it does not park "
        "for approval, because the action is reversible. " + SSH_TRANSPORT_NOTE
    ),
    "exec": (
        "Use to run an operator-declared remediation script on the host: "
        "``linux.script.run`` uploads a script to a temp path and executes it "
        "under a chosen interpreter (optionally under sudo), capturing stdout "
        "/ stderr / exit. The right op to re-run the aborted first-boot script "
        "or apply a multi-step fix. It is the intentional arbitrary-code "
        "surface -- a typed verb governed by approval (it parks for a human), "
        "NOT an interactive shell. " + SSH_TRANSPORT_NOTE
    ),
    "firewall": (
        "Use to inspect or load the host's firewall ruleset: "
        "``linux.firewall.show`` returns the ``nft list ruleset`` output (or "
        "``iptables-save`` on legacy hosts) as rows WITHOUT changing it (the "
        "day-0 signal: confirm the default-deny ruleset the first-boot script "
        "was meant to load is present), and ``linux.firewall.load`` "
        "validates then applies / replaces a ruleset (``nft -c -f`` before "
        "``nft -f``), approval-gated. Large rulesets spill to a result handle "
        "paged with ``result_query(handle_id, offset, limit)``. " + SSH_TRANSPORT_NOTE
    ),
    "storage": (
        "Use to inspect the host's mount table and NFS exports WITHOUT "
        "changing them: ``linux.mount.list`` returns each mount entry and "
        "each exported path as rows carrying a ``kind`` discriminator "
        "(``mount`` / ``export``). The day-0 signal: is the base NFS export "
        "live and is the expected mount present. Block-topology / capacity "
        "(``df`` / ``lsblk``) is a separate op. Large tables spill to a "
        "result handle paged with ``result_query(handle_id, offset, "
        "limit)``. " + SSH_TRANSPORT_NOTE
    ),
    "network": (
        "Use to check the host's own name lookup WITHOUT changing anything: "
        "``linux.dns.lookup`` runs ``getent ahosts <name>`` on the host and "
        "returns the addresses its resolver gives back, or ``found=false`` "
        "when the name cannot be found. It shows what programs on that host "
        "see, so it proves a resolver repair or explains why a pod cannot pull "
        "an image. For a lookup from MEHO's own side use ``net.dns_lookup``. "
        "Safe, so a sensor can run it on a schedule. " + SSH_TRANSPORT_NOTE
    ),
}


#: The ops :class:`LinuxSshConnector` registers at lifespan startup.
LINUX_OPS: tuple[LinuxOp, ...] = _linux_ops()

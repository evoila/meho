# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""LinuxSshConnector -- typed plain-SSH connector for generic Linux hosts.

The keystone (T1, #3360) of the ``linux-ssh`` connector family
(Initiative #3359) -- branch (b) of the guest-ops fork (#3100), the
governed surface for hosts VMware Tools cannot reach (Tools-less
appliances, non-VMware-hypervisor guests, bare metal). A generic Linux
host publishes no unified REST spec, so this is a hand-coded typed
connector against SSH, resolved by target fingerprint like any other; the
agent never sees typed vs generic.

This module ships:

* :class:`LinuxSshConnector`, subclass of :class:`SshConnector`, carrying
  the registry-v2 triple ``("linux", "1.x", "linux-ssh")``.
* :meth:`LinuxSshConnector.fingerprint` -- one fixed SSH round-trip
  reading ``hostname`` + ``/etc/os-release`` + ``uname -r`` + init-system
  detection. Returns ``vendor=<distro ID family>`` / ``product="linux"`` /
  ``version=<VERSION_ID>`` / ``build=<kernel release>``; ``extras`` carries
  ``hostname`` / ``os_pretty`` / ``kernel`` / ``init_system`` /
  ``distro_id``. Any transport / credential failure surfaces as
  ``reachable=False`` + ``extras["error"]`` (the #986 discipline).
* :meth:`LinuxSshConnector.probe` -- the five-reason reachability matrix
  (``tcp_unreachable`` / ``ssh_auth_failed`` / ``command_failed`` /
  ``os_release_unreadable`` / ``systemd_absent``).
* :meth:`LinuxSshConnector.about` -- operator-facing wrapper around
  :meth:`fingerprint` (the ``linux.about`` typed op).
* The per-op bound-method handler shims (``file_read`` / ``log_tail`` /
  ``service_status`` / ``sysctl_read`` / ``firewall_show`` / ``mount_list``)
  delegating to the per-domain op modules.
* :meth:`LinuxSshConnector.execute` -- the G0.6 dispatcher shim (same
  shape as :meth:`Rke2SshConnector.execute` / :meth:`Bind9Connector.execute`).

Auth uses the base :class:`SshConnector` ``_auth_config`` unchanged --
key-preferred, password-fallback -- resolving ``target.secret_ref`` (a
Vault KV-v2 path string) under the operator's identity. Because the Linux
host *is* the target (a 1:1 target-to-host mapping), "one credential per
target" is exactly right, avoiding the guest-ops one-cred-per-vCenter
limitation. The connector does **not** override ``_auth_config`` and does
**not** touch ``known_hosts``.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

import asyncssh
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.linux.ops import (
    LINUX_OPS,
    LINUX_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["LinuxSshConnector", "parse_fingerprint_output", "parse_os_release"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH
# adapter and the bind9 / rke2 siblings.
type Target = Any


# Section markers in the fixed fingerprint round-trip's output. Each
# printed by a literal ``echo`` (no operator input), so the parser can
# attribute the lines that follow to the right field.
_FP_HOSTNAME = "===MEHO_HOSTNAME==="
_FP_KERNEL = "===MEHO_KERNEL==="
_FP_INIT = "===MEHO_INIT==="
_FP_OSRELEASE = "===MEHO_OSRELEASE==="

#: The single fixed fingerprint round-trip. No operator value is
#: interpolated. Init detection prefers the ``/run/systemd/system`` marker
#: (present iff systemd is PID 1), falls back to a ``systemctl`` presence
#: check, then to PID 1's ``comm``.
_FINGERPRINT_CMD: str = (
    f"echo {_FP_HOSTNAME!r}; hostname 2>/dev/null; "
    f"echo {_FP_KERNEL!r}; uname -r 2>/dev/null; "
    f"echo {_FP_INIT!r}; "
    "if [ -d /run/systemd/system ]; then echo systemd; "
    "elif command -v systemctl >/dev/null 2>&1; then echo systemd; "
    "else (ps -p 1 -o comm= 2>/dev/null || echo unknown); fi; "
    f"echo {_FP_OSRELEASE!r}; cat /etc/os-release 2>/dev/null"
)

#: The probe's os-release read -- a distinct round-trip so a non-zero exit
#: (unreadable file) is a separate reason from a systemd-absent host.
_PROBE_OSRELEASE_CMD: str = "cat /etc/os-release"

#: The probe's systemd-presence check. Prints ``systemd`` when systemd is
#: PID 1 (or ``systemctl`` is on PATH), ``other`` otherwise.
_PROBE_SYSTEMD_CMD: str = (
    "if [ -d /run/systemd/system ] || command -v systemctl >/dev/null 2>&1; "
    "then echo systemd; else echo other; fi"
)

#: ``/etc/os-release`` line shape: ``KEY=VALUE`` with optional quoting.
_OS_RELEASE_LINE_RE: re.Pattern[str] = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")


def parse_os_release(content: str) -> dict[str, str]:
    """Parse ``/etc/os-release`` ``KEY=VALUE`` lines into a dict.

    Surrounding double or single quotes are stripped from values (the
    os-release spec allows either). Blank and comment lines are ignored.

    Examples
    --------

    >>> osr = parse_os_release('ID=ubuntu\\nVERSION_ID="22.04"\\nID_LIKE=debian\\n')
    >>> osr["ID"], osr["VERSION_ID"], osr["ID_LIKE"]
    ('ubuntu', '22.04', 'debian')
    """
    parsed: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _OS_RELEASE_LINE_RE.match(stripped)
        if match is None:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _derive_vendor(os_release: dict[str, str]) -> str:
    """Return the distro ID family: ``ID_LIKE``'s first token, else ``ID``.

    ``/etc/os-release`` ``ID_LIKE`` names the upstream family (Ubuntu →
    ``debian``, Rocky → ``rhel fedora``); its first token is the family
    the resolver keys on. A distro with no ``ID_LIKE`` (Debian, Fedora)
    is its own family, so ``ID`` is used. A host with neither falls back
    to ``linux``.
    """
    id_like = os_release.get("ID_LIKE", "").strip()
    if id_like:
        return id_like.split()[0]
    distro_id = os_release.get("ID", "").strip()
    return distro_id or "linux"


def parse_fingerprint_output(stdout: str) -> dict[str, Any]:
    """Parse the fixed fingerprint round-trip into its component fields.

    Returns ``{hostname, kernel, init_system, os_release}`` where
    ``os_release`` is the parsed ``/etc/os-release`` dict. Each section is
    delimited by a literal marker line; a missing section resolves to an
    empty value rather than raising.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    marker_of = {
        _FP_HOSTNAME: "hostname",
        _FP_KERNEL: "kernel",
        _FP_INIT: "init",
        _FP_OSRELEASE: "osrelease",
    }
    for line in stdout.splitlines():
        if line in marker_of:
            current = marker_of[line]
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)

    def _first(name: str) -> str | None:
        lines = [ln.strip() for ln in sections.get(name, []) if ln.strip()]
        return lines[0] if lines else None

    os_release = parse_os_release("\n".join(sections.get("osrelease", [])))
    return {
        "hostname": _first("hostname"),
        "kernel": _first("kernel"),
        "init_system": _first("init"),
        "os_release": os_release,
    }


class LinuxSshConnector(SshConnector):
    """Generic Linux-host connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("linux", "1.x", "linux-ssh")``. The ``1.x``
    version spans the systemd-Linux family served by portable primitives
    (``systemctl`` / ``cat`` / ``nft`` / ``sysctl``); a genuine per-distro
    command dialect would land as a *second* impl resolved by fingerprint,
    not a rewrite.

    **Auth: key-preferred + password-fallback.** Inherits the base
    ``_auth_config`` unchanged: a Vault secret with ``ssh_private_key``
    prefers key auth, otherwise password auth; default username ``root``.
    Credentials resolve via ``load_vault_secret_data`` under the operator's
    identity (the #2155 either/or shape), never an embedded dict.

    **Transport: plain SSH.** Every op runs a fixed, typed command over the
    pooled SSH connection. The read floor (T1) never mutates state and
    never reads secret material into an operation parameter.
    """

    product = "linux"
    version = "1.x"
    impl_id = "linux-ssh"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read hostname + ``/etc/os-release`` + kernel + init in one round-trip.

        Runs a single fixed command (the SSH adapter's pool ensures it
        shares the target's connection). Parses the distro identity, the
        kernel release, the hostname, and the init system.

        Unreachable / SSH-failed / credential-unresolvable → ``reachable
        =False`` + ``extras["error"]`` rather than propagating, so the
        shared :meth:`SshConnector._assert_reachable` guard surfaces the
        failure consistently from :meth:`about` (#986). The catch tuple
        covers the transport (``OSError`` / ``asyncssh.Error``) plus the
        credential-resolution failures ``_auth_config`` raises.
        """
        probed_at = datetime.now(UTC)
        try:
            proc = await self._run_command(target, _FINGERPRINT_CMD, operator=operator)
        except (
            OSError,
            asyncssh.Error,
            ValueError,
            VaultClientError,
            CredentialsReadError,
        ) as exc:
            _log.warning(
                "linux_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="linux",
                product="linux",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: cat /etc/os-release",
                extras={"error": str(exc)},
            )

        raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
        stdout = raw if isinstance(raw, str) else ""
        parsed = parse_fingerprint_output(stdout)
        os_release = parsed["os_release"]

        return FingerprintResult(
            vendor=_derive_vendor(os_release),
            product="linux",
            version=os_release.get("VERSION_ID") or None,
            build=parsed["kernel"],
            reachable=True,
            probed_at=probed_at,
            probe_method="ssh: cat /etc/os-release",
            extras={
                "hostname": parsed["hostname"],
                "os_pretty": os_release.get("PRETTY_NAME") or None,
                "kernel": parsed["kernel"],
                "init_system": parsed["init_system"],
                "distro_id": os_release.get("ID") or None,
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + os-release + systemd-presence check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect (host
          down, firewall, wrong port). Catches :exc:`OSError`.
        * ``ssh_auth_failed`` -- credentials were rejected
          (:exc:`asyncssh.PermissionDenied`), the handshake failed for a
          non-auth reason (:exc:`asyncssh.DisconnectError`), or the Vault
          credential read failed. ``probe()`` carries no operator, so the
          read runs under the synthesised system operator and fails
          closed.
        * ``command_failed`` -- the handshake succeeded but a post-connect
          command dropped or timed out. Catches ``(OSError,
          asyncssh.Error)`` (``TimeoutError`` is an ``OSError`` subclass).
        * ``os_release_unreadable`` -- ``cat /etc/os-release`` exited
          non-zero or returned empty (not a standard distro, or a
          permissions problem).
        * ``systemd_absent`` -- a reachable host with no systemd. Load
          -bearing: the ``service.status`` verb depends on ``systemctl``,
          so a non-systemd host is surfaced as a distinct, actionable
          reason rather than a generic failure.

        The probe does not mutate state -- every command is read-only.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            latency_ms = (time.monotonic() - start) * 1000.0
            return ProbeResult(ok=ok, reason=reason, latency_ms=latency_ms, probed_at=probed_at)

        # Order matters: PermissionDenied (subclass of DisconnectError)
        # must be caught before DisconnectError; OSError is the TCP-level
        # failure. Credential-resolution failures fold into ssh_auth_failed.
        try:
            await self._connect(target)
        except asyncssh.PermissionDenied:
            return _result(False, "ssh_auth_failed")
        except asyncssh.DisconnectError:
            return _result(False, "ssh_auth_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except (ValueError, VaultClientError, CredentialsReadError):
            return _result(False, "ssh_auth_failed")

        # os-release read -- a mid-probe transport failure is command_failed;
        # a non-zero exit or empty body is os_release_unreadable.
        try:
            os_proc = await self._run_command(target, _PROBE_OSRELEASE_CMD)
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")
        os_raw = (os_proc.stdout or "") if hasattr(os_proc, "stdout") else ""
        os_content = os_raw if isinstance(os_raw, str) else ""
        if getattr(os_proc, "exit_status", 0) not in (0, None) or not os_content.strip():
            return _result(False, "os_release_unreadable")

        # systemd-presence -- a reachable host with no systemd cannot serve
        # the service verbs, so it is a distinct reason.
        try:
            init_proc = await self._run_command(target, _PROBE_SYSTEMD_CMD)
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")
        init_raw = (init_proc.stdout or "") if hasattr(init_proc, "stdout") else ""
        init_out = init_raw.strip() if isinstance(init_raw, str) else ""
        if init_out != "systemd":
            return _result(False, "systemd_absent")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the host's vendor/product/version/kernel identity snapshot.

        Op-id: ``linux.about``. Reuses :meth:`fingerprint` so the
        operator-facing op and the canonical fingerprint share one round
        -trip. :meth:`SshConnector._assert_reachable` maps an unreachable
        target to a :exc:`ConnectorUnreachableError` the dispatcher reports
        as a non-ok result (#986) rather than a hollow ``status="ok"``
        envelope of None fields.
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target, operator)
        self._assert_reachable(result)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "kernel": result.build,
            "os_pretty": result.extras.get("os_pretty"),
            "hostname": result.extras.get("hostname"),
            "init_system": result.extras.get("init_system"),
        }

    async def file_read(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.file.read`` (group ``file``)."""
        from meho_backplane.connectors.linux.ops_file import linux_file_read

        return await linux_file_read(self, target, params, operator)

    async def log_tail(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.log.tail`` (group ``log``)."""
        from meho_backplane.connectors.linux.ops_file import linux_log_tail

        return await linux_log_tail(self, target, params, operator)

    async def service_status(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.service.status`` (group ``service``)."""
        from meho_backplane.connectors.linux.ops_host import linux_service_status

        return await linux_service_status(self, target, params, operator)

    async def sysctl_read(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.sysctl.read`` (group ``system``)."""
        from meho_backplane.connectors.linux.ops_host import linux_sysctl_read

        return await linux_sysctl_read(self, target, params, operator)

    async def firewall_show(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.firewall.show`` (group ``firewall``)."""
        from meho_backplane.connectors.linux.ops_firewall import linux_firewall_show

        return await linux_firewall_show(self, target, params, operator)

    async def mount_list(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``linux.mount.list`` (group ``storage``)."""
        from meho_backplane.connectors.linux.ops_storage import linux_mount_list

        return await linux_mount_list(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`LINUX_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Mirrors the
        :meth:`Rke2SshConnector.register_operations` shape -- idempotent
        across pod restarts. Fails closed with :class:`ValueError` if a
        declared ``group_key`` has no curated ``when_to_use`` blurb in
        :data:`LINUX_WHEN_TO_USE_BY_GROUP`.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in LINUX_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"LinuxSshConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = LINUX_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"LinuxSshConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"LINUX_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.linux.ops."
                    )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "linux_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(
        self,
        target: Target,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatcher shim -- delegate to the G0.6 lookup + invoke path.

        Mirrors :meth:`Rke2SshConnector.execute`. Operator-less (no policy
        gate, no audit row, no broadcast) because direct callers do not
        carry an :class:`~meho_backplane.auth.operator.Operator`; the
        operator-aware surface is ``POST /api/v1/operations/call`` via the
        G0.6 meta-tools.
        """
        from sqlalchemy import select

        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import EndpointDescriptor
        from meho_backplane.operations._errors import (
            result_connector_error,
            result_invalid_params,
            result_unknown_op,
        )
        from meho_backplane.operations._handler_resolve import (
            import_handler,
            is_unbound_method,
        )
        from meho_backplane.operations._lookup import count_known_ops
        from meho_backplane.operations._validate import validate_params

        start = time.monotonic()

        def _elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == self.product,
                    EndpointDescriptor.version == self.version,
                    EndpointDescriptor.impl_id == self.impl_id,
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.is_enabled.is_(True),
                )
            )
            descriptor = result.scalar_one_or_none()

        if descriptor is None:
            known_op_count = await count_known_ops(
                tenant_id=None,  # operator-less chassis path: global rows only
                product=self.product,
                version=self.version,
                impl_id=self.impl_id,
            )
            return result_unknown_op(op_id, known_op_count, _elapsed())

        validation_errors = validate_params(descriptor.parameter_schema, params)
        if validation_errors:
            return result_invalid_params(op_id, validation_errors, _elapsed())

        handler = import_handler(descriptor.handler_ref or "")
        if is_unbound_method(handler, type(self)):
            handler = handler.__get__(self, type(self))

        try:
            raw = await handler(target=target, params=params)
        except Exception as exc:
            return result_connector_error(op_id, exc, _elapsed())

        return OperationResult(
            status="ok",
            op_id=op_id,
            result=raw if isinstance(raw, (dict, list)) else {"value": raw},
            duration_ms=_elapsed(),
        )

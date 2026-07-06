# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""HolodeckConnector -- typed SSH-transport connector for VMware Holodeck 9.0.

G3.8-T1 (#853) skeleton -- the tier-4 closer in the SSH-transport
family (after bind9 G3.4 and pfSense G3.7). HoloRouter exposes no
REST API; the connector's only transport is SSH-to-root driving
``pwsh -EncodedCommand`` for Holodeck cmdlets, mediated by
:mod:`meho_backplane.connectors.holodeck._pwsh`.

This module ships:

* :class:`HolodeckConnector`, subclass of
  :class:`~meho_backplane.connectors.adapters.ssh.SshConnector`,
  carrying the registry-v2 triple
  ``("holodeck", "9.0", "holodeck-ssh")``.
* :meth:`HolodeckConnector.fingerprint` -- SSH +
  ``cat /etc/photon-release`` + a ``Get-HoloDeckConfig | ConvertTo-Json``
  cmdlet routed through the pwsh helper. Returns the canonical
  :class:`FingerprintResult` shape with ``vendor="vmware"`` /
  ``product="holodeck"``; ``version`` is the parsed Holodeck release
  string from the cmdlet output; ``extras`` carries
  ``photon_version`` and ``pod_id``. Unreachable / SSH-failed /
  cmdlet-failed targets surface as ``reachable=False`` +
  ``extras["error"]`` with the exception message.
* :meth:`HolodeckConnector.probe` -- TCP + SSH handshake + Photon
  health check (``/etc/photon-release`` non-empty) + Holodeck
  services check (``Get-Service ... | ConvertTo-Json``). Surfaces
  four distinct ``ProbeResult.reason`` values per #371: ``tcp_unreachable``,
  ``ssh_auth_failed``, ``photon_unhealthy``, ``holodeck_services_down``.
* :meth:`HolodeckConnector.about` -- operator-facing wrapper around
  :meth:`fingerprint`, registered as the ``holodeck.about`` typed op
  (T1's canary).
* :meth:`HolodeckConnector.execute` -- the G0.6 dispatcher shim
  (same shape as :meth:`Bind9Connector.execute` and
  :meth:`PfSenseConnector.execute`).

Auth uses the base
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector._auth_config`
unchanged: password-default (the HoloRouter OVA ships root password
auth; ``secret_ref.password``) with key-fallback when
``ssh_private_key`` is present. This inverts the pfSense connector
(key-only, no password) and matches the Initiative #371 specification.

The 8 read ops (config / pod / service / k8s / logs / networking)
ship in G3.8-T2 (#854) by appending to
:data:`~meho_backplane.connectors.holodeck.ops.HOLODECK_OPS` from a
sibling module; the dispatcher shim does not change.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

import asyncssh
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.holodeck._pwsh import PwshRunError, pwsh_run
from meho_backplane.connectors.holodeck.ops import HOLODECK_OPS
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["HolodeckConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH adapter
# and the Bind9 / pfSense siblings.
type Target = Any


# ``/etc/photon-release`` ships in the form ``VMware Photon Linux <X>.<Y>``
# on every Photon release this connector targets (4.x / 5.x). The version
# token is the first ``<digit>.<digit>(.<digit>)?`` group on the line.
_PHOTON_VERSION_RE: re.Pattern[str] = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def parse_photon_version(content: str) -> str | None:
    """Return the Photon version from ``/etc/photon-release``, or ``None``.

    Examples
    --------

    >>> parse_photon_version("VMware Photon Linux 5.0\\nPHOTON_BUILD_NUMBER=...")
    '5.0'
    >>> parse_photon_version("VMware Photon OS 4.0")
    '4.0'
    >>> parse_photon_version("") is None
    True
    """
    if not content.strip():
        return None
    match = _PHOTON_VERSION_RE.search(content)
    return match.group(1) if match else None


#: Curated ``when_to_use`` strings per group key, indexed by
#: :meth:`HolodeckConnector.register_operations`. Each entry covers a
#: ``group_key`` declared in :data:`HOLODECK_OPS`. T1 ships only the
#: ``identity`` group (``holodeck.about``); T2 adds ``config`` /
#: ``pod`` / ``service`` / ``k8s`` / ``logs`` / ``networking`` entries
#: by appending to this mapping (the registration walk fails closed
#: with a :class:`ValueError` if a ``group_key`` lacks a curated entry,
#: per the bind9 / pfSense precedent).
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for Holodeck appliance identity questions before any "
        "per-pod or per-service drill-in: 'which Holodeck version "
        "is this HoloRouter running, on which Photon OS, and what "
        "is the pod identifier?'. The single ``holodeck.about`` op "
        "returns vendor / product / version (parsed Holodeck "
        "release), the Photon OS version, and the pod ID. Call this "
        "first when the agent needs to confirm the HoloRouter is "
        "reachable via SSH + pwsh before issuing higher-level "
        "pod / service / log ops -- Holodeck has no REST API, so "
        "this op also confirms the PowerShell-over-SSH transport "
        "is functional."
    ),
    "config": (
        "Use for Holodeck appliance configuration questions: "
        "``holodeck.config.show`` returns the full Get-HoloDeckConfig "
        "dict (vendor + product + pod ID + services block). For just "
        "the identifying fields, ``holodeck.about`` is faster. "
        "Transport: pwsh-over-SSH (no REST surface)."
    ),
    "pod": (
        "Use for Holodeck nested-pod operations: list the active "
        "pods (``holodeck.pod.list``) or pull per-pod detail "
        "(``holodeck.pod.info <pod_id>``). Pod lists carry state, "
        "primary networking, and VM count; per-pod info adds the VM "
        "list and FRR/BGP attachment. Transport: pwsh-over-SSH."
    ),
    "service": (
        "Use for Holodeck Photon service health: "
        "``holodeck.service.list`` returns the bundled services "
        "(DHCP, DNS, NTP, FRR-BGP, Webtop, K8s-in-appliance) and "
        "their Status. Pair with ``holodeck.logs.tail`` for "
        "drill-in. Transport: pwsh-over-SSH."
    ),
    "k8s": (
        "Use for read-only inspection of the K8s cluster bundled on "
        "the HoloRouter appliance via ``holodeck.k8s.exec`` "
        "(``kubectl get``/``describe``/``logs``/``top``/``explain``/"
        "``api-resources``/``api-versions``/``cluster-info``/"
        "``version``). Mutating verbs are rejected fail-closed at "
        "the schema layer and re-checked by the handler. Transport: "
        "plain SSH to the appliance (no pwsh wrapper); the in-"
        "appliance ``kubectl`` reaches the cluster directly."
    ),
    "logs": (
        "Use for Holodeck runtime log inspection: "
        "``holodeck.logs.tail component=<slug> [lines=N]`` runs "
        "``tail`` over ``/holodeck-runtime/logs/<component>*.log``. "
        "Slugs map to bundled services (``dhcp``, ``dns``, ``frr``, "
        "``webtop``, ``k8s``); lines defaults to 200, clamped at "
        "[1, 5000]. Transport: plain SSH."
    ),
    "networking": (
        "Use for the HoloRouter's networking-surface snapshot: "
        "``holodeck.networking.show`` composes FRR/BGP summary + "
        "kernel routes + DNS zone summary + DHCP leases into a "
        "single envelope with per-sub-section ``ok`` flags. "
        "Pair with ``holodeck.logs.tail component=frr`` for FRR log "
        "drill-in. Transport: plain SSH for vtysh + dhcpd.leases; "
        "pwsh-over-SSH for the DNS zone summary."
    ),
    "diagnostics": (
        "Use for the HoloRouter's disk-pressure snapshot before a "
        "root-fs fill evicts pods: ``holodeck.disk.usage`` returns "
        "root-fs total/used/available bytes + percent-used plus the "
        "byte usage of the two known growth directories "
        "(``/var/backups``, ``/holodeck-runtime``), each with its own "
        "``ok`` flag. Takes no path argument -- the measured dirs are "
        "fixed in code, never operator input. Poll it for early "
        "warning on the 74 GB root fs (VCF-9.x backup fill). "
        "Transport: plain SSH (``df`` / ``du``)."
    ),
}


class HolodeckConnector(SshConnector):
    """VMware Holodeck 9.0 connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("holodeck", "9.0", "holodeck-ssh")``. The
    ``9.0`` version targets the current Holodeck Toolkit release as
    of 2026. A future ``("holodeck", "10.x", ...)`` entry can ship
    alongside without disturbing 9.0 targets.

    **Auth: password-default + key-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged: a Vault secret
    with ``ssh_private_key`` prefers key auth, otherwise password
    auth runs (the HoloRouter OVA ships root password auth out of
    the box per #371). This inverts the pfSense connector's
    key-only override; neither connector embeds the auth-selection
    logic in its own module.

    **Transport: PowerShell-over-SSH.** Holodeck cmdlets reach the
    appliance through ``pwsh -EncodedCommand <base64-utf16le>``
    routed by :func:`~meho_backplane.connectors.holodeck._pwsh.pwsh_run`.
    Output is parsed via stdlib :mod:`json` from the cmdlet's
    ``ConvertTo-Json`` pipe -- the #371 design correction (2026-05-21)
    supersedes the original CliXml note.
    """

    product = "holodeck"
    version = "9.0"
    impl_id = "holodeck-ssh"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read Photon release + ``Get-HoloDeckConfig`` -- canonical fingerprint.

        Runs (in order, sharing one pooled SSH connection):

        1. ``cat /etc/photon-release`` -- read the Photon OS release
           identifier. Parsed via :func:`parse_photon_version`.
        2. ``pwsh -EncodedCommand`` of
           ``Get-HoloDeckConfig | ConvertTo-Json -Compress``. The cmdlet
           returns a flat object with the Holodeck version and pod
           ID; the helper parses the JSON output via stdlib
           :mod:`json` and returns a Python dict.

        ``probe_method="ssh: pwsh Get-HoloDeckConfig"`` matches the
        Initiative #371 specification.

        Unreachable / SSH-failed / cmdlet-failed → ``reachable=False``
        + ``extras["error"]`` with the exception message. The error
        carries the exception class name + a sanitised stderr fragment
        (truncated by :class:`PwshRunError`); credential material
        cannot leak through because the connector handlers never
        interpolate ``target.secret_ref`` fields into the PowerShell
        text.

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — Holodeck
        authenticates via SSH key, not Vault OIDC, so the route operator
        plays no role here.
        """
        del operator  # unused — SSH key auth, no Vault credential read
        probed_at = datetime.now(UTC)

        # Phase 1: read /etc/photon-release. Failure here is a hard
        # fingerprint failure -- there's no Photon-less Holodeck shape
        # to fall back to.
        try:
            photon_proc = await self._run_command(target, "cat /etc/photon-release", raw_jwt="")
        except (OSError, asyncssh.Error) as exc:
            _log.warning(
                "holodeck_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="vmware",
                product="holodeck",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: pwsh Get-HoloDeckConfig",
                extras={"error": str(exc)},
            )

        photon_raw = (photon_proc.stdout or "") if hasattr(photon_proc, "stdout") else ""
        photon_content = photon_raw if isinstance(photon_raw, str) else ""
        photon_version = parse_photon_version(photon_content)
        photon_build = photon_content.strip().splitlines()[0] if photon_content.strip() else None

        # Phase 2: pull the Holodeck config. The cmdlet is the only
        # supported probe shape for the appliance's own version+pod
        # data (Holodeck has no /etc/-style file equivalent).
        try:
            payload = await pwsh_run(
                self,
                target,
                "Get-HoloDeckConfig | ConvertTo-Json -Compress",
            )
        except PwshRunError as exc:
            _log.warning(
                "holodeck_fingerprint_pwsh_failed",
                target=getattr(target, "name", None),
                exit_status=exc.exit_status,
            )
            return FingerprintResult(
                vendor="vmware",
                product="holodeck",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: pwsh Get-HoloDeckConfig",
                extras={
                    "error": str(exc),
                    "photon_version": photon_version,
                    "photon_build": photon_build,
                },
            )

        holodeck_version: str | None = None
        pod_id: str | None = None
        if isinstance(payload, dict):
            raw_version = payload.get("Version") or payload.get("HolodeckVersion")
            holodeck_version = raw_version if isinstance(raw_version, str) else None
            raw_pod = payload.get("PodId") or payload.get("PodID")
            pod_id = raw_pod if isinstance(raw_pod, str) else None

        return FingerprintResult(
            vendor="vmware",
            product="holodeck",
            version=holodeck_version,
            build=photon_build,
            reachable=True,
            probed_at=probed_at,
            probe_method="ssh: pwsh Get-HoloDeckConfig",
            extras={
                "photon_version": photon_version,
                "pod_id": pod_id,
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + Photon-health + Holodeck-services check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect
          (host down, firewall, wrong port). Catches :exc:`OSError`
          raised inside asyncssh's connect() before the SSH handshake.
        * ``ssh_auth_failed`` -- credentials were rejected
          (:exc:`asyncssh.PermissionDenied`) or the handshake failed
          for a non-auth reason (:exc:`asyncssh.DisconnectError`).
          Both shapes fold into ``ssh_auth_failed`` per #371's
          four-bucket failure-mode taxonomy (the Initiative does not
          require the bind9-style split between ``auth_failed`` and
          ``ssh_handshake_failed``; the Holodeck operator response is
          the same -- check the Vault secret or the appliance's
          ``/etc/ssh/sshd_config``).
        * ``photon_unhealthy`` -- SSH succeeded but
          ``/etc/photon-release`` is missing or empty. Photon OS
          ships ``/etc/photon-release`` on every supported release;
          an empty read signals a non-Photon target or a corrupt
          appliance image.
        * ``holodeck_services_down`` -- Photon is healthy but the
          bundled Holodeck services check (``Get-Service`` filtered
          to the Holodeck service set via pwsh) returns a non-empty
          list of services not in the ``Running`` state. The cmdlet
          shape mirrors the #371 body's example.

        The probe does not mutate state. ``Get-Service`` is read-only
        on Photon.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            latency_ms = (time.monotonic() - start) * 1000.0
            return ProbeResult(
                ok=ok,
                reason=reason,
                latency_ms=latency_ms,
                probed_at=probed_at,
            )

        # Order matters: PermissionDenied (subclass of DisconnectError)
        # must be caught before DisconnectError; OSError is the TCP-
        # level failure. ValueError from _auth_config (missing
        # credentials) folds into ssh_auth_failed -- the operator's
        # remediation is the same as for a rejected password.
        try:
            await self._connect(target, raw_jwt="")
        except asyncssh.PermissionDenied:
            return _result(False, "ssh_auth_failed")
        except asyncssh.DisconnectError:
            return _result(False, "ssh_auth_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except ValueError:
            return _result(False, "ssh_auth_failed")

        # Photon health: ``/etc/photon-release`` must produce non-empty
        # stdout. An empty read signals a non-Photon target (somebody
        # repointed the Holodeck target's Vault secret at a stock
        # Linux box) or a degraded appliance image.
        try:
            photon_proc = await self._run_command(target, "cat /etc/photon-release", raw_jwt="")
        except (OSError, asyncssh.Error):
            return _result(False, "ssh_auth_failed")
        photon_raw = (photon_proc.stdout or "") if hasattr(photon_proc, "stdout") else ""
        photon_content = photon_raw if isinstance(photon_raw, str) else ""
        if not photon_content.strip() or photon_proc.exit_status != 0:
            return _result(False, "photon_unhealthy")

        # Holodeck services health: ``Get-Service`` filtered to the
        # bundled Holodeck service set, piped through ConvertTo-Json
        # so we get a structured list back. Any service not in the
        # ``Running`` state flips the probe to ``holodeck_services_down``.
        # The exact filter prefix tracks the #371 body's example; T2's
        # ``holodeck.service.list`` op uses the same shape.
        try:
            services_payload = await pwsh_run(
                self,
                target,
                "Get-Service | Where-Object { $_.Name -like 'Holo*' } | "
                "Select-Object Name,Status | ConvertTo-Json",
            )
        except PwshRunError:
            return _result(False, "holodeck_services_down")

        # ``ConvertTo-Json`` on a single-element list returns a flat
        # dict; on a multi-element list it returns a JSON array. We
        # normalise both shapes to a list before walking it.
        if isinstance(services_payload, dict):
            services: list[Any] = [services_payload]
        elif isinstance(services_payload, list):
            services = services_payload
        else:
            services = []

        for svc in services:
            if not isinstance(svc, dict):
                continue
            status = svc.get("Status")
            # PowerShell renders the ServiceControllerStatus enum as
            # either the numeric int (``4`` for Running) or the string
            # ``"Running"`` depending on the cmdlet's depth setting.
            # Accept both shapes -- the canonical encoding is the
            # string form via ``Select-Object``.
            if status not in ("Running", 4):
                return _result(False, "holodeck_services_down")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the Holodeck appliance's product/version/Photon snapshot.

        Op-id: ``holodeck.about``. The dispatcher routes here after
        the JSON Schema validator has accepted *params* (declared
        empty in :mod:`~meho_backplane.connectors.holodeck.ops`).
        Reuses :meth:`fingerprint` so the operator-facing op and the
        canonical fingerprint share one network round-trip per call
        (two SSH commands plus the pwsh execution; one pooled
        connection). The returned dict is flat (no nested ``extras``)
        because the dispatcher's default reducer forwards the value
        verbatim into ``OperationResult.result``.
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "build": result.build,
            "photon_version": result.extras.get("photon_version"),
            "pod_id": result.extras.get("pod_id"),
        }

    async def config_show(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.config.show`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_config_show`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_config_show as _holodeck_config_show,
        )

        return await _holodeck_config_show(self, target, params)

    async def pod_list(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.pod.list`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_pod_list`.
        A large pod list is reduced by the dispatcher's default
        JsonFluxReducer into a ResultHandle (bounded inline sample plus a
        ``fetch_more`` envelope); the handler emits the ``{rows, total}``
        envelope so the reducer detects the collection without a connector
        change.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_pod_list as _holodeck_pod_list,
        )

        return await _holodeck_pod_list(self, target, params)

    async def pod_info(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.pod.info`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_pod_info`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_pod_info as _holodeck_pod_info,
        )

        return await _holodeck_pod_info(self, target, params)

    async def service_list(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.service.list`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_service_list`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_service_list as _holodeck_service_list,
        )

        return await _holodeck_service_list(self, target, params)

    async def k8s_exec(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.k8s.exec`` (G3.8-T2 #854).

        **Read-only**. Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_k8s_exec`,
        which re-validates the verb against the read-only safelist
        (belt-and-braces over the schema's pattern check) before
        forwarding the command to the in-appliance K8s cluster.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_k8s_exec as _holodeck_k8s_exec,
        )

        return await _holodeck_k8s_exec(self, target, params)

    async def logs_tail(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.logs.tail`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_logs_tail`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_logs_tail as _holodeck_logs_tail,
        )

        return await _holodeck_logs_tail(self, target, params)

    async def networking_show(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.networking.show`` (G3.8-T2 #854).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_networking_show`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_networking_show as _holodeck_networking_show,
        )

        return await _holodeck_networking_show(self, target, params)

    async def disk_usage(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``holodeck.disk.usage`` (G3.18-T1 #2153).

        Delegates to
        :func:`~meho_backplane.connectors.holodeck.ops_read.holodeck_disk_usage`.
        """
        from meho_backplane.connectors.holodeck.ops_read import (
            holodeck_disk_usage as _holodeck_disk_usage,
        )

        return await _holodeck_disk_usage(self, target, params)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`HOLODECK_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.holodeck.ops.HOLODECK_OPS`
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the
        :meth:`Bind9Connector.register_operations` /
        :meth:`PfSenseConnector.register_operations` shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in HOLODECK_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"HolodeckConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = _WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"HolodeckConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry "
                        f"to _WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.holodeck.connector so "
                        f"list_operation_groups surfaces a real "
                        f"selection signal instead of the auto-derive "
                        f"template."
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
            "holodeck_operations_registered",
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

        Mirrors :meth:`Bind9Connector.execute` and
        :meth:`PfSenseConnector.execute`. The dispatch shape is
        operator-less (no policy gate, no audit row, no broadcast)
        because direct callers do not carry an
        :class:`~meho_backplane.auth.operator.Operator`; the
        operator-aware surface is ``POST /api/v1/operations/call``
        via the G0.6 meta-tools.
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

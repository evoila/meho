# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rke2SshConnector -- typed plain-SSH connector for RKE2 cluster nodes.

G-Node/RKE2-T1 (#2221) scaffold -- the read-only entry in the SSH
node-OS-lifecycle family (Initiative #2172), built on the same
approval-gated write-op mold as holodeck-ssh (G3.18 #2145). RKE2 nodes
expose no MEHO REST surface; the connector's only transport is plain SSH
to the node OS over the shared
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector` adapter.

This module ships:

* :class:`Rke2SshConnector`, subclass of :class:`SshConnector`, carrying
  the registry-v2 triple ``("rke2", "1.x", "rke2-ssh")``.
* :meth:`Rke2SshConnector.fingerprint` -- SSH + ``rke2 --version`` +
  ``cat /etc/os-release``. Returns the canonical :class:`FingerprintResult`
  with ``vendor="rancher"`` / ``product="rke2"``; ``version`` is the
  parsed RKE2 release string; ``extras`` carries ``node_os``. Unreachable
  / SSH-failed targets surface as ``reachable=False`` + ``extras["error"]``.
* :meth:`Rke2SshConnector.probe` -- TCP + SSH handshake + RKE2-presence
  check with distinct ``ProbeResult.reason`` values.
* :meth:`Rke2SshConnector.about` -- operator-facing wrapper around
  :meth:`fingerprint`, registered as the ``rke2.about`` typed op.
* :meth:`Rke2SshConnector.posture_show` -- the read-only posture tier
  (``rke2.posture.show``): config-file modes + redacted token presence.
* :meth:`Rke2SshConnector.execute` -- the G0.6 dispatcher shim (same
  shape as :meth:`Bind9Connector.execute` / :meth:`HolodeckConnector.execute`).

Auth uses the base :class:`SshConnector` ``_auth_config`` unchanged --
key-preferred, password-fallback -- resolving ``target.secret_ref`` (a
Vault KV-v2 path string) via ``load_vault_secret_data`` under the
operator's identity (the #2155 either/or key-or-password shape). The
connector does **not** override ``_auth_config`` and does **not** touch
the bind9 anti-shape.

The approval-gated write ops (``rke2.token.rotate`` /
``rke2.node.service.restart`` / ``rke2.node.config.update`` /
``rke2.etcd-snapshot.save``) ship in sibling Tasks #2429/#2430/#2431 by
appending to :data:`~meho_backplane.connectors.rke2.ops.RKE2_OPS`; the
dispatcher shim does not change.
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
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.rke2.ops import RKE2_OPS
from meho_backplane.connectors.rke2.ops_write import RKE2_WHEN_TO_USE_WRITE_BY_GROUP
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["Rke2SshConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH
# adapter and the bind9 / holodeck siblings.
type Target = Any


# ``rke2 --version`` prints ``rke2 version v1.28.5+rke2r1 (<hash>)`` on
# the first line. The token after ``rke2 version`` is the release string.
_RKE2_VERSION_RE: re.Pattern[str] = re.compile(r"rke2 version\s+(\S+)")

# ``/etc/os-release`` carries ``PRETTY_NAME="Ubuntu 22.04.3 LTS"`` on
# every systemd distro RKE2 supports. Quotes are optional per the spec.
_OS_PRETTY_NAME_RE: re.Pattern[str] = re.compile(
    r'^PRETTY_NAME=(?:"([^"\n]*)"|([^\n]*))', re.MULTILINE
)

# ``rke2`` is not always on the login shell PATH; the installer symlinks
# it into ``/usr/local/bin``. Try both, and never fail the command (the
# ``|| true`` keeps the version probe non-fatal -- an agent node without
# the binary on PATH is still a reachable node).
_RKE2_VERSION_CMD: str = (
    "rke2 --version 2>/dev/null || /usr/local/bin/rke2 --version 2>/dev/null || true"
)

# Marker used by :meth:`probe` to confirm the node is an RKE2 node: the
# config directory RKE2 always creates, or the binary on PATH.
_RKE2_MARKER_CMD: str = (
    "test -e /etc/rancher/rke2 && echo present || "
    "(command -v rke2 >/dev/null 2>&1 && echo present) || echo absent"
)


def parse_rke2_version(banner: str) -> str | None:
    """Return the RKE2 release string from ``rke2 --version`` output.

    Examples
    --------

    >>> parse_rke2_version("rke2 version v1.28.5+rke2r1 (abc123)\\ngo version go1.21")
    'v1.28.5+rke2r1'
    >>> parse_rke2_version("") is None
    True
    """
    match = _RKE2_VERSION_RE.search(banner)
    return match.group(1) if match else None


def parse_os_pretty_name(content: str) -> str | None:
    """Return the ``PRETTY_NAME`` from ``/etc/os-release``, or ``None``.

    Examples
    --------

    >>> parse_os_pretty_name('NAME="Ubuntu"\\nPRETTY_NAME="Ubuntu 22.04 LTS"\\n')
    'Ubuntu 22.04 LTS'
    >>> parse_os_pretty_name("") is None
    True
    """
    match = _OS_PRETTY_NAME_RE.search(content)
    if match is None:
        return None
    value = match.group(1) if match.group(1) is not None else match.group(2)
    value = (value or "").strip()
    return value or None


class Rke2SshConnector(SshConnector):
    """RKE2 node connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("rke2", "1.x", "rke2-ssh")``. The ``1.x``
    version spans the RKE2 1.x release line; a future ``("rke2", "2.x",
    ...)`` entry can ship alongside without disturbing 1.x targets.

    **Auth: key-preferred + password-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged: a Vault secret with
    ``ssh_private_key`` prefers key auth, otherwise password auth runs.
    Credentials resolve via ``load_vault_secret_data`` (the #2155
    either/or shape), never the bind9 anti-shape.

    **Transport: plain SSH.** Every op runs a fixed, typed command over
    the pooled SSH connection. The read-only posture tier never reads
    secret material -- it ``stat``s modes + presence only.
    """

    product = "rke2"
    version = "1.x"
    impl_id = "rke2-ssh"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read ``rke2 --version`` + ``/etc/os-release`` -- canonical fingerprint.

        Runs two ``_run_command`` calls in sequence (the SSH adapter's
        pool ensures both share one connection): the RKE2 version probe
        (non-fatal -- ``version`` is ``None`` when the binary is not on
        PATH) and ``cat /etc/os-release`` for the node OS pretty-name.

        Unreachable / SSH-failed / credential-unresolvable → ``reachable
        =False`` + ``extras["error"]`` rather than propagating, so the
        shared :meth:`SshConnector._assert_reachable` guard surfaces the
        failure consistently from :meth:`about` (#986). The catch tuple
        covers the transport (``OSError`` / ``asyncssh.Error``) plus the
        credential-resolution failures ``_auth_config`` raises (the
        two-phase Vault contract + ``ValueError``).

        ``operator`` is threaded to the SSH adapter's ``_auth_config``,
        which resolves ``target.secret_ref`` under the operator's identity
        on a pool miss (#2155). ``None`` (background callers) fails closed
        at Vault and maps to ``reachable=False``.
        """
        probed_at = datetime.now(UTC)
        try:
            os_proc = await self._run_command(target, "cat /etc/os-release", operator=operator)
            ver_proc = await self._run_command(target, _RKE2_VERSION_CMD, operator=operator)
        except (
            OSError,
            asyncssh.Error,
            ValueError,
            VaultClientError,
            VaultCredentialsReadError,
        ) as exc:
            _log.warning(
                "rke2_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="rancher",
                product="rke2",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: rke2 --version",
                extras={"error": str(exc)},
            )

        os_raw = (os_proc.stdout or "") if hasattr(os_proc, "stdout") else ""
        os_content = os_raw if isinstance(os_raw, str) else ""
        node_os = parse_os_pretty_name(os_content)

        ver_raw = (ver_proc.stdout or "") if hasattr(ver_proc, "stdout") else ""
        ver_content = ver_raw if isinstance(ver_raw, str) else ""
        rke2_version = parse_rke2_version(ver_content)

        return FingerprintResult(
            vendor="rancher",
            product="rke2",
            version=rke2_version,
            reachable=True,
            probed_at=probed_at,
            probe_method="ssh: rke2 --version",
            extras={"node_os": node_os},
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + RKE2-presence check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect (host
          down, firewall, wrong port). Catches :exc:`OSError`.
        * ``ssh_auth_failed`` -- credentials were rejected
          (:exc:`asyncssh.PermissionDenied`) or the handshake failed for
          a non-auth reason (:exc:`asyncssh.DisconnectError`), or the
          Vault credential read failed. ``probe()`` carries no operator,
          so the read runs under the synthesised system operator and
          fails closed -- the remediation is the same as a rejected
          password: check the target's Vault secret.
        * ``rke2_undetected`` -- SSH succeeded but neither
          ``/etc/rancher/rke2`` nor an ``rke2`` binary on PATH is present;
          the target is reachable but is not an RKE2 node.

        The probe does not mutate state -- ``test`` / ``stat`` are
        read-only.
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
        except (ValueError, VaultClientError, VaultCredentialsReadError):
            return _result(False, "ssh_auth_failed")

        try:
            marker_proc = await self._run_command(target, _RKE2_MARKER_CMD)
        except (OSError, asyncssh.Error):
            return _result(False, "ssh_auth_failed")
        marker_raw = (marker_proc.stdout or "") if hasattr(marker_proc, "stdout") else ""
        marker = marker_raw.strip() if isinstance(marker_raw, str) else ""
        if marker != "present":
            return _result(False, "rke2_undetected")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the RKE2 node's vendor/product/version/node-OS snapshot.

        Op-id: ``rke2.about``. Reuses :meth:`fingerprint` so the
        operator-facing op and the canonical fingerprint share one
        network round-trip. :meth:`SshConnector._assert_reachable` maps an
        unreachable target to a :exc:`ConnectorUnreachableError` the
        dispatcher reports as a non-ok result (#986) rather than a hollow
        ``status="ok"`` envelope of None fields.
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target, operator)
        self._assert_reachable(result)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "node_os": result.extras.get("node_os"),
        }

    async def posture_show(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``rke2.posture.show`` (G-Node/RKE2-T1 #2221).

        Delegates to
        :func:`~meho_backplane.connectors.rke2.ops_read.rke2_posture_show`,
        which ``stat``s the RKE2 config-file modes + the redacted
        join-token presence over the shared SSH adapter.
        """
        from meho_backplane.connectors.rke2.ops_read import (
            rke2_posture_show as _rke2_posture_show,
        )

        return await _rke2_posture_show(self, target, params, operator)

    async def token_rotate(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for ``rke2.token.rotate`` (G-Node/RKE2-T2 #2429).

        **Approval-gated write.** Delegates to
        :func:`~meho_backplane.connectors.rke2.ops_write.rke2_token_rotate`;
        runs only on the ``_approved=True`` resume path. Mints a new server
        join token, rotates it over sudo-SSH, and stashes it in Vault --
        returning only a pointer, never the token value.
        """
        from meho_backplane.connectors.rke2.ops_write import (
            rke2_token_rotate as _rke2_token_rotate,
        )

        return await _rke2_token_rotate(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`RKE2_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Mirrors the
        :meth:`Bind9Connector.register_operations` /
        :meth:`HolodeckConnector.register_operations` shape -- idempotent
        across pod restarts.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in RKE2_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"Rke2SshConnector op {op.op_id!r} declares "
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
                        f"Rke2SshConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.rke2.connector."
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
            "rke2_operations_registered",
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
        :meth:`HolodeckConnector.execute`. Operator-less (no policy gate,
        no audit row, no broadcast) because direct callers do not carry an
        :class:`~meho_backplane.auth.operator.Operator`; the operator-aware
        surface is ``POST /api/v1/operations/call`` via the G0.6 meta-tools.
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


#: Curated ``when_to_use`` strings per group key, indexed by
#: :meth:`Rke2SshConnector.register_operations`. Each entry covers a
#: ``group_key`` declared in :data:`RKE2_OPS`; the registration walk fails
#: closed with a :class:`ValueError` if a ``group_key`` lacks a curated
#: entry (the bind9 / holodeck precedent). Defined after the class so the
#: strings can reference the transport note without a forward import.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for RKE2 node identity questions before any posture or "
        "(future) maintenance op: 'which RKE2 version is this node "
        "running, on which node OS, and is it reachable via SSH?'. The "
        "single ``rke2.about`` op returns vendor / product / RKE2 version "
        "/ node OS. Transport: plain SSH (no REST surface)."
    ),
    "posture": (
        "Use to audit an RKE2 node's config-surface security posture: "
        "``rke2.posture.show`` reports the permission modes of "
        "``/etc/rancher/rke2/config.yaml`` and the admin kubeconfig "
        "``rke2.yaml``, plus whether the on-disk server join token "
        "exists (presence + mode only -- the token VALUE is never read). "
        "Pair with a rotation runbook to confirm the token is present "
        "before rotating. Transport: plain SSH (``stat``, read-only)."
    ),
    # Approval-gated write groups. Keys carry a ``-write`` suffix so they
    # never collide with the read-op group keys above.
    **RKE2_WHEN_TO_USE_WRITE_BY_GROUP,
}

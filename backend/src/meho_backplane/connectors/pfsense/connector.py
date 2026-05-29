# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PfSenseConnector -- typed SSH-transport connector for pfSense 2.7.

G3.7-T1 (#844) skeleton -- the second ``SshConnector`` tier-3 child
(after Bind9Connector). This module ships:

* :class:`PfSenseConnector`, subclass of
  :class:`~meho_backplane.connectors.adapters.ssh.SshConnector`,
  carrying the registry-v2 triple ``("pfsense", "2.7", "pfsense-ssh")``.
* :meth:`PfSenseConnector._auth_config` -- overrides the base
  ``SshConnector._auth_config`` to **require** ``ssh_private_key`` and
  **reject** password auth. pfSense's default ``admin`` account over
  SSH with a password opens the console menu (interactive PHP shell)
  and hangs; allowing password fallback would create a deadlock-by-
  default failure mode. The override raises :exc:`ValueError` with a
  message naming the WebGUI break-glass credential when
  ``ssh_private_key`` is absent, rather than silently attempting the
  password path.
* :meth:`PfSenseConnector.fingerprint` -- SSH plus ``cat /etc/version``
  which emits the pfSense version string (e.g.
  ``2.7.2-RELEASE (amd64)\\nbuilt on Fri Jan 12 ...\\nFreeBSD ...``).
  Parses product, version, build, and kernel from the multi-line
  output. Unreachable → ``reachable=False`` + ``extras["error"]``.
* :meth:`PfSenseConnector.probe` -- TCP + SSH handshake +
  ``cat /etc/version`` asserting shell access. A console-menu / hang
  response (no ``/etc/version`` content in stdout) maps to
  ``ok=False`` + ``reason="no_shell_access"``. The check is
  deliberately conservative: if stdout is empty the probe fails rather
  than assuming the target is healthy.
* :meth:`PfSenseConnector.about` -- operator-facing wrapper around
  :meth:`fingerprint` registered as the ``pfsense.about`` typed op.
* :meth:`PfSenseConnector.execute` -- the dispatcher shim (same shape
  as :meth:`Bind9Connector.execute`) that routes a typed ``op_id``
  to its registered handler via the ``endpoint_descriptor`` substrate.

G3.7-T2 (#847) adds 7 read ops (``pfctl``/config.xml reads) via
:mod:`~meho_backplane.connectors.pfsense.ops_read`. Each op has a
bound-method shim on :class:`PfSenseConnector` that delegates to the
pure handler function in that module. The dispatcher shim does not
change.
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
from meho_backplane.connectors.pfsense.ops import PFSENSE_OPS
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["PfSenseConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH adapter
# and the Bind9Connector sibling.
type Target = Any

# pfSense /etc/version line 1 shape:
#   "2.7.2-RELEASE (amd64)"
# The version triple is the leading digit.digit.digit sequence; the
# parenthesised arch token follows. Anything after the version and
# arch is treated as trailing context and is ignored.
_VERSION_RE: re.Pattern[str] = re.compile(
    r"^(\d+\.\d+(?:\.\d+)?(?:-\S+)?)\s*(?:\(([^)]+)\))?",
    re.MULTILINE,
)

# pfSense /etc/version line 3+ shape:
#   "FreeBSD 14.1-RELEASE-p5 #1 ..."
# The kernel token is the first "FreeBSD <version>" group on any line.
_KERNEL_RE: re.Pattern[str] = re.compile(
    r"^(FreeBSD\s+\S+)",
    re.MULTILINE,
)


def parse_pfsense_version(content: str) -> dict[str, str | None]:
    """Parse ``/etc/version`` content into version, build, and kernel fields.

    Returns a dict with keys ``version``, ``build``, ``kernel`` --
    all ``str | None``. ``version`` is the pfSense release string
    (e.g. ``"2.7.2-RELEASE"``); ``build`` is the full first line;
    ``kernel`` is the first ``FreeBSD <token>`` fragment found on any
    line (e.g. ``"FreeBSD 14.1-RELEASE-p5"``).

    Examples
    --------

    >>> result = parse_pfsense_version(
    ...     "2.7.2-RELEASE (amd64)\\n"
    ...     "built on Fri Jan 12 18:00:00 UTC 2024\\n"
    ...     "FreeBSD 14.1-RELEASE-p5 #1 releng/14.1"
    ... )
    >>> result["version"]
    '2.7.2-RELEASE'
    >>> result["kernel"]
    'FreeBSD 14.1-RELEASE-p5'

    >>> parse_pfsense_version("")["version"] is None
    True
    """
    if not content.strip():
        return {"version": None, "build": None, "kernel": None}

    lines = content.strip().splitlines()
    first_line = lines[0].strip() if lines else ""

    version: str | None = None
    m_ver = _VERSION_RE.match(first_line)
    if m_ver:
        version = m_ver.group(1)

    build: str | None = first_line or None

    kernel: str | None = None
    m_kernel = _KERNEL_RE.search(content)
    if m_kernel:
        kernel = m_kernel.group(1)

    return {"version": version, "build": build, "kernel": kernel}


#: Curated ``when_to_use`` strings per group key, indexed by
#: :meth:`PfSenseConnector.register_operations`. Each entry is the
#: operator-facing prose answering "which group for my question?".
#: Must cover every ``group_key`` declared in :data:`PFSENSE_OPS`.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for pfSense firewall identity questions before any "
        "per-operation drill-in: 'which pfSense version is this "
        "target running?'. The single ``pfsense.about`` op returns "
        "vendor / product / version (parsed from ``/etc/version``), "
        "the full build line, and the FreeBSD kernel identifier. "
        "Call this first when the agent needs to confirm the firewall "
        "is reachable over SSH with shell access before issuing "
        "higher-level pfctl or config.xml ops."
    ),
    "firewall": (
        "Use for pfSense firewall operations: reading active filter rules "
        "(``pfsense.firewall.rules``) or the active state table "
        "(``pfsense.firewall.state``). Call ``pfsense.firewall.rules`` when "
        "the operator wants to audit the ruleset; call "
        "``pfsense.firewall.state`` when the operator wants to inspect active "
        "connections. The state table can be large on busy firewalls."
    ),
    "nat": (
        "Use for pfSense NAT operations: reading the active NAT ruleset "
        "(``pfsense.nat.rules``). Call when the operator wants to audit "
        "port-forwarding, outbound NAT, or 1:1 NAT rules."
    ),
    "network": (
        "Use for pfSense network-interface operations: listing all "
        "interfaces (``pfsense.interface.list``) or listing configured "
        "gateways (``pfsense.gateway.list``). Call "
        "``pfsense.interface.list`` when the operator wants IP address, "
        "MAC, MTU, or link-status information. Call "
        "``pfsense.gateway.list`` when the operator wants routing-gateway "
        "configuration from the pfSense config."
    ),
    "config": (
        "Use for pfSense configuration operations: reading the full "
        "pfSense configuration (``pfsense.config.show``) or getting a "
        "structured version summary (``pfsense.version``). Call "
        "``pfsense.config.show`` when the operator needs to inspect or "
        "export the complete pfSense config.xml."
    ),
}


class PfSenseConnector(SshConnector):
    """pfSense 2.7 connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("pfsense", "2.7", "pfsense-ssh")``. The
    ``2.7`` version targets the pfSense CE 2.7.x release series
    (FreeBSD 14.1 base, as of 2.7.2). A future ``("pfsense", "3.0",
    ...)`` entry can ship alongside without disturbing 2.7.x targets.

    **Auth: key-only.** The base :class:`SshConnector` supports a
    password fallback; this override removes it. pfSense's ``admin``
    user connected via SSH with a password opens the pfSense console
    menu (an interactive PHP shell) rather than a standard shell,
    causing any ``conn.run()`` command to hang indefinitely. The
    connector requires ``ssh_private_key`` in ``target.secret_ref``
    and raises :exc:`ValueError` with an actionable message when the
    key is absent. The ``password`` field in the Vault secret is the
    WebGUI break-glass credential and must not be used for SSH auth.
    """

    product = "pfsense"
    version = "2.7"
    impl_id = "pfsense-ssh"

    async def _auth_config(self, target: Target) -> dict[str, Any]:
        """Require ``ssh_private_key`` -- reject password auth.

        Overrides :meth:`SshConnector._auth_config` to enforce the
        pfSense-specific constraint that the ``password`` field in the
        Vault secret is the WebGUI break-glass credential and MUST NOT
        be used for SSH sessions. Password-based SSH on pfSense opens
        the console menu and hangs rather than providing a shell.

        Raises :exc:`ValueError` when ``ssh_private_key`` is absent,
        with a message directing the operator to configure SSH key
        auth on the pfSense WebGUI and store the key in the target's
        Vault secret under ``ssh_private_key``.
        """
        secret: dict[str, Any] = getattr(target, "secret_ref", {}) or {}
        username: str = secret.get("username", "admin")
        private_key_text: str | None = secret.get("ssh_private_key")
        if private_key_text:
            key = asyncssh.import_private_key(private_key_text)
            return {"username": username, "client_keys": [key]}
        raise ValueError(
            f"target '{getattr(target, 'name', target)!r}': pfSense connector requires "
            f"ssh_private_key in secret_ref — password auth is not supported. "
            f"The 'password' field in the Vault secret is the WebGUI break-glass "
            f"credential; configure SSH public-key auth on the pfSense WebGUI "
            f"(System > User Manager > admin > Authorized SSH Keys) and store the "
            f"private key in the target's Vault secret under ssh_private_key."
        )

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read ``/etc/version`` -- canonical pfSense fingerprint.

        Runs a single ``_run_command`` call against ``cat /etc/version``.
        The file ships on every pfSense release and contains:

        1. The pfSense release string (e.g. ``2.7.2-RELEASE (amd64)``).
        2. A build timestamp line (e.g. ``built on Fri Jan 12 ...``).
        3. A FreeBSD kernel line (e.g. ``FreeBSD 14.1-RELEASE-p5 ...``).

        The parsed version, full build line, and kernel fragment land in
        ``version``, ``build``, and ``extras["kernel"]`` respectively.
        Unreachable or auth-failure → ``reachable=False`` +
        ``extras["error"]`` with the exception message.

        ``probe_method="ssh: cat /etc/version"`` mirrors the bind9
        sibling's ``probe_method="ssh: named -v"`` convention.

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — pfSense
        authenticates via SSH key, not Vault OIDC, so the route operator
        plays no role here.
        """
        del operator  # unused — SSH key auth, no Vault credential read
        probed_at = datetime.now(UTC)

        try:
            proc = await self._run_command(target, "cat /etc/version", raw_jwt="")
        except (OSError, asyncssh.Error) as exc:
            _log.warning(
                "pfsense_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="netgate",
                product="pfsense",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: cat /etc/version",
                extras={"error": str(exc)},
            )

        stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
        content = stdout if isinstance(stdout, str) else ""
        parsed = parse_pfsense_version(content)

        return FingerprintResult(
            vendor="netgate",
            product="pfsense",
            version=parsed["version"],
            build=parsed["build"],
            reachable=True,
            probed_at=probed_at,
            probe_method="ssh: cat /etc/version",
            extras={"kernel": parsed["kernel"]},
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + shell-access check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect
          (host down, firewall, wrong port). Catches :exc:`OSError`
          raised inside asyncssh's connect() before the SSH handshake.
        * ``ssh_handshake_failed`` -- the TCP socket opened but the
          SSH handshake failed for a non-auth reason. Catches
          :exc:`asyncssh.DisconnectError`.
        * ``auth_failed`` -- credentials were rejected. Catches
          :exc:`asyncssh.PermissionDenied`. Also covers the case
          where ``ssh_private_key`` is absent in ``secret_ref``
          (``_auth_config`` raises :exc:`ValueError` before opening
          a wire connection, which surfaces here as ``auth_failed``).
        * ``no_shell_access`` -- the SSH handshake succeeded but
          ``cat /etc/version`` returned empty stdout. This is the
          console-menu trap: pfSense's ``admin`` SSH session without
          a forced-command may land in the interactive console menu
          (a PHP REPL) instead of a POSIX shell; the command then
          hangs or produces no file output. An empty stdout is treated
          as a definitive "no shell access" signal rather than a
          transient failure so operators get an actionable message
          ("enable SSH shell access on the pfSense WebGUI") rather
          than a generic timeout.
        * ``command_failed`` -- the SSH handshake succeeded but the
          post-connect ``cat /etc/version`` raised (connection dropped
          mid-probe, an :exc:`asyncssh.Error`, or an
          :exc:`asyncio.TimeoutError` from the command timeout). Caught
          here so a mid-probe failure maps to a non-ok
          :class:`~meho_backplane.connectors.schemas.ProbeResult` rather
          than escaping ``probe`` as an unhandled exception (#986).

        The probe does not mutate state and does not write to any
        filesystem -- ``cat /etc/version`` is read-only.
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
        # level failure. ValueError from _auth_config (missing key)
        # maps to auth_failed as well -- it's an auth configuration
        # problem, not a network problem.
        try:
            conn = await self._connect(target, raw_jwt="")
        except asyncssh.PermissionDenied:
            return _result(False, "auth_failed")
        except asyncssh.DisconnectError:
            return _result(False, "ssh_handshake_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except ValueError:
            # _auth_config raised: missing ssh_private_key.
            return _result(False, "auth_failed")

        del conn  # connection is pooled; _run_command will reuse it

        # Shell access check: ``cat /etc/version`` must produce non-empty
        # stdout. An empty stdout (exit_status 0 but no output) signals the
        # console-menu trap -- the admin SSH session landed in the interactive
        # pfSense console menu and the command was swallowed. A non-zero
        # exit_status (file not found, permission denied) also signals a
        # broken shell environment and maps to no_shell_access.
        #
        # The post-connect command is guarded: a connection drop, an
        # ``asyncssh.Error``, or a timeout after a successful handshake must
        # map to ``command_failed`` rather than propagating an unhandled
        # exception out of ``probe`` (#986). ``(OSError, asyncssh.Error)``
        # is the same catch tuple ``fingerprint`` uses; ``TimeoutError`` is
        # an ``OSError`` subclass so ``_run_command``'s ``asyncio.wait_for``
        # expiry is covered.
        try:
            version_proc = await self._run_command(target, "cat /etc/version", raw_jwt="")
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")
        stdout = (version_proc.stdout or "") if hasattr(version_proc, "stdout") else ""
        content = stdout if isinstance(stdout, str) else ""
        if not content.strip() or version_proc.exit_status != 0:
            return _result(False, "no_shell_access")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the pfSense firewall's product/version/build snapshot.

        Op-id: ``pfsense.about``. The dispatcher routes here after the
        JSON Schema validator has accepted *params* (declared empty in
        :mod:`~meho_backplane.connectors.pfsense.ops`). Reuses
        :meth:`fingerprint` so the operator-facing op and the canonical
        fingerprint share one SSH command execution. The returned dict
        is flat (no nested ``extras``) because the dispatcher's default
        reducer forwards the value verbatim.

        When the target is unreachable, :meth:`fingerprint` returns
        ``reachable=False`` rather than raising. ``_assert_reachable``
        re-raises that as a
        :exc:`~meho_backplane.connectors.adapters.ssh.ConnectorUnreachableError`
        so the dispatcher reports a non-ok op instead of a successful op
        carrying empty/None identity fields (#986).
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target)
        self._assert_reachable(result)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "build": result.build,
            "kernel": result.extras.get("kernel"),
        }

    async def get_version(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.version`` (G3.7-T2 #847).

        Named ``get_version`` to avoid shadowing the class-level
        ``version = "2.7"`` registry attribute. The ``handler_attr``
        on the op metadata is ``"get_version"`` accordingly. Delegates
        to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_version`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_version as _pfsense_version,
        )

        return await _pfsense_version(self, target, params)

    async def firewall_rules(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.firewall.rules`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_firewall_rules`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_firewall_rules as _pfsense_firewall_rules,
        )

        return await _pfsense_firewall_rules(self, target, params)

    async def firewall_state(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.firewall.state`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_firewall_state`.
        The state table can contain thousands of rows on busy firewalls;
        the dispatcher's default
        :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
        wraps the result in a ``ResultHandle`` when ``total`` exceeds its
        configured threshold.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_firewall_state as _pfsense_firewall_state,
        )

        return await _pfsense_firewall_state(self, target, params)

    async def nat_rules(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.nat.rules`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_nat_rules`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_nat_rules as _pfsense_nat_rules,
        )

        return await _pfsense_nat_rules(self, target, params)

    async def interface_list(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.interface.list`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_interface_list`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_interface_list as _pfsense_interface_list,
        )

        return await _pfsense_interface_list(self, target, params)

    async def gateway_list(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.gateway.list`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_gateway_list`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_gateway_list as _pfsense_gateway_list,
        )

        return await _pfsense_gateway_list(self, target, params)

    async def config_show(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``pfsense.config.show`` (G3.7-T2 #847).

        Delegates to
        :func:`~meho_backplane.connectors.pfsense.ops_read.pfsense_config_show`.
        """
        from meho_backplane.connectors.pfsense.ops_read import (
            pfsense_config_show as _pfsense_config_show,
        )

        return await _pfsense_config_show(self, target, params)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`PFSENSE_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.pfsense.ops.PFSENSE_OPS` and
        routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the
        :meth:`Bind9Connector.register_operations` shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in PFSENSE_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"PfSenseConnector op {op.op_id!r} declares "
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
                        f"PfSenseConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry "
                        f"to _WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.pfsense.connector so "
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
            "pfsense_operations_registered",
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

        Mirrors :meth:`Bind9Connector.execute`. The dispatch shape is
        operator-less (no policy gate, no audit row, no broadcast)
        because direct callers like the chassis and typed-connector
        internals do not carry an
        :class:`~meho_backplane.auth.operator.Operator`; the
        operator-aware surface is ``POST /api/v1/operations/call``
        via the G0.6 meta-tools. Contract:

        * Unknown ``op_id`` → the structured ``unknown_op`` envelope.
        * Params failing the descriptor's JSON Schema → ``invalid_params``.
        * Handler exception → ``connector_error`` envelope.
        * Happy path → ``OperationResult(status="ok", ...)``.
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

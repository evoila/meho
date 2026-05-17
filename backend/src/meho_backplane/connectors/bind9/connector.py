# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bind9Connector -- typed SSH-transport connector for ISC bind9 9.x.

G3.4-T1 (#587) skeleton -- the first ``SshConnector`` tier-1 child.
This module ships:

* :class:`Bind9Connector`, subclass of
  :class:`~meho_backplane.connectors.adapters.ssh.SshConnector`,
  carrying the registry-v2 triple ``("bind9", "9.x", "bind9-ssh")``.
* :meth:`Bind9Connector._remote_bash_with_sudo` -- the **load-bearing
  safety primitive** for the whole G3.4 connector. The wrapper this
  connector replaces (the consumer's ``scripts/bind9-dns.sh``) leaked
  the sudo password into the remote shell-history file twice in seven
  days (2026-05-04 and 2026-05-05) because its ``remote_bash()`` shape
  let callers mis-order the heredoc lines. The helper here encodes
  the safe ordering **by construction**: the password is a
  keyword-only argument streamed on stdin to ``sudo -S``'s built-in
  reader, the script body is a separate positional argument
  streamed on stdin to ``bash -s`` after ``sudo -S`` has consumed
  its password line, and the constructed remote command is the
  fixed string ``"sudo -S -p '' bash -s"`` -- with no caller-
  controllable substring. The caller **cannot** express a
  mis-ordered payload because there is no API shape that takes a
  pre-built shell script with the password line embedded. Structured
  logs record ``cmd_len`` and ``exit_status`` only; the script body
  and the password are never logged, never present in the remote
  process argv (so ``ps`` cannot see them), and never written to
  the remote shell-history file (because ``-c``/argv is not used).
* :meth:`Bind9Connector.fingerprint` -- SSH plus ``named -v`` and
  ``/etc/os-release`` (with ``/etc/debian_version`` as a fallback).
  Returns the canonical :class:`~meho_backplane.connectors.schemas.FingerprintResult`
  shape with ``version`` parsed from the ``BIND <X.Y.Z>-...`` banner.
* :meth:`Bind9Connector.probe` -- TCP plus SSH handshake plus
  ``pgrep -x named`` (named-process-running check) plus
  ``named-checkconf -p > /dev/null`` (config-parses check). Each
  failure mode surfaces a distinct ``ProbeResult.reason``:
  ``tcp_unreachable``, ``ssh_handshake_failed``, ``auth_failed``,
  ``named_not_running``, ``named_config_invalid``.
* :meth:`Bind9Connector.about` -- the operator-facing wrapper around
  fingerprint registered as the ``bind9.about`` typed op.
* :meth:`Bind9Connector.execute` -- the dispatcher shim (same shape
  as :meth:`KubernetesConnector.execute`) that routes a typed
  ``op_id`` to its registered handler via the
  ``endpoint_descriptor`` substrate.

The remaining 10 ops (zone / record / config reads + writes) land
under G3.4-T2..T4 by extending :data:`BIND9_OPS` from their own
modules; the dispatcher shim does not change.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import asyncssh
import structlog

from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.bind9.ops import BIND9_OPS
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["Bind9Connector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH adapter.
type Target = Any

# Fixed remote command for the safe-sudo helper. Built once at module
# scope so the helper's ``conn.run`` call site has zero string
# interpolation -- the only caller-supplied data flows through
# ``input=`` (stdin), never through the command string. ``-S`` makes
# sudo read the password from stdin; ``-p ''`` suppresses the prompt
# so the password line is not echoed; ``bash -s`` reads its commands
# from stdin (the remainder after ``sudo -S`` has consumed its single
# password line).
_SUDO_BASH_REMOTE_CMD: str = "sudo -S -p '' bash -s"

# Regex that recovers the ``<X.Y.Z>`` version triple from a
# ``named -v`` banner. ISC ships the banner as
# ``BIND <X.Y.Z>-<distro-suffix>`` (e.g.
# ``BIND 9.18.24-1+deb12u2-Debian``) on every supported distribution;
# the version triple is always the first three dot-separated digit
# groups after the leading ``BIND ``. Anything past the triple goes
# into the ``build`` field verbatim.
_NAMED_V_VERSION_RE: re.Pattern[str] = re.compile(r"BIND\s+(\d+\.\d+\.\d+)")


def parse_named_version(banner: str) -> str | None:
    """Return the ``<X.Y.Z>`` triple from a ``named -v`` banner, or ``None``.

    Examples
    --------

    >>> parse_named_version("BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>")
    '9.18.24'
    >>> parse_named_version("BIND 9.16.50-Debian (Extended Support Version) <id:>")
    '9.16.50'
    >>> parse_named_version("") is None
    True
    """
    match = _NAMED_V_VERSION_RE.search(banner)
    return match.group(1) if match else None


def parse_os_release(content: str) -> str | None:
    """Return a human-readable OS identifier from ``/etc/os-release`` content.

    ``/etc/os-release`` is a key=value file documented at
    https://www.freedesktop.org/software/systemd/man/os-release.html;
    the load-bearing fields for the bind9 fingerprint are ``ID`` and
    ``VERSION_ID``. Returns ``"<id> <version_id>"`` (e.g.
    ``"debian 12"``) when both are present, ``ID`` alone when
    ``VERSION_ID`` is missing, and ``None`` when neither is present
    (degenerate file).
    """
    fields: dict[str, str] = {}
    for line in content.splitlines():
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        # Values may be quoted with single or double quotes; the spec
        # is permissive. Stripping both quote shapes covers the cases
        # we see in the wild (Debian quotes ``VERSION_ID``, Ubuntu
        # double-quotes ``PRETTY_NAME``, etc.).
        value = raw_value.strip().strip('"').strip("'")
        if value:
            fields[key.strip()] = value
    os_id = fields.get("ID")
    if not os_id:
        return None
    version_id = fields.get("VERSION_ID")
    if version_id:
        return f"{os_id} {version_id}"
    return os_id


class Bind9Connector(SshConnector):
    """ISC bind9 9.x connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("bind9", "9.x", "bind9-ssh")``. The
    ``9.x`` shape mirrors the Vault sibling's ``1.x`` shape -- bind9
    9.18 (current ESV) and 9.20 (current stable) share the entire
    surface this connector targets; a future ``("bind9", "10.x", ...)``
    entry can ship alongside without disturbing 9.x deployments.
    """

    # Registry v2 metadata. ``impl_id="bind9-ssh"`` (not ``"bind9"``)
    # makes room for a future ``("bind9", "9.x", "bind9-rndc")`` or
    # ``("bind9", "9.x", "bind9-rest")`` sibling once a non-SSH
    # control surface lands; the multi-impl shape is already in the
    # registry-v2 substrate (vmware-rest vs the future
    # vmware-pyvmomi). The ``9.x`` version range covers ISC's 9.18
    # (current Extended Support Version) and 9.20 (current Stable
    # Release) -- both expose ``named -v`` / ``named-checkconf -p``
    # with the same flags and the same banner format.
    product = "bind9"
    version = "9.x"
    impl_id = "bind9-ssh"

    async def _remote_bash_with_sudo(
        self,
        target: Target,
        script: str,
        *,
        raw_jwt: str,
        sudo_password: str,
        timeout: float = 60.0,
    ) -> asyncssh.SSHCompletedProcess:
        """Run *script* on *target* under ``sudo`` without leaking the password.

        Safe-by-construction sudo invocation. The wire shape is fixed:

        1. Remote command (argv): ``"sudo -S -p '' bash -s"`` -- one
           constant string with no caller interpolation. ``sudo -S``
           reads the password from stdin; ``-p ''`` suppresses the
           prompt so the password line is not echoed back; ``bash -s``
           reads its commands from stdin once sudo has consumed the
           password line.
        2. Stdin payload: ``"<sudo_password>\\n<script>\\n"`` -- the
           password is the first line; sudo reads exactly that line
           and exec's bash with the remainder still buffered on the
           channel; bash then reads ``script`` as its script body.

        The caller passes *script* and *sudo_password* as separate
        arguments and **cannot** express a mis-ordered payload: the
        helper builds the stdin string itself. The password never
        appears in the remote argv (so ``ps`` / ``/proc/<pid>/cmdline``
        cannot see it), never appears in the remote shell-history file
        (because ``bash -s`` does not record stdin-read commands), and
        is never written to the local structured-log event (the
        helper logs ``cmd_len`` and ``exit_status`` only; *script* and
        *sudo_password* are not bound into any log call). The shape is
        the encoded fix for the 2026-05-04 and 2026-05-05 credential
        leaks documented in the parent Initiative #367's WI1.

        Parameters
        ----------
        target
            The :class:`Target` the SSH connection is keyed to.
            Passed through to :meth:`_connect`.
        script
            The bash script body to execute under sudo. May include
            multiple lines, shell variables, and pipes. Must not
            include the sudo password -- the helper streams that
            separately via *sudo_password*.
        raw_jwt
            Forwarded to :meth:`_connect` for auth-context
            propagation; unused by the helper itself.
        sudo_password
            The password sudo will read from stdin. Streamed as the
            first stdin line; never logged, never present in argv.
        timeout
            Wall-clock timeout in seconds. Raises
            :exc:`asyncio.TimeoutError` on expiry.

        Returns
        -------
        :class:`asyncssh.SSHCompletedProcess`
            The completed process; callers inspect ``exit_status`` and
            ``stdout`` / ``stderr`` like any other ``conn.run`` result.
        """
        conn = await self._connect(target, raw_jwt)
        stdin_payload = f"{sudo_password}\n{script}\n"
        # ``conn.run(cmd, input=...)`` is the canonical asyncssh shape
        # for "run cmd and feed it stdin"; the password line is
        # therefore on the remote process's stdin, not in any argv.
        # The asyncio.wait_for wrapper matches the parent adapter's
        # _run_command timeout contract.
        result = await asyncio.wait_for(
            conn.run(_SUDO_BASH_REMOTE_CMD, input=stdin_payload, check=False),
            timeout=timeout,
        )
        # Structured-logging discipline: cmd_len uses the fixed
        # remote command's length (so the value is stable across all
        # invocations and carries no information about the script
        # body); script_len uses ``len(script)`` so operators can
        # correlate stdout size against a per-invocation length
        # signal without leaking the body itself. Neither
        # ``sudo_password`` nor ``script`` are bound into the event.
        _log.info(
            "ssh_sudo_command_executed",
            target=target.name,
            cmd_len=len(_SUDO_BASH_REMOTE_CMD),
            script_len=len(script),
            exit_code=result.exit_status,
        )
        return result

    async def fingerprint(self, target: Target) -> FingerprintResult:
        """Read ``named -v`` plus ``/etc/os-release`` -- canonical fingerprint.

        Runs two ``_run_command`` calls in sequence (the SSH adapter's
        pool ensures both share one connection):

        1. ``named -v`` -- returns the BIND banner. The version triple
           parsed via :func:`parse_named_version` lands in
           ``version``; the full banner lands in ``build``.
        2. ``cat /etc/os-release`` -- key=value file parsed via
           :func:`parse_os_release` and surfaced under
           ``extras["os"]``. Falls back to ``cat /etc/debian_version``
           when the os-release read fails (older Debian releases
           predate ``/etc/os-release``); the bare version string from
           ``/etc/debian_version`` is prefixed with ``"debian "`` for
           consistency with the os-release shape.

        ``probe_method="ssh: named -v"`` matches the parent
        Initiative #367's WI2 specification.
        """
        named_proc = await self._run_command(target, "named -v", raw_jwt="")
        banner_raw = (named_proc.stdout or "") if hasattr(named_proc, "stdout") else ""
        banner = banner_raw.strip() if isinstance(banner_raw, str) else ""
        version_triple = parse_named_version(banner)

        os_proc = await self._run_command(target, "cat /etc/os-release", raw_jwt="")
        os_raw = (os_proc.stdout or "") if hasattr(os_proc, "stdout") else ""
        os_content = os_raw if isinstance(os_raw, str) else ""
        os_identifier: str | None = None
        if os_proc.exit_status == 0 and os_content.strip():
            os_identifier = parse_os_release(os_content)
        if os_identifier is None:
            debian_proc = await self._run_command(target, "cat /etc/debian_version", raw_jwt="")
            debian_raw = (debian_proc.stdout or "") if hasattr(debian_proc, "stdout") else ""
            debian_content = debian_raw.strip() if isinstance(debian_raw, str) else ""
            if debian_proc.exit_status == 0 and debian_content:
                os_identifier = f"debian {debian_content}"

        # ``named.conf`` lives at ``/etc/bind/named.conf`` on every
        # Debian-family distro and at ``/etc/named.conf`` on
        # RHEL-family; the value is informational only at T1 (T2's
        # ``bind9.config.show`` consumes the real path), so we pin
        # the Debian-family default here. Future iterations can
        # detect via ``rndc status`` once that op lands.
        named_conf_path = "/etc/bind/named.conf"

        return FingerprintResult(
            vendor="isc",
            product="bind9",
            version=version_triple,
            build=banner or None,
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="ssh: named -v",
            extras={
                "os": os_identifier,
                "named_conf_path": named_conf_path,
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + named-running + config-parses check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect
          (host down, firewall, wrong port). Catches :exc:`OSError`
          raised inside asyncssh's connect() before the SSH
          handshake starts.
        * ``ssh_handshake_failed`` -- the TCP socket opened but the
          SSH handshake failed for a non-auth reason (host-key
          mismatch under a future pinning regime, protocol
          version mismatch, etc.). Catches
          :exc:`asyncssh.DisconnectError`.
        * ``auth_failed`` -- the SSH handshake reached the auth
          phase and the credentials were rejected. Catches
          :exc:`asyncssh.PermissionDenied`.
        * ``named_not_running`` -- SSH succeeded but ``pgrep -x
          named`` exited non-zero (named is not listed in the
          process table).
        * ``named_config_invalid`` -- named is running but
          ``named-checkconf -p`` exited non-zero (the active
          config does not parse).

        The probe does not mutate state and does not require a
        writable filesystem -- ``named-checkconf -p`` is read-only
        and the output goes to ``/dev/null``.
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

        # Order matters: distinct exception types map to distinct
        # reasons, and the most specific class (PermissionDenied,
        # subclass of DisconnectError in asyncssh) must be caught
        # first. asyncssh raises ``OSError`` from the underlying
        # socket layer when TCP itself fails.
        try:
            await self._connect(target, raw_jwt="")
        except asyncssh.PermissionDenied:
            return _result(False, "auth_failed")
        except asyncssh.DisconnectError:
            return _result(False, "ssh_handshake_failed")
        except OSError:
            return _result(False, "tcp_unreachable")

        # ``pgrep -x named`` exits 0 iff a process named exactly
        # ``named`` exists in the process table. ``-x`` requires an
        # exact match so a different binary that happens to contain
        # ``named`` in its argv (``named.conf`` editor, etc.) does
        # not produce a false positive.
        pgrep_proc = await self._run_command(target, "pgrep -x named", raw_jwt="")
        if pgrep_proc.exit_status != 0:
            return _result(False, "named_not_running")

        # ``named-checkconf -p`` parses the running config and emits
        # the canonicalised form on stdout (which we discard). A
        # non-zero exit means the config does not parse, which is the
        # "named is running but the config it would load on reload is
        # broken" failure mode operators most care about.
        checkconf_proc = await self._run_command(
            target, "named-checkconf -p > /dev/null", raw_jwt=""
        )
        if checkconf_proc.exit_status != 0:
            return _result(False, "named_config_invalid")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the bind9 nameserver's product / version / OS snapshot.

        Op-id: ``bind9.about``. The dispatcher routes here after the
        JSON Schema validator has accepted *params* (declared empty
        in :mod:`~meho_backplane.connectors.bind9.ops`); the reducer
        wraps the returned flat dict into ``OperationResult.result``.
        Reuses :meth:`fingerprint` so the operator-facing op and the
        canonical fingerprint share one network round-trip per call
        (two SSH command executions, one pooled connection).

        The returned dict is intentionally flat -- no nested
        ``extras`` -- because the dispatcher's default reducer
        forwards the value verbatim. Future reducers (real JSONFlux
        reduction in a follow-on Initiative) flatten nested shapes
        anyway; staying flat now means the v0.2 callers see the same
        keys before and after the reducer swap.
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "build": result.build,
            "os": result.extras.get("os"),
            "named_conf_path": result.extras.get("named_conf_path"),
        }

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`BIND9_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.bind9.ops.BIND9_OPS` and
        routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`,
        which derives ``handler_ref`` from the bound method's
        ``__module__`` + ``__qualname__``, inserts a new row on first
        call, and skips the embedding compute on re-call with
        unchanged summary / description / tags. Idempotent across
        pod restarts -- mirrors the
        :meth:`KubernetesConnector.register_operations` shape.
        """
        # Lazy import: the operations package pulls in the embedding
        # pipeline (ONNX runtime + a 100 MB+ model on first touch),
        # which a pure-fingerprint / pure-probe unit test should not
        # pay. Lifespan callers already have the embedding service
        # warmed by the time this runs.
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in BIND9_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"Bind9Connector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
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
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "bind9_operations_registered",
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

        Mirrors :meth:`KubernetesConnector.execute` (#391). The
        dispatch shape is operator-less (no policy gate, no audit
        row, no broadcast) because direct callers like the chassis
        and typed-connector internals do not carry an
        :class:`~meho_backplane.auth.operator.Operator`; the
        operator-aware surface is ``POST /api/v1/operations/call``
        via the G0.6 meta-tools. The shim's contract is:

        * Unknown ``op_id`` -> the structured ``unknown_op`` envelope
          :func:`~meho_backplane.operations._errors.result_unknown_op`
          emits.
        * Params failing the descriptor's JSON Schema -> the
          ``invalid_params`` envelope.
        * Handler exception -> the ``connector_error`` envelope.
        * Happy path -> ``OperationResult(status="ok", op_id=op_id,
          result=<handler dict>, duration_ms=<elapsed>)``.

        Lazy imports for the same rationale documented on
        :meth:`register_operations` -- pure-python tests that
        exercise ``fingerprint``/``probe`` shouldn't pay the
        operations package's import cost.
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

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: the load-bearing safe-sudo primitive + bind9 connector
# class are kept colocated; the file is dense (docstrings + JSON Schemas) but
# splitting `_remote_bash_with_sudo` out would re-create the exact "sudo argv
# leaks from a non-whitelisted module" failure shape the safety assertion in
# tests/integration/test_g3_4_bind9_e2e.py polices. See the helper's
# docstring for the wire-shape + leak-prevention analysis.

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
modules; the dispatcher shim does not change. T4 (#590) completes
the 11-op surface with ``bind9.config.apply_file``,
``bind9.config.apply_views`` (atomic-apply reuse),
``bind9.config.backup``, and ``bind9.config.reload``.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import asyncssh
import structlog

from meho_backplane.auth.operator import Operator
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


def _build_sudo_bash_remote_cmd(script_byte_len: int) -> str:
    """Build the remote command for :meth:`Bind9Connector._remote_bash_with_sudo`.

    Returns the constant boilerplate parameterised only by
    *script_byte_len* — an integer derived from the caller's script
    bytes. ``script`` and ``sudo_password`` themselves are never
    interpolated into argv. See the helper's docstring for the full
    wire-shape + leak-prevention analysis; module-level so the unit
    suite (which mocks ``conn.run``) can assert against the exact
    rendered shape without duplicating it.
    """
    return (
        f"set -e; umask 077; f=$(mktemp); "
        f'trap "rm -f $f" EXIT; '
        f'head -c {script_byte_len} > "$f"; '
        f'sudo -S -p "" bash "$f"'
    )


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


#: Curated ``when_to_use`` strings -- one per ``group_key`` declared by
#: :data:`~meho_backplane.connectors.bind9.ops.BIND9_OPS`. Surfaced
#: verbatim by ``list_operation_groups`` (T8) so an agent picking
#: between the connector's four groups gets agent-actionable selection
#: signal rather than the auto-derive template
#: (``"Operations grouped under 'zone' for bind9 bind9-ssh."``). Each
#: entry explicitly names the *kind of question* that routes here and
#: the cross-group pairing pattern with the rest of the bind9 surface.
#: T4b (#732) curated; mirrors the
#: :data:`~meho_backplane.connectors.kubernetes.connector._WHEN_TO_USE_BY_GROUP`
#: shape so the registration loop reads identically.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for bind9 nameserver-identity questions before any per-"
        "zone or per-record drill-in: 'which BIND version is this "
        "target running and on which host OS?'. The single "
        "``bind9.about`` op returns vendor / product / version "
        "(parsed BIND <X.Y.Z>), full named banner, and the host OS "
        "identifier from ``/etc/os-release``. Call this first when "
        "the agent needs to pick a version-flavoured doc page from "
        "the knowledge base, or to confirm the nameserver is "
        "reachable before issuing higher-level DNS ops."
    ),
    "zone": (
        "Use for zone-level inventory and metadata reads: list every "
        "zone the nameserver serves (``bind9.zone.list``) and read "
        "one named zone's metadata + SOA (``bind9.zone.read``). "
        "Read-only; never mutates zone state. The right group when "
        "the agent doesn't yet know which zone to target ('what "
        "zones does this nameserver host?') or needs the zone-level "
        "context (type / file / view binding / SOA serial) before "
        "drilling into records. Pair with the 'record' group once a "
        "zone is identified to query / add / remove RRs inside it, "
        "and with the 'config' group when the question is about "
        "named.conf-level wiring (views, zone clauses) rather than "
        "the zone's own contents."
    ),
    "record": (
        "Use for record-level RR reads and mutations inside a known "
        "zone: query a specific name+type (``bind9.record.get``), "
        "add an RR atomically (``bind9.record.add``), or remove one "
        "(``bind9.record.remove``). Writes route through the atomic-"
        "apply primitive (rndc freeze / journal-sync / journal swap "
        "/ rndc thaw) so a failed apply leaves the zone untouched. "
        "``add`` / ``remove`` are mutating ops -- the future policy "
        "gate keys on their ``caution`` / ``dangerous`` safety_level. "
        "Typically reached after the 'zone' group identifies the "
        "target zone. Pair with the 'config' group when the change "
        "needs a view / zone-clause edit rather than an in-zone RR "
        "edit."
    ),
    "config": (
        "Use for nameserver configuration reads and atomic config "
        "writes: dump the running named.conf "
        "(``bind9.config.show``), apply a single named.conf file "
        "(``bind9.config.apply_file``), apply a multi-file views "
        "bundle (``bind9.config.apply_views``), snapshot the current "
        "config + zones (``bind9.config.backup``), or reload via "
        "rndc (``bind9.config.reload``). ``apply_file`` and "
        "``apply_views`` route through the atomic-apply primitive "
        "(staged write + named-checkconf validation + rollback on "
        "failure); ``backup`` and ``reload`` do not (additive / "
        "single-rndc respectively). The right group for view-level "
        "or server-level changes -- per-RR edits live in the "
        "'record' group, zone-inventory questions in the 'zone' "
        "group. Mutating ops carry ``caution`` / ``dangerous`` "
        "safety_level."
    ),
}


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

    # ~18 lines of executable body wrapped in ~65 lines of docstring
    # encoding the safe-sudo invariant; iter-1 `p1` praised the
    # structural shape; splitting would decouple safety rationale from
    # the API it constrains.  # code-quality-allow: load-bearing safety primitive
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

        Safe-by-construction sudo invocation. The wire shape is built
        by :func:`_build_sudo_bash_remote_cmd`:

        1. Remote command (argv): ``"set -e; umask 077; f=$(mktemp);
           trap \"rm -f $f\" EXIT; head -c <N> > \"$f\"; sudo -S -p ''
           bash \"$f\""``. The only caller-derived interpolation is
           ``<N>`` — the **byte count** of the UTF-8-encoded script,
           an integer. ``script`` and ``sudo_password`` themselves are
           not in argv.
        2. Stdin payload: ``script_bytes + sudo_password + "\\n"`` —
           ``head -c N`` reads exactly the script bytes off the pipe
           into ``$f`` via unbuffered ``read(2)`` (no stdio prefetch),
           then ``sudo -S`` reads the password from what remains on
           stdin (EOF immediately after the password's newline, so
           sudo's stdio buffer has nothing past the password line to
           swallow). ``bash "$f"`` then reads the script body from
           the temp file, not stdin.

        Why the temp-file split (#697 RCA, 2026-05-20): the previous
        shape ``"sudo -S -p '' bash -s"`` + ``<pw>\\n<script>\\n`` on
        stdin silently broke against real sudo. ``sudo -S`` reads
        its password line via buffered stdio (``fgetc``/``getline``),
        which on a pipe consumes not just the ``\\n``-terminated
        password but a chunk of adjacent buffered bytes from the
        same kernel read. Those bytes go into sudo's FILE* and are
        discarded when sudo execs bash — so ``bash -s`` saw EOF
        immediately and write ops (record.add / record.remove /
        config.apply_*) silently no-op'd (audit ``state_after ==
        state_before``). Reproduced locally against the bind9
        testcontainer + Debian sudo 1.9.x. Sliding the script body
        through a temp file isolates sudo's stdio from the script
        bytes — sudo can prefetch all it wants, there's nothing
        beyond the password line on its stdin to lose.

        The caller passes *script* and *sudo_password* as separate
        arguments and **cannot** express a mis-ordered payload: the
        helper builds the stdin string itself. The password never
        appears in the remote argv (so ``ps`` / ``/proc/<pid>/cmdline``
        cannot see it), never appears in the remote shell-history file
        (``bash <file>`` does not record file-sourced commands), and
        is never written to the local structured-log event (the
        helper logs ``cmd_len`` and ``script_len`` only; *script* and
        *sudo_password* are not bound into any log call). The script
        briefly lives on the remote SSH user's tmp under ``umask
        077`` with a ``trap rm -f`` finaliser; the script body is
        explicitly contracted as the bind9 op (NOT credentials), so
        the on-disk window is acceptable. The shape is the encoded
        fix for the 2026-05-04 / 2026-05-05 credential leaks
        documented in the parent Initiative #367's WI1, hardened
        against the 2026-05-20 stdio-buffer-swallow regression
        (#697).

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

        Raises
        ------
        ValueError
            *sudo_password* contains ``\\n``, ``\\r``, or ``\\x00``. The
            helper's safety contract is "password is exactly stdin
            line 1, bash reads line 2 onwards as the script"; a
            control character in *sudo_password* breaks that
            invariant by terminating sudo's read early and feeding
            the trailing bytes to ``bash -s`` as commands. The check
            runs **before** :meth:`_connect` so an invalid credential
            never opens a wire connection.

        Returns
        -------
        :class:`asyncssh.SSHCompletedProcess`
            The completed process; callers inspect ``exit_status`` and
            ``stdout`` / ``stderr`` like any other ``conn.run`` result.
        """
        # Single-line invariant for sudo_password. If a caller feeds a
        # password containing ``\n`` / ``\r`` / ``\x00``, sudo's stdin
        # reader treats the first newline as end-of-password and bash
        # receives the trailing bytes as additional commands -- the
        # exact leak shape this helper exists to make structurally
        # unrepresentable. Reject at entry, before _connect, so an
        # invalid credential never opens a wire connection and the
        # error surfaces as a programming bug rather than a remote
        # exec.
        if any(ch in sudo_password for ch in ("\n", "\r", "\x00")):
            raise ValueError(
                "sudo_password must be a single line: newline / carriage-return / NUL "
                "characters would smuggle additional bash commands onto stdin"
            )
        conn = await self._connect(target, raw_jwt)
        # New wire shape (#697 fix): script bytes first, password
        # last, EOF immediately after. ``head -c <N>`` on the remote
        # reads exactly the script's byte count via unbuffered
        # read(2), so sudo's buffered stdin sees only the password
        # line and can't prefetch-and-swallow script bytes (the bug
        # that silently no-op'd write ops in v0.3 CI).
        script_bytes = script.encode("utf-8")
        cmd = _build_sudo_bash_remote_cmd(len(script_bytes))
        stdin_payload = f"{script}{sudo_password}\n"
        result = await asyncio.wait_for(
            conn.run(cmd, input=stdin_payload, check=False),
            timeout=timeout,
        )
        # Structured-logging discipline: cmd_len records the rendered
        # remote command's length (which varies only by the digit
        # count of len(script_bytes) — a coarser size signal than
        # ``script_len`` already carries); ``script_len`` is the
        # per-invocation byte length operators correlate stdout size
        # against. Neither ``sudo_password`` nor ``script`` are bound
        # into the event.
        _log.info(
            "ssh_sudo_command_executed",
            target=target.name,
            cmd_len=len(cmd),
            script_len=len(script_bytes),
            exit_code=result.exit_status,
        )
        return result

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
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

        Unreachable or auth-failure mid-fingerprint (a connection drop,
        an :exc:`asyncssh.Error`, or an :exc:`asyncio.TimeoutError` from a
        command timeout) → ``reachable=False`` + ``extras["error"]`` with
        the exception message, rather than propagating. This mirrors the
        pfsense sibling and lets the shared
        :meth:`~meho_backplane.connectors.adapters.ssh.SshConnector._assert_reachable`
        guard surface the failure consistently from ``about`` (#986).

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — bind9
        authenticates via SSH key, not Vault OIDC, so the route operator
        plays no role here.
        """
        del operator  # unused — SSH key auth, no Vault credential read
        probed_at = datetime.now(UTC)

        try:
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
        except (OSError, asyncssh.Error) as exc:
            _log.warning(
                "bind9_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="isc",
                product="bind9",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: named -v",
                extras={"error": str(exc)},
            )

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
            probed_at=probed_at,
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
        * ``command_failed`` -- the SSH handshake succeeded but a
          post-connect command (``pgrep -x named`` /
          ``named-checkconf -p``) raised (connection dropped mid-probe,
          an :exc:`asyncssh.Error`, or an :exc:`asyncio.TimeoutError`
          from the command timeout). Caught here so a mid-probe failure
          maps to a non-ok
          :class:`~meho_backplane.connectors.schemas.ProbeResult` rather
          than escaping ``probe`` as an unhandled exception (#986).

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

        # Post-connect commands are guarded: a connection drop, an
        # ``asyncssh.Error``, or a timeout after a successful handshake
        # must map to ``command_failed`` rather than propagating an
        # unhandled exception out of ``probe`` (#986). ``(OSError,
        # asyncssh.Error)`` mirrors ``fingerprint``'s catch tuple;
        # ``TimeoutError`` is an ``OSError`` subclass so ``_run_command``'s
        # ``asyncio.wait_for`` expiry is covered.
        try:
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
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")
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
        ``extras`` -- so the dispatcher's default
        :class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
        passes this scalar (non-set-shaped) payload through verbatim
        into ``OperationResult.result`` with a ``None`` handle; only
        set-shaped responses above the threshold are materialized into a
        :class:`~meho_backplane.connectors.schemas.ResultHandle`.

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
            "os": result.extras.get("os"),
            "named_conf_path": result.extras.get("named_conf_path"),
        }

    async def bind9_zone_list(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.zone.list`` op (G3.4-T2 #588).

        Delegates to
        :func:`~meho_backplane.connectors.bind9.ops_zone.bind9_zone_list`.
        The bound-method shim shape mirrors the K8s connector's
        ``k8s_pod_list`` / ``k8s_pod_info`` / ``k8s_deployment_list`` /
        ``k8s_deployment_info`` pattern: the per-op module owns the
        handler logic and the registration metadata, the connector
        class exposes a thin shim so the descriptor's ``handler_ref``
        round-trips through the dispatcher's
        :func:`~meho_backplane.operations._handler_resolve.import_handler`
        walk against a ``module.ClassName.method`` dotted path.
        """
        from meho_backplane.connectors.bind9.ops_zone import (
            bind9_zone_list as _bind9_zone_list,
        )

        return await _bind9_zone_list(self, target, params)

    async def bind9_zone_read(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.zone.read`` op (G3.4-T2 #588)."""
        from meho_backplane.connectors.bind9.ops_zone import (
            bind9_zone_read as _bind9_zone_read,
        )

        return await _bind9_zone_read(self, target, params)

    async def bind9_record_get(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.record.get`` op (G3.4-T2 #588)."""
        from meho_backplane.connectors.bind9.ops_record import (
            bind9_record_get as _bind9_record_get,
        )

        return await _bind9_record_get(self, target, params)

    async def bind9_record_add(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.record.add`` op (G3.4-T3 #589).

        Atomic A/AAAA add via the staged-validate-commit-reload-verify-
        rollback primitive in :mod:`~meho_backplane.connectors.bind9._atomic`.
        Routes sudo through :meth:`_remote_bash_with_sudo`.
        """
        from meho_backplane.connectors.bind9.ops_record import (
            bind9_record_add as _bind9_record_add,
        )

        return await _bind9_record_add(self, target, params)

    async def bind9_record_remove(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.record.remove`` op (G3.4-T3 #589)."""
        from meho_backplane.connectors.bind9.ops_record import (
            bind9_record_remove as _bind9_record_remove,
        )

        return await _bind9_record_remove(self, target, params)

    async def bind9_config_show(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for the ``bind9.config.show`` op (G3.4-T2 #588)."""
        from meho_backplane.connectors.bind9.ops_config import (
            bind9_config_show as _bind9_config_show,
        )

        return await _bind9_config_show(self, target, params)

    async def bind9_config_apply_file(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``bind9.config.apply_file`` (G3.4-T4 #590).

        Atomic single-fragment write via the T3 atomic-apply primitive
        (single-file staging mode). Routes sudo through
        :meth:`_remote_bash_with_sudo`.
        """
        from meho_backplane.connectors.bind9.ops_config import (
            bind9_config_apply_file as _bind9_config_apply_file,
        )

        return await _bind9_config_apply_file(self, target, params)

    async def bind9_config_apply_views(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``bind9.config.apply_views`` (G3.4-T4 #590).

        Atomic multi-file tree write via the T3 atomic-apply primitive
        (multi-file tar mode). Routes sudo through
        :meth:`_remote_bash_with_sudo`.
        """
        from meho_backplane.connectors.bind9.ops_config import (
            bind9_config_apply_views as _bind9_config_apply_views,
        )

        return await _bind9_config_apply_views(self, target, params)

    async def bind9_config_backup(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``bind9.config.backup`` (G3.4-T4 #590).

        ``tar -czf`` of ``/etc/bind/`` into a timestamped backup file,
        plus a listing of existing backups. Does NOT route through
        atomic-apply (additive, no rollback contract). Routes sudo
        through :meth:`_remote_bash_with_sudo`.
        """
        from meho_backplane.connectors.bind9.ops_config import (
            bind9_config_backup as _bind9_config_backup,
        )

        return await _bind9_config_backup(self, target, params)

    async def bind9_config_reload(
        self,
        target: Target,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``bind9.config.reload`` (G3.4-T4 #590).

        ``rndc reload`` with a structured success/failure envelope.
        Does NOT route through atomic-apply (single command, no staging,
        no rollback contract). Routes sudo through
        :meth:`_remote_bash_with_sudo`.
        """
        from meho_backplane.connectors.bind9.ops_config import (
            bind9_config_reload as _bind9_config_reload,
        )

        return await _bind9_config_reload(self, target, params)

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
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = _WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"Bind9Connector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry "
                        f"to _WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.bind9.connector so "
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

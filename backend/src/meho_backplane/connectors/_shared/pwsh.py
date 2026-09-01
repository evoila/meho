# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared PowerShell-over-SSH transport for the Microsoft-estate connectors.

Several MEHO connectors control hosts that expose no REST API and are
driven entirely through PowerShell cmdlets run over the pooled SSH
connection
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector` maintains.
The wire shape — base64 UTF-16LE ``-EncodedCommand`` + ``ConvertTo-Json``
stdout parsed with the stdlib :mod:`json` — is identical across all of
them, and the code that must stay bug-for-bug identical (secret-hygienic
logging, the no-BOM UTF-16LE encoding, the CLIXML-strip net, the bounded
structured error) is exactly the code that must never drift between
copies.

Why this lives in ``_shared`` (and bind9's ``_atomic`` does not)
----------------------------------------------------------------

MEHO's convention is that a per-connector transport helper stays
package-local until **three or more** connectors consume it: two copies
is a defensible per-connector-transport convention, more is not. This
helper crossed that line. Holodeck (#853, ``pwsh``/PS7 on the Photon-OS
HoloRouter appliance) established the transport; windows_dns (#2760,
Windows PowerShell 5.1 on an AD-DNS host) copied it — and its ``_pwsh``
header flagged the shared hoist as future work (#2759); the
Microsoft-estate initiative (#3259) adds four more (winsrv / msad / wsfc
/ hyperv). Six consumers of one wire shape is over the line, so #3260
executes the hoist. bind9's ``_atomic`` backup-validate-reload helper
stays package-local by the same rule inverted: it has exactly one
consumer, so sharing it would be premature.

Per-connector variation
------------------------

The transport is one wire shape parameterised by three optional
class-level constants read off the connector. Defaults reproduce the
Holodeck original, so a connector that declares none behaves exactly as
Holodeck's hard-coded transport did:

* ``POWERSHELL_EXECUTABLE`` — ``pwsh`` (PS7, the default) vs
  ``powershell`` (Windows PowerShell 5.1). A Windows Server host ships
  ``powershell`` but not ``pwsh`` by default; the Photon-OS HoloRouter
  ships ``pwsh``. Every real connector sets this explicitly; the default
  is only the fallback for an unset connector.
* ``POWERSHELL_SCRIPT_PREFIX`` — a string prepended to every script
  before encoding (default ``""``). windows_dns sets
  ``$ProgressPreference = 'SilentlyContinue'; `` so the DnsServer
  module's first-use progress never CLIXML-serialises onto the streams.
* ``POWERSHELL_LOG_EVENT`` — the structured-log event name (default
  ``pwsh_executed``); each connector names its own so log queries stay
  connector-scoped.

The seams are read via :func:`type` (the connector *class*, not the
instance) so a bare test double — a :class:`~unittest.mock.MagicMock`
with no such class attribute — resolves to the documented default rather
than an auto-vivified child mock, keeping the transport exercisable with
a plain mock connector.

Wire shape
----------

1. The PowerShell text the caller hands in (after the connector's
   ``POWERSHELL_SCRIPT_PREFIX`` is prepended) is UTF-16LE-encoded and
   base64'd into the form ``-EncodedCommand`` expects. Per Microsoft's
   ``about_pwsh`` reference the encoded payload is the base64 of the
   UTF-16LE bytes of the script, no BOM. ``<exe> -EncodedCommand
   <encoded>`` is the supported portable form for sending multi-line
   PowerShell through a single-argument transport — it sidesteps the
   shell's quoting rules entirely.
2. The caller pipes the cmdlet's output through ``| ConvertTo-Json``
   (with ``-Compress`` and an explicit ``-Depth`` — see
   :data:`PWSH_DEFAULT_DEPTH` — when the response tree is deeper than
   two levels). JSON output parses with the stdlib :mod:`json` without
   pulling in a CliXml dependency.
3. :func:`pwsh_run` runs ``<exe> -NoProfile -NonInteractive
   -EncodedCommand <encoded>`` over the pooled SSH connection, strips any
   CLIXML warning/error block that leaked onto stdout (see
   :func:`strip_clixml`), then decodes the remaining ``stdout`` via
   :func:`json.loads` and returns the parsed structure.
4. Failures surface as a single structured :exc:`PwshRunError` that
   carries the exit status and a truncated stderr fragment but never
   embeds the original script body or any auth material.

Safety contract
---------------

* The encoded base64 payload **does** appear on the remote process argv
  (that's the contract of ``-EncodedCommand``). The PowerShell script
  body the caller hands in is therefore visible to a privileged observer
  on the remote host; this is acceptable because the cmdlet scripts are
  deterministic (no per-call credentials embedded in the script — the
  SSH credential is the only secret and it never reaches the script
  text).
* Logging emits ``script_len`` (an integer, the UTF-8 byte count of the
  operator-supplied script) and ``exit_status`` only. Neither the
  original script, the encoded payload, nor any field of
  ``target.secret_ref`` is bound into any log event. The structured
  error mirrors this: ``script`` and ``encoded`` are never written into
  the error's user-visible attributes.
* Callers must not pass credential material in ``script``. The helper
  cannot enforce this because the script is opaque to it; the rule is
  encoded in each connector's per-op handlers, none of which interpolate
  secrets into the PowerShell text they assemble.

Cited references
----------------

* PowerShell ``-EncodedCommand`` / ``about_pwsh``:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh
* PowerShell ``ConvertTo-Json``:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import structlog

__all__ = [
    "PWSH_DEFAULT_DEPTH",
    "PwshRunError",
    "encode_pwsh_command",
    "ps_single_quote",
    "pwsh_run",
    "strip_clixml",
]

_log = structlog.get_logger(__name__)

#: Default ``ConvertTo-Json -Depth`` value the helper recommends in its
#: ``script`` argument. The PowerShell built-in defaults to 2, which
#: silently truncates deeply nested cmdlet output (DnsServer resource
#: records nest ``RecordData`` one level deep; ``Get-Service`` nests
#: ``RequiredServices``). We surface an explicit ``-Depth 4`` as a
#: constant so each caller doesn't reinvent the value.
PWSH_DEFAULT_DEPTH: int = 4

#: Maximum stderr fragment length retained on a :class:`PwshRunError`.
#: Stderr from ``pwsh`` / ``powershell`` can be long (multi-line stack
#: traces, ``Write-Error`` records, the CLIXML progress serialisation
#: Windows PowerShell emits on first module load); a hard cap keeps
#: log/audit payloads bounded and avoids any chance of secret-shaped
#: substrings hidden deeper in the output bleeding into error surfaces.
_STDERR_MAX_LEN: int = 4096

#: Fallback PowerShell executable when the connector declares no
#: ``POWERSHELL_EXECUTABLE``. ``pwsh`` (PS7) is the Holodeck original;
#: Windows-estate connectors override it to ``powershell`` (Windows
#: PowerShell 5.1, the shell present on a Windows Server host).
_DEFAULT_PS_EXECUTABLE: str = "pwsh"

#: Fallback structured-log event name when the connector declares no
#: ``POWERSHELL_LOG_EVENT``.
_DEFAULT_LOG_EVENT: str = "pwsh_executed"


class PwshRunError(RuntimeError):
    """Structured failure from :func:`pwsh_run`.

    Carried fields:

    * ``exit_status`` — the PowerShell process exit code (``int``).
      Non-zero indicates the cmdlet itself failed; ``None`` indicates
      ``-EncodedCommand`` produced output that :mod:`json` could not
      parse.
    * ``stderr`` — the (truncated) stderr fragment. Capped at
      :data:`_STDERR_MAX_LEN` characters so structured-log payloads
      stay bounded.

    The original script, the encoded base64 payload, and any
    ``target.secret_ref`` field are intentionally **not** retained on
    the exception — the dispatcher's ``connector_error`` envelope
    surfaces ``str(exc)`` to the operator and any of those substrings
    would be a leak.
    """

    def __init__(self, message: str, *, exit_status: int | None, stderr: str) -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stderr = stderr[:_STDERR_MAX_LEN]


#: One CLIXML block PowerShell can prepend to stdout when a warning / error
#: record is serialised under ``-EncodedCommand``: the literal ``#< CLIXML``
#: preamble followed by a single ``<Objs>...</Objs>`` document. The pattern
#: is non-greedy to the first ``</Objs>`` so multiple stacked blocks (one per
#: warning) each match their own document, and ``DOTALL`` lets the ``<Objs>``
#: body span the newlines the serialiser emits. The trailing ``\s*`` also
#: consumes the newline the serialiser writes after each block, so a JSON
#: payload trailing the block comes back clean and a warning-only stdout
#: strips to ``""``. ``ConvertTo-Json`` output never contains ``</Objs>``, so
#: the payload itself always survives.
_CLIXML_BLOCK_RE: re.Pattern[str] = re.compile(r"#<\s*CLIXML\b.*?</Objs>\s*", re.DOTALL)


def strip_clixml(stdout: str) -> str:
    """Remove PowerShell CLIXML warning/error blocks polluting *stdout* (#3081).

    ``-EncodedCommand`` can serialise a warning or error record as a
    ``#< CLIXML`` preamble + ``<Objs>...</Objs>`` document written to stdout
    ahead of the real ``ConvertTo-Json`` payload (the ``HoloDeck`` module's
    "unapproved verbs" import warning, or a Windows PowerShell module's
    first-use progress, are the motivating cases). Left in place it makes
    :func:`json.loads` fail on otherwise-valid output. This removes every
    such block, leaving the JSON payload — or, when the cmdlet emitted
    *only* a warning, an empty string that the caller's empty-stdout guard
    turns into an honest failure.

    Public so the unit suite can assert the strip against fixture text without
    driving the whole :func:`pwsh_run` transport. A ``stdout`` with no CLIXML
    marker is returned unchanged (cheap fast-path — the common healthy case).
    """
    if "CLIXML" not in stdout:
        return stdout
    return _CLIXML_BLOCK_RE.sub("", stdout)


def encode_pwsh_command(script: str) -> str:
    """Encode *script* per ``-EncodedCommand``'s contract.

    The contract (Microsoft docs, ``about_pwsh``): the value following
    ``-EncodedCommand`` is the base64 of the UTF-16LE bytes of the
    PowerShell script. No BOM, no surrounding whitespace, no trailing
    newline.

    Example::

        >>> encode_pwsh_command("Get-Service | ConvertTo-Json")
        'RwBlAHQALQBTAGUAcgB2AGkAYwBlACAAfAAgAEMAbwBuAHYAZQByAHQAVABvAC0ASgBzAG8AbgA='

    Public so the unit suite can assert the encoding round-trips against
    the documented convention without re-deriving it inside the test.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def ps_single_quote(value: str) -> str:
    """Return *value* as a safe single-quoted PowerShell string literal.

    Inside a single-quoted PowerShell string the only special character
    is ``'`` itself; PowerShell escapes it by doubling. Single-quoted
    strings are literal (no ``$`` expansion), so this is the safe way to
    embed an operator-supplied non-secret scalar (a zone name, a
    description, an instance id) into a composed ``-EncodedCommand``
    script without it being parsed as a variable or subexpression. This
    is the PowerShell analogue of :func:`shlex.quote`.
    """
    return "'" + value.replace("'", "''") + "'"


def _connector_seam(connector: Any, attr: str, default: str) -> str:
    """Read an optional string transport seam off *connector*'s class.

    The transport seams (``POWERSHELL_EXECUTABLE`` /
    ``POWERSHELL_SCRIPT_PREFIX`` / ``POWERSHELL_LOG_EVENT``) are
    class-level constants. Reading them via :func:`type` (not the
    instance) means a bare test double — a
    :class:`~unittest.mock.MagicMock` instance with no such class
    attribute — resolves to *default* instead of an auto-vivified child
    mock, so the transport stays exercisable with a plain mock connector.
    """
    return getattr(type(connector), attr, default)


async def pwsh_run(
    connector: Any,
    target: Any,
    script: str,
    *,
    operator: Any = None,
    depth: int = PWSH_DEFAULT_DEPTH,
    timeout: float = 30.0,
) -> Any:
    """Run *script* on *target* via ``-EncodedCommand``; parse JSON output.

    *connector* is an
    :class:`~meho_backplane.connectors.adapters.ssh.SshConnector` whose
    ``_run_command`` seam (pooling + auth) the helper reuses and whose
    ``POWERSHELL_EXECUTABLE`` / ``POWERSHELL_SCRIPT_PREFIX`` /
    ``POWERSHELL_LOG_EVENT`` class constants drive the per-connector wire
    variation (see the module docstring). *target* is passed through to
    ``_run_command``. *script* is the PowerShell body; callers pipe its
    output through ``| ConvertTo-Json`` (the helper does not append the
    conversion — some ops build multi-statement scripts where only the
    final pipeline produces JSON). *operator* is forwarded for the
    operator-context Vault read on an SSH pool miss (``None`` fails
    closed). *depth* is advisory (see :data:`PWSH_DEFAULT_DEPTH`);
    *timeout* is the wall-clock second budget forwarded to
    ``_run_command``.

    Returns the parsed JSON payload (typically a ``dict`` or ``list``).
    Raises :class:`PwshRunError` when the process exits non-zero or stdout
    does not parse as JSON — ``exit_status`` carries the integer exit on a
    non-zero exit, ``None`` when the JSON parse failed.
    """
    del depth  # advisory — see docstring
    executable = _connector_seam(connector, "POWERSHELL_EXECUTABLE", _DEFAULT_PS_EXECUTABLE)
    prefix = _connector_seam(connector, "POWERSHELL_SCRIPT_PREFIX", "")
    log_event = _connector_seam(connector, "POWERSHELL_LOG_EVENT", _DEFAULT_LOG_EVENT)

    full_script = prefix + script
    encoded = encode_pwsh_command(full_script)
    cmd = f"{executable} -NoProfile -NonInteractive -EncodedCommand {encoded}"

    proc = await connector._run_command(target, cmd, operator=operator, timeout=timeout)

    stdout: str = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stderr: str = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    if not isinstance(stdout, str):
        stdout = ""
    if not isinstance(stderr, str):
        stderr = ""
    # Strip any CLIXML warning/error block the shell serialised onto stdout
    # ahead of the JSON payload (#3081) before the empty-check and json.loads
    # below. A no-op fast-path when no CLIXML marker is present, so a
    # connector that suppresses the warning at source pays nothing.
    stdout = strip_clixml(stdout)
    exit_status: int | None = getattr(proc, "exit_status", None)

    # Structured logging discipline (mirrors the SSH adapter's
    # ``ssh_command_executed`` event): only sizes + exit code; never the
    # script body, never the encoded payload, never anything that could
    # carry credential bytes. ``script_len`` is the caller-supplied
    # script's byte length (excluding any prefix the transport prepended).
    _log.info(
        log_event,
        target=getattr(target, "name", None),
        script_len=len(script.encode("utf-8")),
        encoded_len=len(encoded),
        exit_status=exit_status,
    )

    if exit_status is None or exit_status != 0:
        raise PwshRunError(
            f"{executable} -EncodedCommand exited with status {exit_status!r}",
            exit_status=exit_status,
            stderr=stderr,
        )

    if not stdout.strip():
        raise PwshRunError(
            f"{executable} -EncodedCommand produced empty stdout; expected ConvertTo-Json output",
            exit_status=exit_status,
            stderr=stderr,
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # ``json.loads`` raises with the offending bytes in its message;
        # :class:`PwshRunError` re-words to 'didn't parse' so the
        # operator-visible surface never carries the raw payload.
        raise PwshRunError(
            f"{executable} -EncodedCommand stdout was not valid JSON; "
            "expected ConvertTo-Json output",
            exit_status=exit_status,
            stderr=stderr,
        ) from None

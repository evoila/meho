# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PowerShell-over-SSH helper for the windows_dns connector.

A Windows AD-DNS server exposes no REST API for record management; every
``DnsServer`` cmdlet (``Get-DnsServerZone``,
``Add-DnsServerResourceRecordA``, ``Remove-DnsServerResourceRecord`` …)
runs through ``pwsh`` / ``powershell`` on the Windows host, reached via
the pooled SSH connection
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector` maintains
(Windows Server ships an OpenSSH server; the default shell can be set to
``pwsh``). This module is the **single** seam between the connector's
Python handlers and that PowerShell surface; every fingerprint / probe /
op handler in the package routes its script through :func:`pwsh_run`.

This helper is a faithful sibling of
:mod:`meho_backplane.connectors.holodeck._pwsh` — the Holodeck connector
established the PowerShell-over-SSH transport (base64 UTF-16LE
``-EncodedCommand`` + stdlib :mod:`json` parse) and this module reuses
that exact wire shape for the Windows DNS role. It is kept package-local
(rather than importing Holodeck's private ``_pwsh`` module) so the
package mirrors the repo's per-connector-transport-helper convention
(bind9 owns ``_atomic``; Holodeck owns ``_pwsh``); a future refactor may
hoist a shared ``_pwsh`` into ``_shared`` (noted in #2759).

Wire shape
----------

1. The PowerShell text the caller hands in is UTF-16LE-encoded and
   base64'd into the form ``pwsh`` expects for ``-EncodedCommand``.
   Per Microsoft's about_pwsh reference: the encoded payload is the
   base64 of the UTF-16LE bytes of the script, no BOM.
   ``pwsh -EncodedCommand <encoded>`` is the supported portable form
   for sending multi-line PowerShell through a single-argument
   transport — it sidesteps the shell's quoting rules entirely, which
   is why it is preferred over ``-Command "..."`` for any script that
   interpolates operator-supplied values.
2. The caller is responsible for piping the cmdlet's output through
   ``| ConvertTo-Json`` (with ``-Compress`` and an explicit ``-Depth``
   when the response tree is deeper than two levels). Json output parses
   with the stdlib :mod:`json` without pulling in a CliXml dependency.
3. :func:`pwsh_run` runs ``<executable> -NoProfile -NonInteractive
   -EncodedCommand <encoded>`` over the pooled SSH connection and decodes
   ``stdout`` via :func:`json.loads`, returning the parsed structure. The
   executable defaults to ``powershell`` (Windows PowerShell 5.1, per
   :data:`_DEFAULT_PS_EXECUTABLE`) -- **not** ``pwsh`` -- because a Windows
   AD-DNS host has Windows PowerShell but not PS7 by default; a connector
   overrides it with a ``POWERSHELL_EXECUTABLE`` class attr. A
   ``$ProgressPreference = 'SilentlyContinue'`` guard is prepended so the
   ``DnsServer`` module's first-use progress never pollutes the streams.
4. Failures surface as a single structured :exc:`PwshRunError` that
   carries the exit status and a truncated stderr fragment but never
   embeds the original script body or any auth material.

Safety contract
---------------

* The encoded base64 payload **does** appear on the remote process
  argv (that's the contract of ``-EncodedCommand``). The PowerShell
  script body the caller hands in is therefore visible to a privileged
  observer on the remote host; this is acceptable because the DnsServer
  ops are deterministic (no per-call credentials embedded in the
  script — the SSH credential is the only secret and it never reaches
  the script text).
* Logging emits ``script_len`` (an integer, the UTF-8 byte count of
  the operator-supplied script) and ``exit_status`` only. Neither the
  original script, the encoded payload, nor any field of
  ``target.secret_ref`` is bound into any log event. The structured
  error mirrors this: ``script`` and ``encoded`` are never written
  into the error's user-visible attributes.
* Callers must not pass credential material in ``script``. The helper
  cannot enforce this because the script is opaque to it; the rule is
  encoded in the connector's per-op handlers, none of which
  interpolate secrets into the PowerShell text they assemble.

Cited references
----------------

* PowerShell ``-EncodedCommand`` / ``about_pwsh``:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh
* PowerShell ``ConvertTo-Json``:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json
* DnsServer module cmdlet reference:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/
* Transport sibling:
  :mod:`meho_backplane.connectors.holodeck._pwsh`.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import structlog

__all__ = ["PWSH_DEFAULT_DEPTH", "PwshRunError", "encode_pwsh_command", "pwsh_run"]

_log = structlog.get_logger(__name__)

#: Default ``ConvertTo-Json -Depth`` value the helper recommends in its
#: ``script`` argument. The PowerShell built-in defaults to 2, which
#: silently truncates deeply nested cmdlet output (DnsServer resource
#: records nest ``RecordData`` one level deep, and zone objects carry
#: nested aging / scavenging blocks). We surface an explicit ``-Depth 4``
#: as a constant so each caller doesn't reinvent the value.
PWSH_DEFAULT_DEPTH: int = 4

#: Maximum stderr fragment length retained on a :class:`PwshRunError`.
#: Stderr from ``powershell`` can be long (multi-line stack traces / cmdlet
#: ``Write-Error`` records, plus the CLIXML progress serialisation Windows
#: PowerShell emits to stderr on first module load); a hard cap keeps
#: log/audit payloads bounded and avoids any chance of secret-shaped
#: substrings hidden deeper in the output bleeding into error surfaces.
_STDERR_MAX_LEN: int = 4096

#: The PowerShell executable this transport invokes on the remote host.
#: Deliberately ``powershell`` (Windows PowerShell 5.1) -- **not** ``pwsh``
#: (PowerShell 7). A Windows Server AD-DNS host ships Windows PowerShell
#: 5.1 as ``powershell.exe`` and the ``DnsServer`` module targets it;
#: ``pwsh`` is a separate optional install that is **absent by default on
#: a domain controller** (verified 2026-08-03 against a live WS2022 DC:
#: ``pwsh -EncodedCommand`` → ``'pwsh' is not recognized as an internal or
#: external command``; ``powershell -EncodedCommand`` → DnsServer module
#: ``2.0.0.0``). This is the one deliberate divergence from the Holodeck
#: ``_pwsh`` sibling, whose HoloConsole host runs PS7. A connector may
#: override the executable via a ``POWERSHELL_EXECUTABLE`` class attr
#: (e.g. a future target that ships PS7).
_DEFAULT_PS_EXECUTABLE: str = "powershell"

#: Prepended to every script so the ``DnsServer`` module's first-use
#: progress stream ("Preparing modules for first use") does not serialise
#: as CLIXML onto the remote streams. Windows PowerShell emits that
#: progress to **stderr** (verified 2026-08-03: stdout stays clean JSON
#: either way, so ``json.loads(stdout)`` already parses), but suppressing
#: it keeps a :class:`PwshRunError`'s stderr fragment free of CLIXML noise
#: and matches the operator-proven convention (ai-lab-de #35). ``pwsh``
#: (PS7) honours the same variable, so this stays correct if a target
#: overrides :data:`_DEFAULT_PS_EXECUTABLE` back to PS7.
_PROGRESS_GUARD: str = "$ProgressPreference = 'SilentlyContinue'; "


class PwshRunError(RuntimeError):
    """Structured failure from :func:`pwsh_run`.

    Carried fields:

    * ``exit_status`` — ``pwsh``'s exit code (``int``). Non-zero
      indicates the cmdlet itself failed; ``None`` indicates
      ``pwsh -EncodedCommand`` produced output that :mod:`json`
      could not parse.
    * ``stderr`` — the (truncated) ``pwsh`` stderr fragment. Capped at
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


def encode_pwsh_command(script: str) -> str:
    """Encode *script* per ``pwsh -EncodedCommand``'s contract.

    The contract (Microsoft docs, ``about_pwsh``): the value following
    ``-EncodedCommand`` is the base64 of the UTF-16LE bytes of the
    PowerShell script. No BOM, no surrounding whitespace, no trailing
    newline.

    Example::

        >>> encode_pwsh_command("Get-DnsServerZone | ConvertTo-Json")
        'RwBlAHQALQBEAG4AcwBTAGUAcgB2AGUAcgBaAG8AbgBlACAAfAAgAEMAbwBuAHYAZQByAHQAVABvAC0ASgBzAG8AbgA='

    Public so the unit suite can assert the encoding round-trips
    against the documented convention without re-deriving it inside
    the test.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


async def pwsh_run(
    connector: Any,
    target: Any,
    script: str,
    *,
    operator: Any = None,
    depth: int = PWSH_DEFAULT_DEPTH,
    timeout: float = 30.0,
) -> Any:
    """Run *script* on *target* via ``pwsh -EncodedCommand``; parse JSON output.

    Parameters
    ----------
    connector
        An :class:`~meho_backplane.connectors.adapters.ssh.SshConnector`
        instance (typically the :class:`WindowsDnsConnector` itself).
        The helper reaches into its ``_run_command`` seam so the
        adapter's pooling + auth machinery is preserved.
    target
        The :class:`Target` the SSH connection is keyed to. Passed
        through to ``_run_command``.
    script
        The PowerShell script body the caller wants to run. Callers
        are expected to pipe the cmdlet's output through
        ``| ConvertTo-Json`` (or ``ConvertTo-Json -Compress -Depth N``
        for deep cmdlet outputs); the helper does not append the
        conversion itself because some ops construct multi-statement
        scripts where only the final pipeline produces the JSON.
    operator
        Forwarded to ``_run_command`` for the operator-context Vault
        credential read on an SSH pool miss; ``None`` fails closed.
    depth
        Advisory — surfaced as the recommended ``-Depth`` value via
        :data:`PWSH_DEFAULT_DEPTH`. The helper itself does not rewrite
        the script; this argument exists so the constant has a public
        named entry point on the function signature for callers that
        want to assert "I used the default depth".
    timeout
        Wall-clock seconds for the remote command. Forwarded to
        ``_run_command``; expiry raises :exc:`asyncio.TimeoutError`.

    Returns
    -------
    The parsed JSON payload (typically a ``dict`` or ``list``).

    Raises
    ------
    PwshRunError
        ``pwsh`` exited non-zero, or stdout did not parse as JSON.
        ``PwshRunError.exit_status`` carries the integer exit on
        non-zero exit; ``None`` indicates the JSON parse failed.
    """
    del depth  # advisory — see docstring
    executable = getattr(connector, "POWERSHELL_EXECUTABLE", _DEFAULT_PS_EXECUTABLE)
    # Prepend the progress guard so the DnsServer module's first-use
    # progress never pollutes the remote streams (see :data:`_PROGRESS_GUARD`).
    full_script = _PROGRESS_GUARD + script
    encoded = encode_pwsh_command(full_script)
    cmd = f"{executable} -NoProfile -NonInteractive -EncodedCommand {encoded}"

    proc = await connector._run_command(target, cmd, operator=operator, timeout=timeout)

    stdout: str = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stderr: str = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    if not isinstance(stdout, str):
        stdout = ""
    if not isinstance(stderr, str):
        stderr = ""
    exit_status: int | None = getattr(proc, "exit_status", None)

    # Structured logging discipline (mirrors the SSH adapter's
    # ``ssh_command_executed`` event): only sizes + exit code; never
    # the script body, never the encoded payload, never anything that
    # could carry credential bytes.
    _log.info(
        "windows_dns_pwsh_executed",
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

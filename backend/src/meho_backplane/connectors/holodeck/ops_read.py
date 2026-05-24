# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Holodeck read ops -- 7 typed read ops for G3.8-T2 (#854).

Adds the following typed ops onto :class:`HolodeckConnector`:

* ``holodeck.config.show`` -- ``Get-HoloDeckConfig | ConvertTo-Json``
  routed through :func:`pwsh_run`; returns the parsed config dict.
* ``holodeck.pod.list`` -- ``Get-HoloDeckPod | ConvertTo-Json``
  routed through :func:`pwsh_run`; returns ``{rows, total}``
  (the JSONFlux-shaped envelope sibling connectors emit; the actual
  handle creation is the reducer's job, not the connector's, per the
  :mod:`~meho_backplane.connectors.pfsense.ops_read` precedent).
* ``holodeck.pod.info`` -- ``Get-HoloDeckPod -Id <id> | ConvertTo-Json``;
  returns the single-pod detail dict (state, networking, VMs).
* ``holodeck.service.list`` -- ``Get-Service | Where-Object { $_.Name
  -like 'Holo*' } | Select-Object Name,Status,DisplayName |
  ConvertTo-Json``; returns ``{rows, total}``.
* ``holodeck.k8s.exec`` -- in-appliance ``kubectl <verb> ...``
  passthrough. **Read-only**: the handler fails closed when the first
  non-flag token after ``kubectl`` is not in
  :data:`_K8S_READ_VERBS`. Schema also pins ``kubectl <verb> ...``
  with a ``pattern`` so the dispatcher's validator catches the bad
  shape before reaching the handler.
* ``holodeck.logs.tail`` -- ``tail -<lines> /holodeck-runtime/logs/
  <component>*.log`` over plain SSH (no ``pwsh`` indirection — the
  cmd is a stock POSIX shell pipeline). Returns the tail text plus
  the per-file line totals.
* ``holodeck.networking.show`` -- composite of FRR/BGP summary
  (``vtysh -c 'show bgp summary' && vtysh -c 'show ip route'``) +
  DNS zone summary (``pwsh -c "Get-DnsServerZone | ConvertTo-Json"``)
  + DHCP leases (``cat /var/lib/dhcp/dhcpd.leases``). Returns a
  structured envelope.

Pure parsers vs handler thin layer
-----------------------------------

Following the pfSense ``ops_read.py`` / bind9 ``ops_zone.py``
convention, the heavy lifting lives in pure functions
(:func:`parse_kubectl_command`, :func:`parse_logs_tail_output`,
:func:`parse_networking_payload`) that accept captured stdout / JSON
text and return Python data. The bound-method handlers on
:class:`HolodeckConnector` are the thin SSH-call + parse + shape
layer. The unit suite pins the parsers directly against fixture text
without booting an event loop.

JSONFlux handle pattern -- deferred to the reducer
---------------------------------------------------

``holodeck.pod.list`` and ``holodeck.service.list`` return
``{rows, total}``. Mirroring the bind9 / pfSense precedent, the
JSONFlux handle is the **reducer's** responsibility — not the
connector's. Setting handle creation here would couple every
connector to the reducer's calibration threshold and bypass the
reducer's audit / TTL / store-routing logic.

The handler ships ``rows`` + ``total`` so a future JSONFlux reducer
can pull both signals (inlined sample size + total) to drive its
threshold check. No handle exists today — the current
:class:`~meho_backplane.operations.reducer.PassThroughReducer` always
returns the inline payload unchanged.

Safety -- read-only k8s exec
-----------------------------

The acceptance criterion calls for ``holodeck.k8s.exec`` to reject
mutating ``kubectl`` verbs (``create``/``apply``/``delete``/``edit``/
``scale``/``patch``/...) **and** the canonical shell-injection
exploit shapes (``;`` / ``&&`` / ``|`` / ``$(...)`` / backticks /
``>`` / ``<`` / newline). The defence runs in two complementary
allowlist layers:

1. **Schema layer** -- ``parameter_schema.properties.command`` carries
   a ``pattern`` regex anchored ``\\A ... \\Z`` that requires the
   command to start with ``kubectl``, hit one of the read-only verbs,
   and carry only characters from the allowlist
   ``[A-Za-z0-9._/=:,@-]`` in its arguments. Whitespace between tokens
   is constrained to space-or-tab (``[ \\t]``) so a newline cannot
   smuggle a second command line through the ``\\s`` class. The
   dispatcher's
   :func:`~meho_backplane.operations._validate.validate_params` walks
   this before reaching the handler; bad shapes surface as a
   ``result_invalid_params`` envelope.
2. **Handler layer** -- before the SSH call lands, the handler
   re-parses the command via :func:`parse_kubectl_command`, which
   (a) scans the raw command for any character in
   :data:`_SHELL_METACHARS_RE` and refuses on hit, then (b) tokenises
   via :func:`shlex.split` and (c) compares the verb to
   :data:`_K8S_READ_VERBS`. Any rejected step raises
   :exc:`KubectlSafetyError`, which the dispatcher's exception path
   turns into a ``result_connector_error`` envelope. The metacharacter
   reject is **load-bearing**: ``shlex.split`` in POSIX mode does not
   treat shell separators as token boundaries, so a chained payload
   like ``kubectl get pods; rm -rf /`` would otherwise tokenise to
   ``['kubectl', 'get', 'pods;', ...]`` -- the verb-safelist check
   on ``tokens[idx]`` would see ``'get'`` and approve, and the
   handler would then forward the **raw string** to
   ``asyncssh.SSHClientConnection.run``, which delegates to the
   remote login shell where the shell interprets the metacharacter.

The handler layer is the **authoritative** gate; the schema pattern
is a guardrail so a misshapen call doesn't even reach the connector.
Both layers reject every chained-shell shape independently -- a
future widening on either side does not silently re-open the hole.

References
----------

* Task: G3.8-T2 (#854).
* Parent initiative: G3.8 (#371).
* Precedents: :mod:`meho_backplane.connectors.pfsense.ops_read`,
  :mod:`meho_backplane.connectors.bind9.ops_zone`.
* PowerShell ``Get-HoloDeckConfig`` / ``Get-HoloDeckPod`` --
  vendor cmdlets shipped with VMware Holodeck Toolkit 9.x.
* ``kubectl`` verbs (read vs mutate):
  https://kubernetes.io/docs/reference/kubectl/.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.holodeck._pwsh import PwshRunError, pwsh_run
from meho_backplane.connectors.holodeck.ops import HolodeckOp

if TYPE_CHECKING:
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "READ_OPS",
    "KubectlSafetyError",
    "holodeck_config_show",
    "holodeck_k8s_exec",
    "holodeck_logs_tail",
    "holodeck_networking_show",
    "holodeck_pod_info",
    "holodeck_pod_list",
    "holodeck_service_list",
    "parse_kubectl_command",
    "parse_logs_tail_output",
    "parse_networking_payload",
]


# ---------------------------------------------------------------------------
# Read-only kubectl guard
# ---------------------------------------------------------------------------

#: Whitelist of ``kubectl`` verbs the handler considers read-only.
#: ``get`` / ``describe`` / ``logs`` are the three primary read verbs
#: the Initiative #371 body calls out. ``top`` (metrics) and
#: ``explain`` (schema) are pure reads. ``api-resources`` /
#: ``api-versions`` / ``cluster-info`` / ``version`` cover the
#: inspection surface without mutating cluster state. Any verb absent
#: from this set -- notably ``create`` / ``apply`` / ``delete`` /
#: ``edit`` / ``replace`` / ``patch`` / ``scale`` / ``rollout``
#: (``restart``) / ``label`` (``--overwrite``) / ``annotate`` /
#: ``cp`` / ``exec`` / ``port-forward`` / ``proxy`` / ``drain`` /
#: ``cordon`` -- fails closed. Multi-word inspection verbs
#: (``config view``, ``auth can-i``) are intentionally **not**
#: surfaced today: the safelist matches on the single verb token, so
#: ``kubectl config get-contexts`` would be approved through the
#: ``config`` parent verb. Surfacing them safely requires a verb +
#: sub-verb safelist, which is deferred -- callers that need them
#: should file a follow-up.
_K8S_READ_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "describe",
        "logs",
        "top",
        "explain",
        "api-resources",
        "api-versions",
        "cluster-info",
        "version",
    }
)

#: Shell metacharacters banned anywhere in a ``kubectl`` command line.
#: ``shlex.split`` in POSIX mode does **not** treat these as token
#: boundaries (it only splits on whitespace + quotes), so a chained
#: payload like ``kubectl get pods; rm -rf /`` parses to verb=``get``
#: with the verb-safelist check happy -- and ``_run_command`` then
#: hands the **raw string** to ``asyncssh.SSHClientConnection.run``,
#: which delegates to the remote login shell where the
#: metacharacter is interpreted. Allowlist for safe characters is
#: applied as a positive check by the schema layer regex; this
#: blocklist is the handler-layer equivalent and the authoritative
#: gate (a future schema widening must not let writes through). The
#: character set covers every POSIX-shell control operator that can
#: chain a command, expand to a subshell, or redirect IO:
#:
#: * ``;`` / newline / CR -- statement separators
#: * ``&`` / ``|`` -- background, AND/OR list, pipe
#: * ``<`` / ``>`` -- input / output redirection
#: * ``$`` / ``(`` / ``)`` -- arithmetic, command, process substitution
#: * backtick -- legacy command substitution
#: * ``\\`` -- line continuation / escape into the next char
_SHELL_METACHARS_RE: re.Pattern[str] = re.compile(r"[;&|<>`$()\\\n\r]")


class KubectlSafetyError(ValueError):
    """Raised by :func:`parse_kubectl_command` when the verb is not read-only.

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly with
    the other validation-style failures. The exception message names
    the rejected verb so the operator-visible surface explains why
    the call was refused; the message does **not** include the full
    command (which can carry resource names that, while not secret,
    don't belong in user-visible error messages either -- the schema
    layer already echoes the supplied command verbatim in the
    ``invalid_params`` envelope when the pattern check fails).
    """


def parse_kubectl_command(command: str) -> tuple[str, list[str]]:
    """Parse ``command`` into ``(verb, args)``; enforce the read-only safelist.

    The first whitespace-separated token must be ``kubectl``. The
    second non-flag token is the verb; the verb must appear in
    :data:`_K8S_READ_VERBS`. Returns the verb and the remaining args
    so callers can re-serialise the command for SSH.

    Tokenisation runs via :func:`shlex.split` so quoted resource names
    (``"my pod"``) survive the parse; the safety check operates on the
    verb token, which is unquoted in every legal ``kubectl`` invocation.

    Before tokenisation the function scans for POSIX-shell
    metacharacters (:data:`_SHELL_METACHARS_RE`) and refuses the call
    if any are found. This is **load-bearing**: ``shlex.split`` in
    POSIX mode does not treat ``;`` / ``&&`` / ``|`` / ``$(...)`` /
    backticks / ``>`` / ``<`` as token boundaries, so a chained payload
    like ``kubectl get pods; rm -rf /`` would tokenise to
    ``['kubectl', 'get', 'pods;', 'rm', '-rf', '/']``, the verb check
    on ``tokens[idx]`` would see ``'get'`` and approve, and the
    handler would then forward the **raw string** verbatim to
    ``asyncssh.SSHClientConnection.run`` -- which delegates to the
    remote login shell where the metacharacters are interpreted. The
    metachar reject closes that hole before tokenisation runs.

    Empty command, or command containing shell metacharacters, or
    command not starting with ``kubectl``, or verb not in
    :data:`_K8S_READ_VERBS` -> :exc:`KubectlSafetyError`. The handler
    raises before any SSH traffic happens.

    Examples
    --------

    >>> parse_kubectl_command("kubectl get pods -n holodeck")
    ('get', ['pods', '-n', 'holodeck'])
    >>> parse_kubectl_command("kubectl describe pod my-pod")
    ('describe', ['pod', 'my-pod'])
    """
    if not command or not command.strip():
        raise KubectlSafetyError("kubectl command is empty")
    # Metacharacter rejection -- load-bearing security gate (see
    # docstring above and :data:`_SHELL_METACHARS_RE`). Don't echo the
    # offending character back in the message: that's an
    # operator-visible surface and the rejected verbatim character
    # set is auditable through the audit_log row's ``params_hash``.
    if _SHELL_METACHARS_RE.search(command):
        raise KubectlSafetyError(
            "kubectl command rejected: shell metacharacter detected; "
            "only a single unchained kubectl invocation is allowed "
            "(no ';', '&&', '||', '|', '$(...)', backticks, '>', '<', "
            "newlines, or line continuations)"
        )
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise KubectlSafetyError(f"kubectl command failed to tokenise: {exc}") from exc
    if not tokens:
        raise KubectlSafetyError("kubectl command tokenises to nothing")
    if tokens[0] != "kubectl":
        raise KubectlSafetyError(f"kubectl command must start with 'kubectl' (got {tokens[0]!r})")
    # The verb is the first non-flag token after ``kubectl``. A handful
    # of legal invocations open with a global flag (``kubectl
    # --context=foo get pods``); we walk past leading flag tokens but
    # only consume their *attached* values (``--context=foo``). When a
    # flag is in the separated form (``--context foo``) the next token
    # is the flag's value, not a verb -- we skip it too.
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        token = tokens[idx]
        idx += 1
        if "=" not in token and idx < len(tokens) and not tokens[idx].startswith("-"):
            # Separated flag value (``--context foo``); consume.
            idx += 1
    if idx >= len(tokens):
        raise KubectlSafetyError("kubectl command has no verb after global flags")
    verb = tokens[idx]
    if verb not in _K8S_READ_VERBS:
        raise KubectlSafetyError(
            f"kubectl verb {verb!r} is not on the read-only safelist; "
            f"allowed: {sorted(_K8S_READ_VERBS)}"
        )
    return verb, tokens[idx + 1 :]


# ---------------------------------------------------------------------------
# Pure parsers -- tail output + networking composite
# ---------------------------------------------------------------------------


def parse_logs_tail_output(stdout: str) -> dict[str, Any]:
    """Parse ``tail -N file1 file2 ...`` GNU-style output into ``{files}``.

    GNU ``tail`` with multiple files emits a header before each file
    block::

        ==> /holodeck-runtime/logs/holodeck-dhcp.log <==
        <up to N lines>
        ==> /holodeck-runtime/logs/holodeck-dns.log <==
        <up to N lines>

    Single-file ``tail`` emits no header — the entire stdout is the
    file's tail. The parser detects which form the stdout is in by
    presence of the ``==> ... <==`` marker. Returns:

    .. code-block:: python

        {
            "files": [
                {"path": "/holodeck-runtime/logs/foo.log", "lines": "..."},
                ...
            ],
            "raw": "<full stdout>"
        }

    ``raw`` carries the unparsed stdout so callers can show it verbatim
    when the parsed structure is empty / a parse hint is needed.

    >>> output = (
    ...     "==> /holodeck-runtime/logs/dhcp.log <==\\n"
    ...     "dhcp event 1\\n"
    ...     "dhcp event 2\\n"
    ...     "==> /holodeck-runtime/logs/dns.log <==\\n"
    ...     "dns event 1\\n"
    ... )
    >>> parsed = parse_logs_tail_output(output)
    >>> parsed["files"][0]["path"]
    '/holodeck-runtime/logs/dhcp.log'
    >>> parsed["files"][1]["lines"]
    'dns event 1\\n'
    """
    raw = stdout
    if not raw.strip():
        return {"files": [], "raw": raw}

    if "==> " not in raw:
        # Single-file tail: no header.
        return {"files": [{"path": None, "lines": raw}], "raw": raw}

    files: list[dict[str, Any]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("==>") and stripped.endswith("<=="):
            # Flush the previous block.
            if current_path is not None:
                files.append({"path": current_path, "lines": "".join(current_lines)})
            current_path = stripped[3:-3].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_path is not None:
        files.append({"path": current_path, "lines": "".join(current_lines)})
    return {"files": files, "raw": raw}


def parse_networking_payload(
    *,
    bgp_text: str,
    routes_text: str,
    dns_zones_json: Any,
    dhcp_leases_text: str,
) -> dict[str, Any]:
    """Compose the ``holodeck.networking.show`` envelope from four inputs.

    The handler routes each sub-command separately (FRR/BGP via
    ``vtysh -c`` over plain SSH; DNS zones via ``pwsh`` for the
    structured JSON; DHCP leases via plain ``cat``). The composer
    keeps the four signal streams separated so callers can drill into
    one without the others.

    Returns:

    .. code-block:: python

        {
            "bgp": {"summary_text": "...", "ok": True},
            "routes": {"text": "...", "ok": True},
            "dns": {"zones": [...], "total": N, "ok": True},
            "dhcp": {"leases_text": "...", "ok": True},
        }

    Each sub-section's ``ok`` is ``True`` when the input is non-empty.
    The shape is intentionally narrative -- ``vtysh`` BGP output is
    operator-formatted text, not structured JSON, and the consumer's
    onboarding doc (T3 #855) walks operators through the BGP fields by
    eye. Forcing a per-field structured parse here would invert the
    cost of curation: we'd ship parser tests for every ``vtysh``
    version drift the appliance encounters.
    """
    dns_zones_list: list[Any]
    if isinstance(dns_zones_json, list):
        dns_zones_list = dns_zones_json
    elif isinstance(dns_zones_json, dict):
        dns_zones_list = [dns_zones_json]
    else:
        dns_zones_list = []
    return {
        "bgp": {
            "summary_text": bgp_text,
            "ok": bool(bgp_text.strip()),
        },
        "routes": {
            "text": routes_text,
            "ok": bool(routes_text.strip()),
        },
        "dns": {
            "zones": dns_zones_list,
            "total": len(dns_zones_list),
            "ok": isinstance(dns_zones_json, (list, dict)),
        },
        "dhcp": {
            "leases_text": dhcp_leases_text,
            "ok": bool(dhcp_leases_text.strip()),
        },
    }


# ---------------------------------------------------------------------------
# Handler functions (bound-method shims on HolodeckConnector)
# ---------------------------------------------------------------------------


async def holodeck_config_show(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the Holodeck configuration dict from ``Get-HoloDeckConfig``.

    Op-id: ``holodeck.config.show``. Runs ``Get-HoloDeckConfig |
    ConvertTo-Json -Depth 4 -Compress`` via the pwsh helper and
    returns the parsed dict. Cmdlet failure -> ``{config: None,
    error: "<reason>"}`` envelope.
    """
    del params  # declared empty; intentionally ignored
    script = "Get-HoloDeckConfig | ConvertTo-Json -Depth 4 -Compress"
    try:
        payload = await pwsh_run(self, target, script)
    except PwshRunError as exc:
        return {"config": None, "error": str(exc)}
    return {"config": payload}


async def holodeck_pod_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the active Holodeck nested-pod inventory.

    Op-id: ``holodeck.pod.list``. Runs ``Get-HoloDeckPod |
    ConvertTo-Json -Depth 4`` via the pwsh helper. Returns a
    ``{rows, total}`` envelope -- the JSONFlux-shaped surface the
    sibling connectors emit. The JSONFlux handle itself is the
    reducer's responsibility (per the pfSense / bind9 precedent); the
    handler just ships both signals (inlined rows + total count).

    ``ConvertTo-Json`` on a single-element list returns a flat dict;
    on a multi-element list it returns a JSON array. We normalise
    both shapes to a list before populating ``rows``.
    """
    del params  # declared empty; intentionally ignored
    script = "Get-HoloDeckPod | ConvertTo-Json -Depth 4"
    try:
        payload = await pwsh_run(self, target, script)
    except PwshRunError as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    rows = _normalise_pwsh_json_array(payload)
    return {"rows": rows, "total": len(rows)}


async def holodeck_pod_info(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the per-pod detail dict for *pod_id*.

    Op-id: ``holodeck.pod.info``. Runs ``Get-HoloDeckPod -Id '<id>' |
    ConvertTo-Json -Depth 4`` via the pwsh helper. *params* must
    carry ``pod_id`` (the schema marks it required); the handler
    inlines it into the cmdlet via PowerShell-safe single-quoting.

    Cmdlet failure -> ``{pod: None, error: "<reason>"}``.
    """
    pod_id = params.get("pod_id")
    if not isinstance(pod_id, str) or not pod_id.strip():
        return {"pod": None, "error": "pod_id is required and must be a non-empty string"}
    # PowerShell single-quoting: the only escape inside single quotes
    # is doubling the single quote itself. asyncssh's argv path runs
    # the cmd through a shell on the appliance, so the operator-supplied
    # pod_id is also subject to shell quoting -- but the encoded-command
    # contract means the shell never sees the value as a discrete token;
    # the entire pwsh script body is base64'd before transport.
    quoted_id = pod_id.replace("'", "''")
    script = f"Get-HoloDeckPod -Id '{quoted_id}' | ConvertTo-Json -Depth 4"
    try:
        payload = await pwsh_run(self, target, script)
    except PwshRunError as exc:
        return {"pod": None, "error": str(exc)}
    return {"pod": payload}


async def holodeck_service_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return Photon services with the ``Holo*`` prefix and their status.

    Op-id: ``holodeck.service.list``. Runs ``Get-Service |
    Where-Object { $_.Name -like 'Holo*' } | Select-Object
    Name,Status,DisplayName | ConvertTo-Json -Depth 4`` via the pwsh
    helper. Returns ``{rows, total}``.

    Mirrors the cmdlet shape that :meth:`HolodeckConnector.probe`
    uses for the ``holodeck_services_down`` check, but exposes the
    raw list to operators instead of folding it into a single boolean.
    """
    del params  # declared empty; intentionally ignored
    script = (
        "Get-Service | Where-Object { $_.Name -like 'Holo*' } | "
        "Select-Object Name,Status,DisplayName | ConvertTo-Json -Depth 4"
    )
    try:
        payload = await pwsh_run(self, target, script)
    except PwshRunError as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    rows = _normalise_pwsh_json_array(payload)
    return {"rows": rows, "total": len(rows)}


async def holodeck_k8s_exec(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Forward a **read-only** ``kubectl`` command to the in-appliance K8s.

    Op-id: ``holodeck.k8s.exec``. *params* carries ``command`` (a
    string starting with ``kubectl <read-verb> ...``). The handler:

    1. Parses the command via :func:`parse_kubectl_command`; shell
       metacharacters and mutating verbs raise
       :exc:`KubectlSafetyError` **before** any SSH transport is
       touched. The exception is folded into a ``result_connector_
       error`` envelope by the structured-error path below.
    2. Runs the parsed command verbatim over plain SSH (no ``pwsh``;
       the in-appliance K8s is reached through the appliance's
       ``kubectl`` binary, not through PowerShell).
    3. Returns the command stdout / exit_status / stderr fragment.

    The schema pattern in :data:`READ_OPS` enforces an allowlist
    shape (``\\Akubectl ... (read-verb) ([ \\t]+[A-Za-z0-9._/=:,@-]+)*
    \\Z``) at the validator layer; this handler is the authoritative
    gate, not a fallback. Both layers reject the canonical
    shell-injection shapes (``;`` / ``&&`` / ``|`` / ``$(...)`` /
    backticks / ``>`` / ``<`` / newline) independently.

    Stderr is **truncated** at 4096 chars to keep operator surfaces
    bounded (same convention as :class:`PwshRunError`). The
    handler's logging emits ``command_len`` + ``exit_status``; the
    full command body is never logged (the verb shape is operator-
    auditable through the audit_log row's ``params_hash`` plus the
    schema's pattern enforcement).
    """
    raw_command = params.get("command")
    if not isinstance(raw_command, str):
        return {
            "stdout": "",
            "stderr": "",
            "exit_status": None,
            "error": "command is required and must be a string",
        }
    # The handler is the authoritative gate. Re-parse even though the
    # schema pattern caught the obvious bad shape -- a future schema
    # widening must not silently let writes through.
    try:
        parse_kubectl_command(raw_command)
    except KubectlSafetyError as exc:
        return {
            "stdout": "",
            "stderr": "",
            "exit_status": None,
            "error": f"k8s.exec safety check: {exc}",
        }
    # The command is operator-supplied; run it verbatim via the pooled
    # SSH connection. The base adapter's ``_run_command`` already
    # bounds the wall clock at 30s by default.
    try:
        proc = await self._run_command(target, raw_command, raw_jwt="")
    except Exception as exc:
        return {
            "stdout": "",
            "stderr": "",
            "exit_status": None,
            "error": str(exc),
        }
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    if not isinstance(stdout, str):
        stdout = ""
    if not isinstance(stderr, str):
        stderr = ""
    # Cap stderr at 4096 chars -- same convention as :class:`PwshRunError`.
    if len(stderr) > 4096:
        stderr = stderr[:4096]
    exit_status = getattr(proc, "exit_status", None)
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_status": exit_status,
    }


async def holodeck_logs_tail(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Tail ``/holodeck-runtime/logs/<component>*.log`` for *N* lines.

    Op-id: ``holodeck.logs.tail``. *params*:

    * ``component`` -- required; the log-file slug (e.g. ``"dhcp"``,
      ``"dns"``, ``"frr"``, ``"webtop"``). The slug is restricted to
      ``[A-Za-z0-9._-]+`` by the schema's pattern; the handler
      rejects any other shape with an error envelope as the
      belt-and-braces fallback.
    * ``lines`` -- optional integer; defaults to 200. Schema's
      ``minimum`` / ``maximum`` clamp at ``[1, 5000]``.

    Runs ``tail -<lines> /holodeck-runtime/logs/<component>*.log``
    over plain SSH (no pwsh — the cmd is a stock POSIX pipeline).
    Returns the parsed tail envelope via
    :func:`parse_logs_tail_output`.
    """
    component = params.get("component")
    if not isinstance(component, str) or not component.strip():
        return {
            "files": [],
            "raw": "",
            "lines_requested": 0,
            "error": "component is required and must be a non-empty string",
        }
    # Belt-and-braces shape check: schema pattern enforces this, but
    # the handler refuses anything outside the safelist so a future
    # schema edit can't accidentally let through metacharacters that
    # would change the meaning of the shell pipeline.
    if not _COMPONENT_SAFE_RE.fullmatch(component):
        return {
            "files": [],
            "raw": "",
            "lines_requested": 0,
            "error": (
                f"component {component!r} contains unsafe characters; "
                f"only [A-Za-z0-9._-]+ are allowed"
            ),
        }
    lines_raw = params.get("lines", 200)
    if not isinstance(lines_raw, int) or lines_raw < 1 or lines_raw > 5000:
        return {
            "files": [],
            "raw": "",
            "lines_requested": 0,
            "error": "lines must be an integer in [1, 5000]",
        }
    cmd = f"tail -n {lines_raw} /holodeck-runtime/logs/{component}*.log"
    try:
        proc = await self._run_command(target, cmd, raw_jwt="")
    except Exception as exc:
        return {
            "files": [],
            "raw": "",
            "lines_requested": lines_raw,
            "error": str(exc),
        }
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    parsed = parse_logs_tail_output(content)
    parsed["lines_requested"] = lines_raw
    return parsed


async def holodeck_networking_show(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compose the FRR/BGP + DNS + DHCP networking snapshot.

    Op-id: ``holodeck.networking.show``. Runs four sub-commands
    over the pooled SSH connection:

    1. ``vtysh -c 'show bgp summary'`` -- FRR/BGP peer summary.
    2. ``vtysh -c 'show ip route'`` -- the kernel routing table as
       FRR sees it.
    3. ``pwsh`` of ``Get-DnsServerZone | Select-Object ZoneName,
       ZoneType | ConvertTo-Json -Depth 4`` -- DNS zone summary.
    4. ``cat /var/lib/dhcp/dhcpd.leases`` -- raw DHCP leases (the
       file format is documented and operator-readable; structured
       parsing belongs in T3's recipe rather than here).

    Each sub-section's ``ok`` flips false when the sub-command failed
    or produced empty output; the operator-visible envelope
    explicitly partitions the four signals so a single-component
    failure doesn't blank the whole response.

    Cmd failures (``OSError``/``asyncssh.Error``) on the plain-SSH
    paths surface as empty strings (parsed by
    :func:`parse_networking_payload` into ``ok=False`` sub-sections).
    ``PwshRunError`` on the DNS path surfaces as
    ``dns.ok=False`` + an empty zones list.
    """
    del params  # declared empty; intentionally ignored

    bgp_text = await _safe_run_text(self, target, "vtysh -c 'show bgp summary'")
    routes_text = await _safe_run_text(self, target, "vtysh -c 'show ip route'")
    dns_zones_payload: Any
    try:
        dns_zones_payload = await pwsh_run(
            self,
            target,
            "Get-DnsServerZone | Select-Object ZoneName,ZoneType | ConvertTo-Json -Depth 4",
        )
    except PwshRunError:
        dns_zones_payload = None
    dhcp_text = await _safe_run_text(self, target, "cat /var/lib/dhcp/dhcpd.leases")

    return parse_networking_payload(
        bgp_text=bgp_text,
        routes_text=routes_text,
        dns_zones_json=dns_zones_payload,
        dhcp_leases_text=dhcp_text,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


#: Component-name safelist for ``holodeck.logs.tail``. The handler
#: rejects any value outside ``[A-Za-z0-9._-]+`` so the resulting
#: ``tail`` cmdline cannot smuggle in shell metacharacters or escape
#: the ``/holodeck-runtime/logs/`` prefix via a directory traversal.
_COMPONENT_SAFE_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9._-]+")


def _normalise_pwsh_json_array(payload: Any) -> list[Any]:
    """Normalise ``ConvertTo-Json`` outputs into a Python list of rows.

    PowerShell's ``ConvertTo-Json`` collapses a single-element
    pipeline result into a flat object (not a 1-element list).
    Multi-element pipelines render as JSON arrays. Empty pipelines
    render as ``$null`` -> Python ``None``. The normaliser folds
    all three shapes into a list so the ``{rows, total}`` envelope
    has a uniform shape.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if item is not None]
    if isinstance(payload, dict):
        return [payload]
    # Unexpected scalar payload (e.g. an int or a bool) -- ConvertTo-Json
    # can render scalars verbatim. Wrap into a single-item list so the
    # envelope stays consistent; callers can drill into the row to
    # recover the original scalar.
    return [payload]


async def _safe_run_text(
    self: HolodeckConnector,
    target: Any,
    cmd: str,
) -> str:
    """Run *cmd* via plain SSH, return stdout text; failures -> empty string.

    The networking composer keeps each sub-command's failure isolated;
    a ``vtysh`` crash on the BGP summary path must not blank the DNS
    or DHCP sub-sections. The handler relies on the empty-string
    convention -- :func:`parse_networking_payload` flips the
    sub-section's ``ok`` to ``False`` when the input is empty.
    """
    try:
        proc = await self._run_command(target, cmd, raw_jwt="")
    except Exception:
        return ""
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    if isinstance(stdout, str):
        return stdout
    return ""


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


#: SSH-only / pwsh transport reminder copied verbatim into every op's
#: ``llm_instructions``. The Initiative #371 body and CLAUDE.md
#: postulate 5 both require agent-facing descriptions to call out the
#: PowerShell-over-SSH transport so an LLM doesn't compose against a
#: non-existent REST surface.
_SSH_TRANSPORT_NOTE: str = (
    "Holodeck has no REST API; the underlying transport is "
    "PowerShell-over-SSH (pwsh -EncodedCommand routed through asyncssh) "
    "for cmdlet ops, plain SSH for kubectl / shell-pipeline ops."
)


READ_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id="holodeck.config.show",
        handler_attr="config_show",
        summary="Return the Holodeck appliance configuration dict.",
        description=(
            "Runs ``Get-HoloDeckConfig | ConvertTo-Json -Depth 4 "
            "-Compress`` over the pwsh-over-SSH transport and returns "
            "the parsed configuration dict. The dict carries vendor / "
            "product / version / pod ID and a `services` block summarising "
            "the bundled Holodeck Photon services. Use when the operator "
            "needs the full appliance configuration snapshot; for just "
            "the identifying fields prefer ``holodeck.about`` (faster, "
            "fewer fields). No params; safe to call on any healthy "
            "HoloRouter target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "config": {"type": ["object", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="config",
        tags=("read-only", "config", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants the full Holodeck "
                "appliance configuration snapshot (vendor / product / "
                "pod ID + services block). For just the identifying "
                "fields, ``holodeck.about`` is a faster, narrower "
                "alternative. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{config: {...}}``. ``config`` is the parsed cmdlet "
                "output -- a flat dict with the appliance's identifying "
                "fields and a nested `services` block. ``error`` is set "
                "when the pwsh cmdlet failed."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.pod.list",
        handler_attr="pod_list",
        summary="List the active Holodeck nested pods.",
        description=(
            "Runs ``Get-HoloDeckPod | ConvertTo-Json -Depth 4`` over "
            "the pwsh-over-SSH transport and returns one row per active "
            "nested pod. Each row carries the pod ID, name, state, "
            "primary network, and embedded VM count. Returns a "
            "``{rows, total}`` envelope; future JSONFlux reducer will "
            "spill large pod lists to the HandleStore via the standard "
            "``result_describe`` / ``result_query`` flow. No params; "
            "safe to call on any healthy HoloRouter target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="pod",
        tags=("read-only", "pod", "list", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants the active nested-pod "
                "inventory on a HoloRouter. Each row carries the pod ID "
                "and its primary networking + VM count. Pair with "
                "``holodeck.pod.info <pod_id>`` for per-pod details. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{...pod fields...}], total: N}``. Rows are "
                "returned inline; future JSONFlux reducer paths large "
                "pod lists through ``result_describe`` / ``result_query`` "
                "via the HandleStore. ``error`` is set when the cmdlet "
                "failed."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.pod.info",
        handler_attr="pod_info",
        summary="Return per-pod detail (state, networking, VMs) for a Holodeck pod.",
        description=(
            "Runs ``Get-HoloDeckPod -Id <id> | ConvertTo-Json -Depth 4`` "
            "over the pwsh-over-SSH transport and returns the single-pod "
            "detail dict: state, networking (FRR/BGP attachment), and "
            "the embedded VM list with their power state. *pod_id* is "
            "required; use ``holodeck.pod.list`` to enumerate available "
            "IDs first."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "pod_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Holodeck pod identifier (e.g. ``HoloPod-001``). "
                        "Enumerate via ``holodeck.pod.list``."
                    ),
                },
            },
            "required": ["pod_id"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "pod": {"type": ["object", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="pod",
        tags=("read-only", "pod", "info", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants the detailed state of a "
                "specific Holodeck nested pod -- VM list, power state, "
                "networking. Use ``holodeck.pod.list`` first to "
                "discover available pod IDs. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "pod_id": (
                    "Holodeck pod identifier (e.g. ``HoloPod-001``). "
                    "Enumerate via ``holodeck.pod.list``."
                ),
            },
            "output_shape": (
                "``{pod: {id, state, networking, vms: [...]}}``. ``error`` "
                "is set when the cmdlet failed (e.g. unknown pod ID)."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.service.list",
        handler_attr="service_list",
        summary="List Photon services with the Holo* prefix and their status.",
        description=(
            "Runs ``Get-Service | Where-Object { $_.Name -like 'Holo*' "
            "} | Select-Object Name,Status,DisplayName | "
            "ConvertTo-Json -Depth 4`` over the pwsh-over-SSH "
            "transport. Returns ``{rows, total}`` where each row "
            "carries ``Name``, ``Status``, ``DisplayName``. Mirrors "
            "the cmdlet shape ``probe`` uses for "
            "``holodeck_services_down`` but exposes the raw list. "
            "No params; safe to call on any healthy HoloRouter target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="service",
        tags=("read-only", "service", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants the per-service health of "
                "the bundled Holodeck Photon services (DHCP, DNS, NTP, "
                "FRR-BGP, Webtop, K8s-in-appliance). ``holodeck.about`` "
                "and ``holodeck.config.show`` answer 'what version', "
                "this op answers 'which services are running'. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{Name, Status, DisplayName}], total: N}``. "
                "``Status`` is the PowerShell ServiceControllerStatus "
                "enum, typically rendered as ``Running`` / ``Stopped`` "
                "via ``Select-Object``."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.k8s.exec",
        handler_attr="k8s_exec",
        summary="Run a read-only kubectl command on the in-appliance Kubernetes cluster.",
        description=(
            "Forwards a **read-only** ``kubectl`` command to the K8s "
            "cluster bundled on the HoloRouter appliance. The handler "
            "fails closed when the supplied command's verb is not on "
            "the read-only safelist (allowed: ``get``, ``describe``, "
            "``logs``, ``top``, ``explain``, ``api-resources``, "
            "``api-versions``, ``cluster-info``, ``version``). Use to "
            "inspect cluster state without mutating it. The schema "
            "pattern enforces the ``kubectl <read-verb> ...`` shape at "
            "the validator layer; the handler re-checks the verb as a "
            "belt-and-braces safety gate."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": (
                        # Allowlist-shaped regex: ``kubectl``, optional
                        # global flags, a read-only verb, then a fully
                        # constrained tail. The tail's character class
                        # ``[A-Za-z0-9._/=:,@-]`` excludes every
                        # POSIX-shell metacharacter that ``shlex.split``
                        # would happily fold into a single token while
                        # leaving the shell separator semantically
                        # intact (``;`` / ``&`` / ``|`` / ``$`` /
                        # backtick / ``>`` / ``<`` / parens / ``\\``).
                        # Whitespace between tokens is constrained to
                        # ``[ \\t]`` (space or tab) so a newline can't
                        # be smuggled through ``\\s`` to introduce a
                        # second command line. The end anchor ``\\Z``
                        # is load-bearing -- without a tight end the
                        # regex would accept ``kubectl get pods; rm
                        # -rf /`` because the prefix is a valid match.
                        # Verb alternation is followed by ``(?=[ \\t]
                        # |\\Z)`` so verb prefixes (``getfoo``) don't
                        # sneak through.
                        r"\Akubectl"
                        r"([ \t]+--?[A-Za-z0-9_-]+(=[A-Za-z0-9._/=:,@-]+)?)*"
                        r"[ \t]+(get|describe|logs|top|explain|"
                        r"api-resources|api-versions|cluster-info|"
                        r"version)(?=[ \t]|\Z)"
                        r"([ \t]+[A-Za-z0-9._/=:,@-]+)*"
                        r"[ \t]*\Z"
                    ),
                    "description": (
                        "Full kubectl command line starting with "
                        "``kubectl`` and a read-only verb. Allowed "
                        "verbs: get, describe, logs, top, explain, "
                        "api-resources, api-versions, cluster-info, "
                        "version. Mutating verbs (create, apply, "
                        "delete, edit, replace, patch, scale, "
                        "rollout, label, annotate, cp, exec, "
                        "port-forward, proxy, drain, cordon) are "
                        "rejected at the schema layer. Arguments are "
                        "restricted to ``[A-Za-z0-9._/=:,@-]`` -- shell "
                        "metacharacters (``;``, ``&``, ``|``, ``$``, "
                        "backticks, ``>``, ``<``, parens, ``\\``) are "
                        "rejected before the handler is reached."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_status": {"type": ["integer", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="k8s",
        tags=("read-only", "k8s", "kubectl", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants to inspect the K8s "
                "cluster bundled on the HoloRouter appliance via "
                "``kubectl``. Only read-only verbs are allowed: ``get``, "
                "``describe``, ``logs``, ``top``, ``explain``, "
                "``api-resources``, ``api-versions``, ``cluster-info``, "
                "``version``. Mutating verbs fail closed. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "command": (
                    "Full kubectl invocation, e.g. ``kubectl get pods "
                    "-n holodeck`` or ``kubectl describe service "
                    "<name>``. Single-line; arguments tokenised via "
                    "shlex."
                ),
            },
            "output_shape": (
                "``{stdout: '<text>', stderr: '<truncated text>', "
                "exit_status: <int|null>}``. ``stderr`` is capped at "
                "4096 chars. ``error`` is set when the safety check "
                "rejected the verb or the SSH call failed."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.logs.tail",
        handler_attr="logs_tail",
        summary="Tail Holodeck runtime log files for a given component.",
        description=(
            "Runs ``tail -n <lines> /holodeck-runtime/logs/"
            "<component>*.log`` over plain SSH and returns the tail "
            "envelope. *component* is a slug (e.g. ``dhcp``, ``dns``, "
            "``frr``); *lines* defaults to 200, range [1, 5000]. "
            "Multiple files matching the glob are surfaced separately "
            "via the GNU ``tail`` ``==> path <==`` header convention. "
            "Read-only by construction; the cmd is a stock POSIX "
            "pipeline with no pwsh indirection."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9._-]+$",
                    "minLength": 1,
                    "description": (
                        "Log-file slug; the cmdline expands to "
                        "``/holodeck-runtime/logs/<component>*.log``. "
                        "Only ``[A-Za-z0-9._-]+`` accepted to keep the "
                        "shell pipeline injection-safe."
                    ),
                },
                "lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "default": 200,
                    "description": "Number of trailing lines to return per file.",
                },
            },
            "required": ["component"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": ["string", "null"]},
                            "lines": {"type": "string"},
                        },
                    },
                },
                "raw": {"type": "string"},
                "lines_requested": {"type": "integer"},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="logs",
        tags=("read-only", "logs", "tail", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants to inspect recent log "
                "lines for a specific Holodeck Photon service (``dhcp``, "
                "``dns``, ``frr``, ``webtop``, ``k8s``). Returns the "
                "last <lines> lines per matching log file. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "component": (
                    "Log-file slug; expands to "
                    "``/holodeck-runtime/logs/<component>*.log``. "
                    "Allowed chars: [A-Za-z0-9._-]+."
                ),
                "lines": "Trailing line count per file; default 200, max 5000.",
            },
            "output_shape": (
                "``{files: [{path, lines}], raw: '<full stdout>', "
                "lines_requested: N}``. When multiple log files match "
                "the glob, each is surfaced under its own ``files`` "
                "entry. Single-file matches have ``path=None`` (no "
                "GNU tail header)."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.networking.show",
        handler_attr="networking_show",
        summary="Composite FRR/BGP + DNS zone + DHCP lease snapshot for the appliance.",
        description=(
            "Runs four sub-commands over the pooled SSH connection: "
            "``vtysh -c 'show bgp summary'``, ``vtysh -c 'show ip "
            "route'``, ``pwsh`` of ``Get-DnsServerZone | "
            "ConvertTo-Json -Depth 4``, and ``cat "
            "/var/lib/dhcp/dhcpd.leases``. Returns an envelope with "
            "four narrative sub-sections (``bgp``, ``routes``, ``dns``, "
            "``dhcp``), each carrying its own ``ok`` flag so a "
            "single-component failure doesn't blank the whole "
            "response. Read-only; no mutating commands issued."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "bgp": {
                    "type": "object",
                    "properties": {
                        "summary_text": {"type": "string"},
                        "ok": {"type": "boolean"},
                    },
                },
                "routes": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "ok": {"type": "boolean"},
                    },
                },
                "dns": {
                    "type": "object",
                    "properties": {
                        "zones": {"type": "array"},
                        "total": {"type": "integer"},
                        "ok": {"type": "boolean"},
                    },
                },
                "dhcp": {
                    "type": "object",
                    "properties": {
                        "leases_text": {"type": "string"},
                        "ok": {"type": "boolean"},
                    },
                },
            },
            "additionalProperties": True,
        },
        group_key="networking",
        tags=("read-only", "networking", "bgp", "dns", "dhcp", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants a one-shot snapshot of "
                "the HoloRouter's networking surface: FRR/BGP peer "
                "summary + kernel routes + DNS zones + DHCP leases. "
                "Use ``holodeck.logs.tail component=frr`` for FRR log "
                "drill-in. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{bgp: {summary_text, ok}, routes: {text, ok}, "
                "dns: {zones, total, ok}, dhcp: {leases_text, ok}}``. "
                "Each sub-section's ``ok`` flips false when its "
                "sub-command failed or produced empty output. BGP and "
                "routes are operator-formatted text (vtysh output); "
                "DNS is structured (parsed JSON); DHCP is the raw "
                "leases file."
            ),
        },
    ),
)

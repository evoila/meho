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
  passthrough. **Read-only**: the handler fails closed when the verb
  token after ``kubectl`` is neither in :data:`_K8S_READ_VERBS`
  (single-word reads) nor a (parent, sub-verb) pair in
  :data:`_K8S_MULTIWORD_READ_VERBS` (``config view`` / ``auth can-i``
  / ...). Schema also pins ``kubectl <verb> ...`` with a ``pattern``
  so the dispatcher's validator catches the bad shape before reaching
  the handler.
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
   via :func:`shlex.split`, (c) checks for a 2-token verb prefix
   against :data:`_K8S_MULTIWORD_READ_VERBS` first, and (d) falls
   through to the single-word :data:`_K8S_READ_VERBS` safelist, and
   (e) validates **every** flag token -- before and after the verb,
   attached and separated -- against the positive
   :data:`_K8S_ALLOWED_FLAGS` allowlist so endpoint/TLS/credential/
   context overrides, the raw credential view (``--raw``) and
   file-writing options (``--output-directory``) are rejected even
   though they carry no shell metacharacter and sit behind a
   read-only verb. Any rejected step raises :exc:`KubectlSafetyError`,
   which the dispatcher's exception path turns into a
   ``result_connector_error`` envelope. The metacharacter reject is
   **load-bearing**: ``shlex.split`` in POSIX mode does not treat
   shell separators as token boundaries, so a chained payload like
   ``kubectl get pods; rm -rf /`` would otherwise tokenise to
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

from meho_backplane.connectors._shared.pwsh import PwshRunError, ps_single_quote, pwsh_run
from meho_backplane.connectors.holodeck._response_schemas import HOLODECK_RESPONSE_SCHEMAS
from meho_backplane.connectors.holodeck.ops import SSH_TRANSPORT_NOTE, HolodeckOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "GROWTH_DIRS",
    "READ_OPS",
    "KubectlSafetyError",
    "holodeck_backups_list",
    "holodeck_config_show",
    "holodeck_disk_usage",
    "holodeck_k8s_exec",
    "holodeck_logs_tail",
    "holodeck_networking_show",
    "holodeck_pod_info",
    "holodeck_pod_list",
    "holodeck_service_list",
    "parse_backup_listing_output",
    "parse_disk_usage_output",
    "parse_kubectl_command",
    "parse_logs_tail_output",
    "parse_networking_payload",
]


# ---------------------------------------------------------------------------
# Disk-usage growth-dir constant (G3.18-T1 #2153)
# ---------------------------------------------------------------------------

#: Fixed set of HoloRouter directories the ``holodeck.disk.usage`` op
#: measures with ``du`` alongside the root-fs ``df``. VCF-9.x fills the
#: 74 GB root fs via backup accumulation under ``/var/backups`` plus
#: pod-runtime growth under ``/holodeck-runtime`` (Initiative #2145);
#: those two dirs crack ~all pre-eviction incidents. The set is a
#: **code constant on purpose** -- the op takes no path parameter, so it
#: can never be coerced into an arbitrary ``du`` over an operator-chosen
#: (or attacker-chosen) path. Adding a growth dir is a code change with a
#: review, not runtime input.
GROWTH_DIRS: tuple[str, ...] = ("/var/backups", "/holodeck-runtime")


# ---------------------------------------------------------------------------
# Read-only kubectl guard
# ---------------------------------------------------------------------------

#: Whitelist of single-word ``kubectl`` verbs the handler considers
#: read-only. ``get`` / ``describe`` / ``logs`` are the three primary
#: read verbs the Initiative #371 body calls out. ``top`` (metrics) and
#: ``explain`` (schema) are pure reads. ``api-resources`` /
#: ``api-versions`` / ``cluster-info`` / ``version`` cover the
#: inspection surface without mutating cluster state. Any verb absent
#: from this set -- notably ``create`` / ``apply`` / ``delete`` /
#: ``edit`` / ``replace`` / ``patch`` / ``scale`` / ``rollout``
#: (``restart``) / ``label`` (``--overwrite``) / ``annotate`` /
#: ``cp`` / ``exec`` / ``port-forward`` / ``proxy`` / ``drain`` /
#: ``cordon`` -- fails closed.
#:
#: Multi-word inspection verbs (``config view``, ``auth can-i``,
#: ``config get-contexts``, ``auth whoami``) are handled separately
#: through :data:`_K8S_MULTIWORD_READ_VERBS`: the parent token
#: (``config`` / ``auth``) is a shared namespace where both reads and
#: writes live, so the verb-safelist check must pin both the parent
#: and the sub-verb together.
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

#: Whitelist of multi-word ``kubectl <parent> <sub>`` read-only verb
#: pairs. The single-word safelist (:data:`_K8S_READ_VERBS`) cannot
#: cover these: the parent verb (``config`` / ``auth``) is a shared
#: namespace where both read and write sub-verbs live -- e.g.
#: ``kubectl config view`` is a read but ``kubectl config set-context``
#: is a mutation. The check therefore matches on the verb + sub-verb
#: pair, with both tokens looked up in this allowlist.
#:
#: Per the kubectl reference docs (Kubernetes 1.x current generated
#: docs, see https://kubernetes.io/docs/reference/kubectl/generated/
#: kubectl_config/ and https://kubernetes.io/docs/reference/access-
#: authn-authz/authorization/#checking-api-access), the read-only
#: ``config`` sub-verbs are ``view`` / ``get-contexts`` /
#: ``get-clusters`` / ``get-users`` / ``current-context``; the
#: read-only ``auth`` sub-verbs are ``can-i`` / ``whoami``. Adjacent
#: mutating sub-verbs (``set-context``, ``set-cluster``,
#: ``set-credentials``, ``unset``, ``use-context``, ``delete-context``,
#: ``delete-cluster``, ``delete-user``, ``rename-context`` under
#: ``config``; ``reconcile`` under ``auth``) are deliberately **not**
#: present and fail closed -- a 2-token verb prefix walks this dict,
#: hits the parent, then checks the sub-verb against the frozenset;
#: absence is rejection.
_K8S_MULTIWORD_READ_VERBS: dict[str, frozenset[str]] = {
    "config": frozenset(
        {
            "view",
            "get-contexts",
            "get-clusters",
            "get-users",
            "current-context",
        }
    ),
    "auth": frozenset(
        {
            "can-i",
            "whoami",
        }
    ),
}

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

#: Positive allowlist of ``kubectl`` flags accepted anywhere in a
#: read invocation -- both the "global" position before the verb and
#: the argument position after it. Every flag **not** in this set is
#: rejected, which closes the argument-level injection class that a
#: verb safelist plus a shell-metacharacter reject leave open:
#:
#: * **Endpoint / TLS-trust / credential overrides** -- ``--server`` /
#:   ``-s``, ``--kubeconfig``, ``--token``, ``--username`` /
#:   ``--password``, ``--client-certificate`` / ``--client-key``,
#:   ``--certificate-authority``, ``--insecure-skip-tls-verify``,
#:   ``--tls-server-name``, ``--as`` / ``--as-group`` / ``--as-uid``.
#:   These would let the caller point the appliance's bearer
#:   credentials at an endpoint of their choosing or disable TLS
#:   verification. Endpoint, context, credential and TLS trust are
#:   owned by the appliance's own kubeconfig, never by the caller.
#: * **Context / cluster / user selection** -- ``--context`` /
#:   ``--cluster`` / ``--user`` pick a different identity out of the
#:   kubeconfig; the appliance's default context is the only one the
#:   caller may exercise.
#: * **Raw credential view** -- ``--raw`` on ``config view`` prints
#:   client certs / bearer tokens verbatim.
#: * **File-writing options** -- ``--output-directory`` (``cluster-info
#:   dump``), ``--output-file``, ``--cache-dir``, ``--profile-output``
#:   write on the appliance filesystem.
#:
#: The allowlisted flags are all read-scoping / output-formatting
#: options that neither redirect the connection nor touch disk. See
#: the kubectl reference for the read/global flag split:
#: https://kubernetes.io/docs/reference/kubectl/.
_K8S_ALLOWED_FLAGS: frozenset[str] = frozenset(
    {
        # Scoping.
        "-n",
        "--namespace",
        "-A",
        "--all-namespaces",
        "-l",
        "--selector",
        "--field-selector",
        # Output formatting -- read-only, never writes to disk.
        "-o",
        "--output",
        "--sort-by",
        "-L",
        "--label-columns",
        "--show-labels",
        "--show-kind",
        "--no-headers",
        "--ignore-not-found",
        "--chunk-size",
        # get / explain.
        "--recursive",
        "--api-version",
        "--subresource",
        # logs.
        "-c",
        "--container",
        "--all-containers",
        "--tail",
        "--since",
        "--since-time",
        "--timestamps",
        "-p",
        "--previous",
        "--prefix",
        "--limit-bytes",
        "--max-log-requests",
        # describe.
        "--show-events",
        # top.
        "--containers",
        # auth can-i.
        "--list",
        "-q",
        "--quiet",
    }
)

#: Subset of :data:`_K8S_ALLOWED_FLAGS` that consume a following value
#: token when written in the **separated** form (``--namespace
#: holodeck`` rather than ``--namespace=holodeck``). The walk needs
#: this to step over the value while locating the verb -- otherwise a
#: value like ``holodeck`` in ``kubectl -n holodeck get pods`` would be
#: mistaken for the verb. Boolean flags (``-A``, ``--show-labels``,
#: ...) are absent, so the token after them is treated as the verb /
#: a positional. A separated value is only swallowed when it does not
#: itself start with ``-`` (so ``--namespace --server=evil`` does not
#: hide ``--server`` as a value -- the ``--server`` token is then
#: validated and rejected on the next step; negative numeric values
#: must use the attached form, e.g. ``--tail=-1``).
_K8S_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-n",
        "--namespace",
        "-l",
        "--selector",
        "--field-selector",
        "-o",
        "--output",
        "--sort-by",
        "-L",
        "--label-columns",
        "--chunk-size",
        "--api-version",
        "--subresource",
        "-c",
        "--container",
        "--tail",
        "--since",
        "--since-time",
        "--limit-bytes",
        "--max-log-requests",
    }
)


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


def _validate_flag(token: str) -> bool:
    """Validate one ``-``-prefixed *token* against :data:`_K8S_ALLOWED_FLAGS`.

    The flag name is the part before an ``=`` (``--namespace`` for both
    ``--namespace=x`` and ``--namespace x``). A name absent from the
    allowlist raises :exc:`KubectlSafetyError` -- this is the gate that
    rejects endpoint / TLS / credential / context overrides, the raw
    credential view and file-writing options that a verb safelist alone
    lets through.

    Returns ``True`` when the flag takes a value in the **separated**
    form (name is in :data:`_K8S_VALUE_FLAGS` and the token carried no
    attached ``=value``), so the caller knows a following value token
    may need to be stepped over. The returned message never echoes a
    flag value -- only the flag name, which is not sensitive.
    """
    name = token.split("=", 1)[0]
    if name not in _K8S_ALLOWED_FLAGS:
        raise KubectlSafetyError(
            f"kubectl flag {name!r} is not on the read-only allowlist; "
            "endpoint (--server), TLS-trust, credential (--token, "
            "--kubeconfig), context/cluster/user, raw-credential "
            "(--raw) and file-writing (--output-directory) overrides "
            "are rejected -- those are owned by the appliance config"
        )
    return "=" not in token and name in _K8S_VALUE_FLAGS


def _skip_global_flags(tokens: list[str]) -> int:
    """Return the index of the first non-flag token after ``kubectl``.

    Walks past leading flag tokens, **validating each against
    :data:`_K8S_ALLOWED_FLAGS`** (via :func:`_validate_flag`) so a
    global-position credential/endpoint override is refused before the
    verb is even reached. Attached-value flags (``--namespace=foo``)
    consume one token; separated-value flags (``--namespace foo``)
    consume two -- but a separated value that itself starts with ``-``
    is not swallowed, so it is validated as its own flag on the next
    iteration rather than hiding a rejected flag as a value. The
    returned index points at the verb -- or off the end of ``tokens``
    when there is no verb. Raises :exc:`KubectlSafetyError` on the
    first disallowed flag.
    """
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        consumes_value = _validate_flag(tokens[idx])
        idx += 1
        if consumes_value and idx < len(tokens) and not tokens[idx].startswith("-"):
            # Separated flag value (``--namespace foo``); step over it.
            idx += 1
    return idx


def _reject_disallowed_arg_flags(args: list[str]) -> None:
    """Validate every ``-``-prefixed token among post-verb *args*.

    Positional tokens (resource kinds / names) pass untouched -- they
    were already screened for shell metacharacters in
    :func:`parse_kubectl_command`. Flag tokens go through
    :func:`_validate_flag`, so an argument-position override
    (``kubectl get pods --server=… --insecure-skip-tls-verify``,
    ``kubectl config view --raw``, ``kubectl cluster-info dump
    --output-directory=/tmp``) is rejected exactly like a global-position
    one. Raises :exc:`KubectlSafetyError` on the first disallowed flag.
    """
    for token in args:
        if token.startswith("-"):
            _validate_flag(token)


def _check_multiword_verb(tokens: list[str], idx: int, verb: str) -> tuple[str, list[str]]:
    """Validate the 2-token ``(parent, sub-verb)`` prefix; return ``(canonical, args)``.

    Pre-condition: ``verb == tokens[idx]`` AND ``verb in
    _K8S_MULTIWORD_READ_VERBS``. Raises :exc:`KubectlSafetyError`
    when the sub-verb is missing or not on the per-parent
    frozenset. The canonical form returned is the space-joined
    ``"<parent> <sub>"`` so callers can re-serialise; ``args`` is
    every token after the sub-verb.
    """
    allowed_subs = _K8S_MULTIWORD_READ_VERBS[verb]
    if idx + 1 >= len(tokens):
        raise KubectlSafetyError(
            f"kubectl multi-word verb {verb!r} requires a sub-verb; allowed: {sorted(allowed_subs)}"
        )
    sub_verb = tokens[idx + 1]
    if sub_verb not in allowed_subs:
        raise KubectlSafetyError(
            f"kubectl sub-verb {sub_verb!r} under {verb!r} is not on "
            f"the read-only safelist; allowed: {sorted(allowed_subs)}"
        )
    return f"{verb} {sub_verb}", tokens[idx + 2 :]


def parse_kubectl_command(command: str) -> tuple[str, list[str]]:
    """Parse ``command`` into ``(verb, args)``; enforce the read-only safelist.

    The first whitespace-separated token must be ``kubectl``. Every
    flag -- in the global position before the verb (validated while
    :func:`_skip_global_flags` locates the verb) and in the argument
    position after it (validated by :func:`_reject_disallowed_arg_flags`)
    -- is checked against the positive :data:`_K8S_ALLOWED_FLAGS`
    allowlist. Endpoint (``--server``), TLS-trust
    (``--insecure-skip-tls-verify``, ``--certificate-authority`` …),
    credential (``--token``, ``--kubeconfig`` …), context/cluster/user
    selection, the raw credential view (``--raw``) and file-writing
    (``--output-directory``, ``--output-file`` …) flags are all
    off-list and rejected, in both attached (``--flag=value``) and
    separated (``--flag value``) forms; those settings are owned by the
    appliance's own kubeconfig, not the caller's command line. After
    walking past the (now-validated) global flags, the verb is matched
    against two safelists in order:

    1. **Multi-word prefix first** (:func:`_check_multiword_verb`).
       If ``tokens[idx]`` is a key in :data:`_K8S_MULTIWORD_READ_VERBS`
       (``config`` / ``auth``) AND ``tokens[idx + 1]`` is in the
       corresponding sub-verb frozenset, the command is accepted; the
       canonical verb returned is the space-joined ``"<parent> <sub>"``
       form and ``args`` is everything after the sub-verb. A parent
       with no sub-verb, or a sub-verb not on the per-parent
       frozenset (``config set-context``, ``auth reconcile``), is
       rejected -- the parent-only fallthrough is intentionally NOT
       attempted, because the parent token is never a legal read verb
       on its own.
    2. **Single-word fallback.** Otherwise the verb is matched against
       :data:`_K8S_READ_VERBS` (``get`` / ``describe`` / ``logs`` …).

    Returns ``(verb, args)`` so callers can re-serialise the command
    for SSH; for multi-word verbs ``verb`` is the space-joined form
    (``"config view"``).

    Tokenisation runs via :func:`shlex.split` so quoted resource names
    (``"my pod"``) survive the parse. POSIX-shell metacharacters
    (:data:`_SHELL_METACHARS_RE`) are rejected *before* tokenisation --
    load-bearing, since ``shlex.split`` does not split on shell
    separators, so an un-scanned ``kubectl get pods; rm -rf /`` would
    reach the remote login shell verbatim (see the module docstring).
    Empty / metachar-bearing / non-``kubectl`` / safelist- or
    allowlist-misses all raise :exc:`KubectlSafetyError` before any SSH
    traffic happens.

    Examples
    --------

    >>> parse_kubectl_command("kubectl get pods -n holodeck")
    ('get', ['pods', '-n', 'holodeck'])
    >>> parse_kubectl_command("kubectl config view")
    ('config view', [])
    >>> parse_kubectl_command("kubectl auth can-i list pods")
    ('auth can-i', ['list', 'pods'])
    """
    if not command or not command.strip():
        raise KubectlSafetyError("kubectl command is empty")
    # Metacharacter rejection -- load-bearing security gate. Don't echo
    # the offending character back (operator-visible surface).
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
    idx = _skip_global_flags(tokens)
    if idx >= len(tokens):
        raise KubectlSafetyError("kubectl command has no verb after global flags")
    verb = tokens[idx]
    # Multi-word prefix path checked first. The parent token
    # (``config`` / ``auth``) is a shared namespace where both read
    # and write sub-verbs live, so we MUST pin the (parent, sub)
    # pair together -- a fallthrough to the single-word check on the
    # parent alone would silently approve ``kubectl config set-context``
    # by way of the ``config`` parent being legible. The single-word
    # safelist does NOT include the multi-word parents, so a missing
    # sub-verb fails closed via ``_check_multiword_verb``.
    if verb in _K8S_MULTIWORD_READ_VERBS:
        canonical, args = _check_multiword_verb(tokens, idx, verb)
        _reject_disallowed_arg_flags(args)
        return canonical, args
    if verb not in _K8S_READ_VERBS:
        raise KubectlSafetyError(
            f"kubectl verb {verb!r} is not on the read-only safelist; "
            f"allowed: {sorted(_K8S_READ_VERBS)}"
        )
    args = tokens[idx + 1 :]
    _reject_disallowed_arg_flags(args)
    return verb, args


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


def _parse_df_root(df_root_text: str) -> dict[str, Any]:
    """Parse ``df -B1 /`` byte-count output into the root-fs sub-section.

    ``df -B1`` reports every column in bytes (``-B1`` = 1-byte block
    size) so the envelope never has to reverse a human-readable suffix.
    The expected two-line shape is::

        Filesystem  1B-blocks  Used  Available Use% Mounted on
        /dev/md2    467909804032  58598952960  385467109376  14%  /

    The parser skips the header, reads the last non-empty data row
    (a device path may soft-wrap onto its own line on very long
    device names, in which case the counts land on the following
    row -- taking the last row with >=4 trailing numeric-ish fields
    is robust to that), and pulls total / used / available from the
    1B-block columns. ``percent_used`` is computed from the byte
    counts rather than trusting ``df``'s rounded ``Use%`` column, so
    a scheduler polling this value sees a stable float.

    Returns ``{"ok": False}`` (with null counts) when the output is
    empty or unparseable -- mirroring the ``networking.show`` per
    sub-section ``ok`` convention so one failed sub-command never
    blanks the others.
    """
    failed: dict[str, Any] = {
        "total_bytes": None,
        "used_bytes": None,
        "avail_bytes": None,
        "percent_used": None,
        "ok": False,
    }
    if not df_root_text.strip():
        return failed
    data_row: list[str] | None = None
    for line in df_root_text.splitlines():
        fields = line.split()
        if not fields or fields[0].lower() == "filesystem":
            continue
        # A valid data row carries at least: device, total, used,
        # avail, use%, mount. Require the three byte columns to be
        # digit-only so a wrapped device-name-only line is skipped.
        if len(fields) >= 4 and fields[1].isdigit() and fields[2].isdigit() and fields[3].isdigit():
            data_row = fields
    if data_row is None:
        return failed
    total = int(data_row[1])
    used = int(data_row[2])
    avail = int(data_row[3])
    # Percent-used off the byte counts (df's Use% is rounded). Guard
    # a zero-total (unmounted / pseudo fs) so we never divide by zero.
    percent_used = round((used / total) * 100.0, 2) if total > 0 else None
    return {
        "total_bytes": total,
        "used_bytes": used,
        "avail_bytes": avail,
        "percent_used": percent_used,
        "ok": True,
    }


def _parse_du_bytes(path: str, du_text: str) -> dict[str, Any]:
    """Parse ``du -sb <dir>`` output into one growth-dir sub-section.

    ``du -sb`` prints a single line ``<bytes>\\t<path>``. The first
    whitespace-delimited field is the apparent size in bytes. Empty
    output (the sub-command failed -- ``_safe_run_text`` maps a
    missing dir / SSH error to ``""``) or a non-numeric first field
    yields ``ok: False`` for this entry without touching the others.
    """
    stripped = du_text.strip()
    if not stripped:
        return {"path": path, "used_bytes": None, "ok": False}
    first_field = stripped.split()[0]
    if not first_field.isdigit():
        return {"path": path, "used_bytes": None, "ok": False}
    return {"path": path, "used_bytes": int(first_field), "ok": True}


def parse_disk_usage_output(
    *,
    df_root_text: str,
    dir_usages: list[tuple[str, str]],
) -> dict[str, Any]:
    """Compose the ``holodeck.disk.usage`` envelope from raw command text.

    Inputs:

    * ``df_root_text`` -- stdout of ``df -B1 /``.
    * ``dir_usages`` -- ``[(path, du_stdout), ...]`` in
      :data:`GROWTH_DIRS` order; one ``du -sb`` run per growth dir.

    Returns:

    .. code-block:: python

        {
            "root_fs": {
                "total_bytes": 467909804032,
                "used_bytes": 58598952960,
                "avail_bytes": 385467109376,
                "percent_used": 12.52,
                "ok": True,
            },
            "growth_dirs": [
                {"path": "/var/backups", "used_bytes": 40000000000, "ok": True},
                {"path": "/holodeck-runtime", "used_bytes": None, "ok": False},
            ],
        }

    Each sub-section carries its own ``ok`` flag; a failed ``df`` or a
    single failed ``du`` (empty / unparseable output) flips only that
    entry's ``ok`` to ``False`` while the rest of the envelope stays
    populated -- the same failure-isolation contract
    :func:`parse_networking_payload` follows for ``networking.show``.
    """
    return {
        "root_fs": _parse_df_root(df_root_text),
        "growth_dirs": [_parse_du_bytes(path, du_text) for path, du_text in dir_usages],
    }


def parse_backup_listing_output(stdout: str) -> list[dict[str, Any]]:
    """Parse ``find ... -printf '%T@ %s %p\\n' | sort -rn`` output into rows.

    Each non-empty line carries three fields in the order the ``-printf``
    format emits them, separated by a single space:

    * ``%T@`` -- the file's modification time as seconds since the epoch,
      *with a fractional part* (GNU findutils ``find(1)``); parsed to a
      float.
    * ``%s`` -- the file's size in bytes; parsed to an int.
    * ``%p`` -- the file path. It is the trailing field and may itself
      contain spaces, so only the first two fields are split off; the rest
      of the line is the path verbatim.

    The remote pipeline already orders newest-first (``sort -rn`` on the
    numeric ``%T@`` field -- the same ordering ``holodeck.backups.prune``
    uses internally to pick its keep window), so this parser **preserves**
    input order rather than re-sorting. A row whose mtime or size field is
    non-numeric is skipped, mirroring the :func:`_parse_du_bytes`
    non-numeric guard so a stray line never crashes the envelope.

    Returns ``[{path, mtime, size_bytes}, ...]`` newest-first.

    >>> rows = parse_backup_listing_output(
    ...     "1699999999.5 4096 /var/backups/db-2.sql.gz\\n"
    ...     "1699990000.1 8192 /var/backups/db-1.sql.gz\\n"
    ... )
    >>> [r["path"] for r in rows]
    ['/var/backups/db-2.sql.gz', '/var/backups/db-1.sql.gz']
    >>> rows[0]["size_bytes"]
    4096
    """
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        # Split on the two literal separator spaces the ``-printf`` format
        # emits; maxsplit=2 keeps a path containing spaces intact as the
        # trailing field.
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        mtime_field, size_field, path = parts
        try:
            mtime = float(mtime_field)
            size_bytes = int(size_field)
        except ValueError:
            continue
        rows.append({"path": path, "mtime": mtime, "size_bytes": size_bytes})
    return rows


# ---------------------------------------------------------------------------
# Handler functions (bound-method shims on HolodeckConnector)
# ---------------------------------------------------------------------------


async def holodeck_config_show(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
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
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"config": None, "error": str(exc)}
    return {"config": payload}


async def holodeck_pod_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
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
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    rows = _normalise_pwsh_json_array(payload)
    return {"rows": rows, "total": len(rows)}


async def holodeck_pod_info(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
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
    # PowerShell single-quoting via the shared helper (it escapes every
    # character PowerShell reads as a single quote). The encoded-command
    # contract means the appliance shell never sees the value as a discrete
    # token; the entire pwsh script body is base64'd before transport.
    script = f"Get-HoloDeckPod -Id {ps_single_quote(pod_id)} | ConvertTo-Json -Depth 4"
    try:
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"pod": None, "error": str(exc)}
    return {"pod": payload}


async def holodeck_service_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
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
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    rows = _normalise_pwsh_json_array(payload)
    return {"rows": rows, "total": len(rows)}


async def holodeck_k8s_exec(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Forward a **read-only** ``kubectl`` command to the in-appliance K8s.

    Op-id: ``holodeck.k8s.exec``. *params* carries ``command`` (a
    string starting with ``kubectl <read-verb> ...``). The handler:

    1. Parses the command via :func:`parse_kubectl_command`; shell
       metacharacters, mutating verbs and off-allowlist flags
       (endpoint/TLS/credential/context overrides, ``--raw``,
       file-writing options) raise :exc:`KubectlSafetyError`
       **before** any SSH transport is touched. The exception is
       folded into a ``result_connector_error`` envelope by the
       structured-error path below.
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
    # bounds the wall clock at 30s by default. Auth / transport
    # failures propagate to the dispatcher's ``connector_error``
    # branch (mirroring ``holodeck.about``) — swallowing them into a
    # ``status="ok"`` envelope with empty stdout invites an agent to
    # act on hollow output (#2155).
    proc = await self._run_command(target, raw_command, operator=operator)
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
    operator: Operator | None = None,
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
        proc = await self._run_command(target, cmd, operator=operator)
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
    operator: Operator | None = None,
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

    bgp_text = await _safe_run_text(self, target, "vtysh -c 'show bgp summary'", operator)
    routes_text = await _safe_run_text(self, target, "vtysh -c 'show ip route'", operator)
    dns_zones_payload: Any
    try:
        dns_zones_payload = await pwsh_run(
            self,
            target,
            "Get-DnsServerZone | Select-Object ZoneName,ZoneType | ConvertTo-Json -Depth 4",
            operator=operator,
        )
    except PwshRunError:
        dns_zones_payload = None
    dhcp_text = await _safe_run_text(self, target, "cat /var/lib/dhcp/dhcpd.leases", operator)

    return parse_networking_payload(
        bgp_text=bgp_text,
        routes_text=routes_text,
        dns_zones_json=dns_zones_payload,
        dhcp_leases_text=dhcp_text,
    )


async def holodeck_disk_usage(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Report root-fs usage plus the fixed growth-dir set.

    Op-id: ``holodeck.disk.usage``. Runs, over the pooled SSH
    connection:

    1. ``df -B1 /`` -- root-fs total / used / available in bytes.
    2. ``du -sb <dir>`` for each dir in :data:`GROWTH_DIRS`
       (``/var/backups``, ``/holodeck-runtime``) -- the two known
       VCF-9.x root-fill sources.

    The op takes **no** path parameter: the growth-dir set is the
    :data:`GROWTH_DIRS` module constant, so this can never become an
    arbitrary ``du`` over operator-supplied input. Each dir token is
    still ``shlex.quote``- d before interpolation as belt-and-braces
    (the constants contain no metacharacters today, but a future edit
    to the constant is protected from accidentally breaking the
    quoting invariant).

    Sub-command failures are isolated the way ``networking.show``
    isolates its four sub-sections: each runs through
    :func:`_safe_run_text` (SSH / OSError -> empty string), and
    :func:`parse_disk_usage_output` flips only the empty entry's
    ``ok`` to ``False`` -- a missing ``/var/backups`` never blanks
    the root-fs numbers or the other growth dir. Read-only; issues
    no mutating command.
    """
    del params  # declared empty; intentionally ignored

    df_root_text = await _safe_run_text(self, target, "df -B1 /", operator)
    dir_usages: list[tuple[str, str]] = []
    for path in GROWTH_DIRS:
        du_text = await _safe_run_text(self, target, f"du -sb {shlex.quote(path)}", operator)
        dir_usages.append((path, du_text))

    return parse_disk_usage_output(df_root_text=df_root_text, dir_usages=dir_usages)


async def holodeck_backups_list(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """List backup artefacts under ``/var/backups`` newest-first.

    Op-id: ``holodeck.backups.list``. *params*:

    * ``path`` -- optional sub-path under ``/var/backups`` (absolute or
      relative to it). Resolved via
      :func:`~meho_backplane.connectors.holodeck.ops_write.resolve_backup_dir`,
      the **same** bound ``holodeck.backups.prune`` applies -- a traversal
      or absolute-path escape is rejected before any SSH traffic. Omit to
      list the root.

    Runs ``find <dir> -maxdepth 1 -type f -printf '%T@ %s %p\\n' | sort
    -rn`` over plain SSH -- the read-only half of the prune handler's own
    listing pipeline, with a ``%s`` (size) field added and the destructive
    ``tail | cut | xargs rm`` tail dropped. Returns a ``{rows, total}``
    envelope (:func:`parse_backup_listing_output`) whose rows are ordered
    newest-first, exactly the ordering ``backups.prune`` uses internally to
    decide its keep window -- so a caller can pass the same ``keep_newest``
    a prune request proposes and locally read off which rows (index >=
    ``keep_newest``) that prune would delete, without executing anything.

    An SSH/transport failure surfaces as
    ``{rows: [], total: 0, error: "<reason>"}`` so an empty result caused
    by a broken connection is distinguishable from a genuinely empty
    directory (the backup-landing health-check signal).
    """
    # Reuse the prune handler's own path bound -- imported lazily to keep
    # the ops / ops_read / ops_write module triangle free of an import
    # cycle (the same convention connector.py and ops._holodeck_ops use).
    from meho_backplane.connectors.holodeck.ops_write import (
        HolodeckWriteSafetyError,
        resolve_backup_dir,
    )

    try:
        resolved_dir = resolve_backup_dir(params.get("path"))
    except HolodeckWriteSafetyError as exc:
        return {"rows": [], "total": 0, "error": f"backups.list safety check: {exc}"}

    quoted_dir = shlex.quote(resolved_dir)
    cmd = f"find {quoted_dir} -maxdepth 1 -type f -printf '%T@ %s %p\\n' | sort -rn"
    try:
        proc = await self._run_command(target, cmd, operator=operator)
    except Exception as exc:
        return {"rows": [], "total": 0, "error": str(exc)}
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    rows = parse_backup_listing_output(content)
    return {"rows": rows, "total": len(rows)}


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
    operator: Operator | None = None,
) -> str:
    """Run *cmd* via plain SSH, return stdout text; failures -> empty string.

    The networking composer keeps each sub-command's failure isolated;
    a ``vtysh`` crash on the BGP summary path must not blank the DNS
    or DHCP sub-sections. The handler relies on the empty-string
    convention -- :func:`parse_networking_payload` flips the
    sub-section's ``ok`` to ``False`` when the input is empty.
    """
    try:
        proc = await self._run_command(target, cmd, operator=operator)
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
    "examples": [{}],
}


#: Module-local alias for the canonical SSH-only/pwsh transport reminder
#: hoisted onto :mod:`~meho_backplane.connectors.holodeck.ops` by
#: G3.8-T3 (#855) so the T1 canary op (``holodeck.about``) shares the
#: same string verbatim with the 7 T2 read ops below. The alias keeps
#: every existing reference in this file readable without churn.
_SSH_TRANSPORT_NOTE: str = SSH_TRANSPORT_NOTE


_R = HOLODECK_RESPONSE_SCHEMAS

READ_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id="holodeck.config.show",
        handler_attr="config_show",
        summary="Show the Holodeck appliance configuration.",
        description=(
            "Runs Get-HoloDeckConfig on the HoloRouter and returns its answer as "
            "'config'. Read-only. For just the version and pod id, holodeck.about "
            "is smaller."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_R["holodeck.config.show"],
        group_key="config",
        tags=("read-only", "config", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need the full Holodeck configuration of the "
                "appliance. For just the version and pod id, call "
                "holodeck.about. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{config, error}. 'config' is the cmdlet answer as the appliance "
                "returns it (MEHO reads 'Version' and 'PodId' from it). 'config' "
                "is null and 'error' is set when the cmdlet failed."
            ),
            "next_step": (
                "To see the pods, call holodeck.pod.list. To see a deploy, call "
                "holodeck.instance.status."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.pod.list",
        handler_attr="pod_list",
        summary="List the Holodeck pods on the appliance.",
        description=(
            "Runs Get-HoloDeckPod on the HoloRouter and returns one row per pod "
            "as {rows, total}. Read-only. A long list comes back as a link to the "
            "stored result."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_R["holodeck.pod.list"],
        group_key="pod",
        tags=("read-only", "pod", "list", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need the list of Holodeck pods on a HoloRouter, "
                "for example to find a pod id. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{rows, total, error}. Each row is a pod object as Get-HoloDeckPod "
                "returns it; MEHO does not rename its keys. 'error' is set (rows "
                "empty) when the cmdlet failed. A long list is returned as a link "
                "to the stored result; read it with result_query."
            ),
            "next_step": "For one pod in detail, call holodeck.pod.info with its id.",
        },
    ),
    HolodeckOp(
        op_id="holodeck.pod.info",
        handler_attr="pod_info",
        summary="Show the details of one Holodeck pod.",
        description=(
            "Runs Get-HoloDeckPod -Id <pod_id> on the HoloRouter and returns the "
            "pod as 'pod'. Read-only. Get the id from holodeck.pod.list."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "pod_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The Holodeck pod id. Get it from holodeck.pod.list.",
                },
            },
            "required": ["pod_id"],
            "additionalProperties": False,
            "examples": [{"pod_id": "<pod-id>"}],
        },
        response_schema=_R["holodeck.pod.info"],
        group_key="pod",
        tags=("read-only", "pod", "info", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need the state of one Holodeck pod, for example "
                "its VMs and network. Call holodeck.pod.list first to get the "
                "id. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "pod_id": "The pod id from holodeck.pod.list.",
            },
            "output_shape": (
                "{pod, error}. 'pod' is the cmdlet answer as the appliance returns "
                "it. 'pod' is null and 'error' is set when the cmdlet failed, for "
                "example for an unknown id."
            ),
            "next_step": ("For the network view of the appliance, call holodeck.networking.show."),
        },
    ),
    HolodeckOp(
        op_id="holodeck.service.list",
        handler_attr="service_list",
        summary="List the Holodeck services on the appliance and their state.",
        description=(
            "Runs Get-Service on the HoloRouter for services whose name starts "
            "with 'Holo' and returns {rows, total} with Name, Status and "
            "DisplayName. Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_R["holodeck.service.list"],
        group_key="service",
        tags=("read-only", "service", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need to know which Holodeck services run on the "
                "appliance. holodeck.about tells you the version; this op tells "
                "you what is running. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{rows: [{Name, Status, DisplayName}], total, error}. 'Status' is "
                "'Running' or 'Stopped', or the matching number (4 is running)."
            ),
            "next_step": (
                "For a service that is not running, read its log with holodeck.logs.tail."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.k8s.exec",
        handler_attr="k8s_exec",
        summary="Run a read-only kubectl command on the appliance's Kubernetes cluster.",
        description=(
            "Runs one read-only kubectl command (get, describe, logs, ...) on the "
            "HoloRouter and returns its output. Commands that change the cluster "
            "are refused. A person must approve each call, because the command "
            "line comes from the caller."
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
                        #
                        # Multi-word verb literals (``config view`` /
                        # ``auth can-i`` / ...) appear FIRST in the
                        # alternation. The regex engine tries
                        # alternations left-to-right, so listing the
                        # longer multi-word literal before the
                        # single-word ``config`` / ``auth`` ensures
                        # the engine commits to the multi-word match
                        # when both could apply -- this matches the
                        # handler-layer ordering in
                        # :func:`parse_kubectl_command`. The
                        # single-word safelist does NOT include
                        # ``config`` or ``auth``, so a misshapen
                        # ``kubectl config set-context`` falls through
                        # both alternation branches and is rejected
                        # at the schema layer too.
                        r"\Akubectl"
                        r"([ \t]+--?[A-Za-z0-9_-]+(=[A-Za-z0-9._/=:,@-]+)?)*"
                        r"[ \t]+("
                        r"config[ \t]+(view|get-contexts|get-clusters|"
                        r"get-users|current-context)|"
                        r"auth[ \t]+(can-i|whoami)|"
                        r"get|describe|logs|top|explain|"
                        r"api-resources|api-versions|cluster-info|"
                        r"version)(?=[ \t]|\Z)"
                        r"([ \t]+[A-Za-z0-9._/=:,@-]+)*"
                        r"[ \t]*\Z"
                    ),
                    "description": (
                        "One kubectl command line that starts with 'kubectl' and a "
                        "read-only verb: get, describe, logs, top, explain, "
                        "api-resources, api-versions, cluster-info, version, "
                        "config view|get-contexts|get-clusters|get-users|"
                        "current-context, auth can-i|whoami. Arguments may use "
                        "only letters, digits and . _ / = : , @ -"
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
            "examples": [{"command": "kubectl get pods -n kube-system -o wide"}],
        },
        response_schema=_R["holodeck.k8s.exec"],
        group_key="k8s",
        tags=("k8s", "kubectl", "holodeck", "approval-gated"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call when you need to look at the Kubernetes cluster that runs "
                "on the HoloRouter appliance. Only reads are allowed. Allowed "
                "verbs: get, describe, logs, top, explain, api-resources, "
                "api-versions, cluster-info, version; and config view, config "
                "get-contexts, config get-clusters, config get-users, config "
                "current-context, auth can-i, auth whoami. Verbs that change "
                "the cluster (create, apply, delete, edit, patch, scale, ...) "
                "are refused. Only flags that pick or format what you read are "
                "allowed (-n, -A, -l, --field-selector, -o, --tail, -c, ...). "
                "Flags that change where kubectl connects or which credentials "
                "it uses (--server, --token, --kubeconfig, --context, "
                "--insecure-skip-tls-verify), --raw and flags that write files "
                "are refused. " + _SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call before the command runs. Write "
                "the command on one line, with no shell characters such as ; | "
                "& $ > < or quotes."
            ),
            "parameter_hints": {
                "command": (
                    "For example 'kubectl get pods -n kube-system', 'kubectl "
                    "describe service <name>', 'kubectl config view' or "
                    "'kubectl auth can-i list pods'."
                ),
            },
            "output_shape": (
                "{stdout, stderr, exit_status, error}. 'stderr' is cut at 4096 "
                "characters. 'error' is set when the safety check refused the verb "
                "or a flag."
            ),
            "next_step": (
                "To clean up pods that have ended, call holodeck.k8s.pods.gc. "
                "For appliance logs, call holodeck.logs.tail."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.logs.tail",
        handler_attr="logs_tail",
        summary="Show the last lines of the Holodeck log files of one component.",
        description=(
            "Runs 'tail -n <lines>' on /holodeck-runtime/logs/<component>*.log "
            "on the HoloRouter and returns the text per file. Read-only. 'lines' "
            "is 200 by default, at most 5000."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9._-]+$",
                    "minLength": 1,
                    "description": (
                        "Start of the log file name, for example 'dhcp', 'dns' or "
                        "'frr'. MEHO reads /holodeck-runtime/logs/<component>*.log. "
                        "Only letters, digits and . _ - are allowed."
                    ),
                },
                "lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "default": 200,
                    "description": "Number of last lines to return per file.",
                },
            },
            "required": ["component"],
            "additionalProperties": False,
            "examples": [{"component": "dhcp", "lines": 100}],
        },
        response_schema=_R["holodeck.logs.tail"],
        group_key="logs",
        tags=("read-only", "logs", "tail", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need the recent log lines of one Holodeck "
                "component, for example dhcp, dns, frr, webtop or k8s. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "component": (
                    "Start of the log file name; MEHO reads "
                    "/holodeck-runtime/logs/<component>*.log."
                ),
                "lines": "Last lines per file; 200 by default, at most 5000.",
            },
            "output_shape": (
                "{files: [{path, lines}], raw, lines_requested, error}. When "
                "several files match, each has its own entry. When one file "
                "matches, 'path' is null. An empty 'files' list means no file "
                "matched."
            ),
            "next_step": (
                "For the deploy state, call holodeck.instance.status. For the "
                "network view, call holodeck.networking.show."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.networking.show",
        handler_attr="networking_show",
        summary="Show the HoloRouter's BGP peers, routes, DNS zones and DHCP leases.",
        description=(
            "Runs four reads on the HoloRouter: BGP summary and routes (vtysh), "
            "DNS zones (Get-DnsServerZone) and DHCP leases (dhcpd.leases). Each "
            "part has its own 'ok' flag, so one failed part does not hide the "
            "others."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_R["holodeck.networking.show"],
        group_key="networking",
        tags=("read-only", "networking", "bgp", "dns", "dhcp", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need a picture of the HoloRouter network: BGP "
                "peers, routes, DNS zones and DHCP leases, in one call. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{bgp: {summary_text, ok}, routes: {text, ok}, dns: {zones, total, "
                "ok}, dhcp: {leases_text, ok}}. BGP and routes are vtysh text, DNS "
                "is a list of {ZoneName, ZoneType}, DHCP is the raw leases file. "
                "'ok' is false when that part failed or printed nothing."
            ),
            "next_step": (
                "For BGP problems, read the FRR log with holodeck.logs.tail component=frr."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.disk.usage",
        handler_attr="disk_usage",
        summary="Show how full the HoloRouter root disk is, and the size of its growth folders.",
        description=(
            "Runs 'df -B1 /' and 'du -sb' on /var/backups and /holodeck-runtime "
            "on the HoloRouter. Returns bytes and percent used. Read-only. The "
            "folder list is fixed in code; the op takes no path."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_R["holodeck.disk.usage"],
        group_key="diagnostics",
        tags=("read-only", "diagnostics", "disk", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call when you need to know if the HoloRouter root disk is "
                "filling up, before a full disk stops pods. It is also a good "
                "op for a regular check. Backups in /var/backups and runtime "
                "data in /holodeck-runtime are the two usual causes. " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{root_fs: {total_bytes, used_bytes, avail_bytes, percent_used, "
                "ok}, growth_dirs: [{path, used_bytes, ok}]}. All sizes are bytes. "
                "'ok' is false when that part failed."
            ),
            "next_step": (
                "When /var/backups is large, list the files with "
                "holodeck.backups.list, then remove old ones with "
                "holodeck.backups.prune."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.backups.list",
        handler_attr="backups_list",
        summary="List the backup files in /var/backups on the HoloRouter, newest first.",
        description=(
            "Runs 'find' in /var/backups (or a folder inside it, not recursive) "
            "on the HoloRouter and returns one row per file: path, modified time, "
            "size. Read-only. The order is the same one holodeck.backups.prune "
            "uses."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional folder inside /var/backups, as a full path or "
                        "relative to it. Must stay inside /var/backups. Leave it "
                        "out to list /var/backups itself."
                    ),
                },
            },
            "additionalProperties": False,
            "examples": [{"path": "<backup-folder>"}],
        },
        response_schema=_R["holodeck.backups.list"],
        group_key="backups",
        tags=("read-only", "backups", "list", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call before a holodeck.backups.prune, so the person who "
                "approves it sees which files it would remove. Also call it to "
                "check that new backups still arrive (age and size of the "
                "newest file). " + _SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "path": (
                    "Optional; must stay inside /var/backups. Leave it out for "
                    "the top folder. Same rule as holodeck.backups.prune."
                ),
            },
            "output_shape": (
                "{rows: [{path, mtime, size_bytes}], total, error}, newest first. "
                "'mtime' is seconds since 1970. With keep_newest=N, prune removes "
                "the rows from index N on. 'error' is set (rows empty) when the "
                "command failed; that is not the same as an empty folder."
            ),
            "next_step": (
                "To remove old files, call holodeck.backups.prune with the same "
                "path and a keep_newest value."
            ),
        },
    ),
)

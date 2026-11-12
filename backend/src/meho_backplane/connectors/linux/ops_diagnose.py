# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host-readiness reads for ``linux-ssh``: tools, login identity, HTTP reachability.

Three read verbs for host-readiness checks. Before them the only way was
``linux.script.run``, which always waits for a person, once per probe:

* ``linux.tool.which`` (group ``system``) -- is each named program on the
  host's ``PATH``? Returns ``{tools: [{name, found, path}]}``.
* ``linux.identity.show`` (group ``system``) -- which user, uid, gid and
  groups does the SSH login have, and can it run ``sudo`` without a password?
  Returns ``{user, uid, gid, groups, is_root, sudo_installed,
  sudo_passwordless, sudo_exit_code}``.
* ``linux.http.head`` (group ``network``) -- send one HTTP HEAD request from
  the host with ``curl`` and return the status and the headers. The URL is an
  address the host connects to, so MEHO's address floor applies: it is
  ``caution`` and waits for one approval (the other two are ``safe``). Returns
  ``{url, reachable, status_code, status_line, headers, curl_exit_code,
  error}``.

Every command is fixed. An operator value is checked against a strict
pattern before a command is built, and is then ``shlex.quote``d. No value
is ever run as a shell fragment, and no command runs through ``sudo`` with
operator input: the one ``sudo -n true`` in ``identity.show`` is constant.
Disk usage is ``linux.fs.usage`` (``ops_inspect``).
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import SSH_TRANSPORT_NOTE, LinuxOp

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "DIAGNOSE_OPS",
    "HTTP_HEAD_TIMEOUT_S",
    "MAX_TOOL_NAMES",
    "TOOL_NAME_PATTERN",
    "URL_MAX_LENGTH",
    "URL_PATTERN",
    "LinuxDiagnoseError",
    "build_http_head_command",
    "build_identity_command",
    "build_tool_which_command",
    "linux_http_head",
    "linux_identity_show",
    "linux_tool_which",
    "parse_http_head_output",
    "parse_identity_output",
    "parse_tool_which_output",
    "validate_tool_names",
    "validate_url",
]

#: Program-name charset. Starts with a letter or digit so a name can never be
#: read as an option; no ``/`` (a name, not a path) and no shell metacharacter.
TOOL_NAME_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
_TOOL_NAME_RE: re.Pattern[str] = re.compile(TOOL_NAME_PATTERN)
MAX_TOOL_NAMES: int = 20

#: ``http`` or ``https`` only; a host name or a bracketed IPv6 address; an
#: optional port, path and query. No user name or password (no ``@``), no
#: fragment (``#``), no ``&``, no quote and no space. The query uses the same
#: characters as the path.
_URL_CHARS: str = r"A-Za-z0-9._~%:+,;=/-"
URL_MAX_LENGTH: int = 2048
URL_PATTERN: str = (
    r"^https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])"
    r"(?::[0-9]{1,5})?"
    rf"(?:/[{_URL_CHARS}]*)?"
    rf"(?:\?[{_URL_CHARS}]*)?$"
)
_URL_RE: re.Pattern[str] = re.compile(URL_PATTERN)

#: Wall-clock cap for one ``curl`` round-trip, on the host (``--max-time``)
#: and over SSH (a little longer, so ``curl`` reports its own timeout).
_CURL_MAX_TIME_S: int = 15
_CURL_CONNECT_TIMEOUT_S: int = 10
HTTP_HEAD_TIMEOUT_S: float = 25.0

#: Exit code of the commands when the binary is missing.
_NO_BINARY_EXIT: int = 127

#: ``curl`` exits that mean the request itself was wrong or ``curl`` cannot
#: run it (unsupported, failed init, bad URL, bad option). They are a probe
#: failure, not a reachability verdict. Every other non-zero exit is a
#: network answer (could not resolve, could not connect, timeout, TLS ...).
_CURL_PROBE_FAILURE_EXITS: frozenset[int] = frozenset({1, 2, 3, 4, 127})

_MAX_HEADERS: int = 64
_MAX_HEADER_VALUE_CHARS: int = 512
_MAX_ERROR_CHARS: int = 300

#: Headers that carry a login or session; never returned.
_DROPPED_HEADERS: frozenset[str] = frozenset({"set-cookie", "set-cookie2"})


class LinuxDiagnoseError(RuntimeError):
    """A readiness probe could not run (a missing binary, or an unreadable answer).

    The dispatcher maps it to a ``connector_error`` result, so a probe that
    did not run is never shown as "not found" or "not reachable".
    """


def validate_tool_names(names: Any) -> list[str]:
    """Return *names* when it is a list of valid, distinct program names, else raise."""
    if not isinstance(names, list) or not 1 <= len(names) <= MAX_TOOL_NAMES:
        raise ValueError(f"names must be a list of 1 to {MAX_TOOL_NAMES} program names")
    for name in names:
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError(f"name {name!r} is not a valid program name")
    if len(set(names)) != len(names):
        raise ValueError("names must not repeat")
    return list(names)


def validate_url(url: Any) -> str:
    """Return *url* when it is a plain ``http`` / ``https`` URL, else raise."""
    if not isinstance(url, str) or len(url) > URL_MAX_LENGTH or not _URL_RE.fullmatch(url):
        raise ValueError(f"url {url!r} is not a plain http or https URL")
    return url


def build_tool_which_command(names: list[str]) -> str:
    """Build the fixed ``command -v`` loop for validated *names*."""
    quoted = " ".join(shlex.quote(n) for n in validate_tool_names(names))
    return (
        f"for n in {quoted}; do "
        'if p=$(command -v "$n" 2>/dev/null) && [ -n "$p" ]; then '
        'printf \'FOUND\\t%s\\t%s\\n\' "$n" "$p"; '
        "else printf 'MISSING\\t%s\\n' \"$n\"; fi; done"
    )


def build_identity_command() -> str:
    """Build the fixed identity and ``sudo -n true`` command (no operator input)."""
    return (
        "printf 'user=%s\\n' \"$(id -un 2>/dev/null)\"; "
        "printf 'uid=%s\\n' \"$(id -u 2>/dev/null)\"; "
        "printf 'gid=%s\\n' \"$(id -g 2>/dev/null)\"; "
        "printf 'groups=%s\\n' \"$(id -Gn 2>/dev/null)\"; "
        "if command -v sudo >/dev/null 2>&1; then "
        "sudo -n true </dev/null >/dev/null 2>&1; printf 'sudo_rc=%s\\n' \"$?\"; "
        f"else printf 'sudo_rc={_NO_BINARY_EXIT}\\n'; fi"
    )


def build_http_head_command(url: str) -> str:
    """Build the fixed ``curl -I`` command for a validated *url*.

    ``curl`` sends one HEAD request, follows no redirect, speaks only
    ``http`` and ``https`` and does not expand ``[]`` or ``{}`` in the URL.
    """
    return (
        f"command -v curl >/dev/null 2>&1 || exit {_NO_BINARY_EXIT}; "
        f"curl -sS -I -g --proto '=http,https' --max-time {_CURL_MAX_TIME_S} "
        f"--connect-timeout {_CURL_CONNECT_TIMEOUT_S} {shlex.quote(validate_url(url))}"
    )


def _kv(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            parsed[key.strip()] = value.strip()
    return parsed


def parse_tool_which_output(names: list[str], stdout: str) -> dict[str, Any]:
    """Parse the ``command -v`` loop output into ``{tools: [...]}``.

    Every asked name must be answered. A name with no line means the loop
    did not run to the end, so this raises instead of calling it missing.
    """
    found: dict[str, str | None] = {}
    missing: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "FOUND" and len(parts) >= 3:
            path = parts[2].strip()
            found[parts[1]] = path if path.startswith("/") else None
        elif parts[0] == "MISSING" and len(parts) >= 2:
            missing.add(parts[1])
    unanswered = [n for n in names if n not in found and n not in missing]
    if unanswered:
        raise LinuxDiagnoseError(f"the host gave no answer for: {', '.join(unanswered)}")
    return {
        "tools": [{"name": n, "found": n in found, "path": found.get(n)} for n in names],
    }


def parse_identity_output(stdout: str) -> dict[str, Any]:
    """Parse the identity command output.

    An unreadable user or uid raises: a login whose identity is unknown must
    never be read as "not root" or "no sudo".
    """
    fields = _kv(stdout)
    user = fields.get("user", "")
    uid_text = fields.get("uid", "")
    gid_text = fields.get("gid", "")
    rc_text = fields.get("sudo_rc", "")
    if not user or not uid_text.isdigit() or not gid_text.isdigit() or not rc_text.isdigit():
        raise LinuxDiagnoseError("the host did not return a readable user identity")
    sudo_rc = int(rc_text)
    return {
        "user": user,
        "uid": int(uid_text),
        "gid": int(gid_text),
        "groups": fields.get("groups", "").split(),
        "is_root": int(uid_text) == 0,
        "sudo_installed": sudo_rc != _NO_BINARY_EXIT,
        "sudo_passwordless": sudo_rc == 0,
        "sudo_exit_code": sudo_rc,
    }


def _header_block(stdout: str) -> list[str]:
    """Return the lines of the last response block (``curl -I`` prints one per hop)."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in stdout.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("HTTP/") and not current:
            current = [line]
        elif line == "":
            if current:
                blocks.append(current)
                current = []
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks[-1] if blocks else []


def parse_http_head_output(
    url: str, stdout: str, stderr: str, exit_status: int | None
) -> dict[str, Any]:
    """Parse ``curl -I`` output into the reachability answer.

    Exit ``0`` is a reachable server of any status (also 404 or 503). A
    network exit (no DNS answer, connection refused, timeout, TLS error) is
    ``reachable=false`` with ``curl``'s own exit code and message. A missing
    ``curl`` or a request ``curl`` cannot make raises, so it is never shown
    as "not reachable".
    """
    code = 0 if exit_status is None else exit_status
    if code in _CURL_PROBE_FAILURE_EXITS:
        raise LinuxDiagnoseError(f"curl exited {code} for {url!r}; the probe could not run")
    error = " ".join(stderr.split())[:_MAX_ERROR_CHARS] or None
    if code != 0:
        return {
            "url": url,
            "reachable": False,
            "status_code": None,
            "status_line": None,
            "headers": {},
            "curl_exit_code": code,
            "error": error,
        }
    block = _header_block(stdout)
    parts = block[0].split(None, 2) if block else []
    if len(parts) < 2 or not parts[1].isdigit():
        raise LinuxDiagnoseError(f"curl returned no HTTP status line for {url!r}")
    headers: dict[str, str] = {}
    for line in block[1:]:
        name, sep, value = line.partition(":")
        key = name.strip().lower()
        if not sep or not key or key in _DROPPED_HEADERS:
            continue
        text = value.strip()[:_MAX_HEADER_VALUE_CHARS]
        if key in headers:
            headers[key] = f"{headers[key]}, {text}"[:_MAX_HEADER_VALUE_CHARS]
        elif len(headers) < _MAX_HEADERS:
            headers[key] = text
    return {
        "url": url,
        "reachable": True,
        "status_code": int(parts[1]),
        "status_line": block[0][:_MAX_HEADER_VALUE_CHARS],
        "headers": headers,
        "curl_exit_code": 0,
        "error": error,
    }


def _text(proc: Any, attr: str) -> str:
    raw = getattr(proc, attr, "") or ""
    return raw if isinstance(raw, str) else ""


async def linux_tool_which(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.tool.which`` -- is each program on the host's PATH?"""
    names = validate_tool_names(params["names"])
    proc = await connector._run_command(target, build_tool_which_command(names), operator=operator)
    if getattr(proc, "exit_status", 0) not in (0, None):
        raise LinuxDiagnoseError(f"the tool check exited {proc.exit_status}")
    return parse_tool_which_output(names, _text(proc, "stdout"))


async def linux_identity_show(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.identity.show`` -- the login's identity and sudo capability."""
    del params  # declared empty in schema; intentionally ignored
    proc = await connector._run_command(
        target, build_identity_command(), operator=operator, timeout=15.0
    )
    return parse_identity_output(_text(proc, "stdout"))


async def linux_http_head(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.http.head`` -- one HTTP HEAD request from the host."""
    url = validate_url(params["url"])
    proc = await connector._run_command(
        target, build_http_head_command(url), operator=operator, timeout=HTTP_HEAD_TIMEOUT_S
    )
    return parse_http_head_output(
        url, _text(proc, "stdout"), _text(proc, "stderr"), getattr(proc, "exit_status", 0)
    )


_TOOL_WHICH_OP = LinuxOp(
    op_id="linux.tool.which",
    handler_attr="tool_which",
    summary="Check which of up to 20 programs are installed on the host's PATH.",
    description=(
        "Runs command -v for each named program and returns {tools: [{name, found, path}]}. "
        "found=false means the program is not on the PATH of the SSH login. Read-only. "
        "Each name is checked against a strict pattern before any command is built."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_TOOL_NAMES,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": TOOL_NAME_PATTERN},
                "description": "Program names, e.g. ['curl', 'nfsstat']. Names only, no paths.",
            },
        },
        "required": ["names"],
        "additionalProperties": False,
        "examples": [{"names": ["curl", "jq", "exportfs"]}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "found": {"type": "boolean"},
                        "path": {"type": ["string", "null"]},
                    },
                    "required": ["name", "found", "path"],
                },
            },
        },
        "required": ["tools"],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "diagnostic", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before a plan that needs a program on the host, for example curl, jq or "
            "an NFS tool, to see whether it is installed. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {"names": "A list of program names. No paths, no options."},
        "output_shape": (
            "{tools: [{name, found, path}]}, in the order asked. path is null when the "
            "program is a shell builtin or not found."
        ),
        "next_step": "For the login user and sudo, call linux.identity.show.",
    },
)

_IDENTITY_SHOW_OP = LinuxOp(
    op_id="linux.identity.show",
    handler_attr="identity_show",
    summary="Show the SSH login's user, uid, groups, and whether sudo works without a password.",
    description=(
        "Runs id and sudo -n true on the host. Returns {user, uid, gid, groups, is_root, "
        "sudo_installed, sudo_passwordless, sudo_exit_code}. sudo_passwordless=true means the "
        "login can run sudo without a password. The command is fixed and takes no parameters. "
        "Read-only: sudo only runs the program true."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "user": {"type": "string"},
            "uid": {"type": "integer"},
            "gid": {"type": "integer"},
            "groups": {"type": "array", "items": {"type": "string"}},
            "is_root": {"type": "boolean"},
            "sudo_installed": {"type": "boolean"},
            "sudo_passwordless": {"type": "boolean"},
            "sudo_exit_code": {"type": "integer"},
        },
        "required": [
            "user",
            "uid",
            "gid",
            "groups",
            "is_root",
            "sudo_installed",
            "sudo_passwordless",
            "sudo_exit_code",
        ],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "diagnostic", "identity", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to learn which user MEHO logs in as on the host, and whether that user "
            "can use sudo without a password, before you plan an approval-gated change "
            "that needs root. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "{user, uid, gid, groups, is_root, sudo_installed, sudo_passwordless, "
            "sudo_exit_code}. sudo_passwordless=false with sudo_installed=true means sudo "
            "asks for a password or the login may not use it."
        ),
        "next_step": (
            "If sudo_passwordless is false, an approval-gated change that needs root "
            "will fail; tell the operator before you plan it."
        ),
    },
)

_HTTP_HEAD_OP = LinuxOp(
    op_id="linux.http.head",
    handler_attr="http_head",
    summary="Send one HTTP HEAD request from the host and return the status and headers.",
    description=(
        "Runs curl -I on the host for one http or https URL. Returns reachable, status_code, "
        "status_line and headers. Any HTTP status gives reachable=true; a failure to resolve, "
        "connect or complete TLS gives reachable=false. No redirect is followed. "
        "Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 8,
                "maxLength": URL_MAX_LENGTH,
                "pattern": URL_PATTERN,
                "x-meho-caller-address": True,
                "description": (
                    "http or https URL, e.g. https://mirror.example.com/repo/. No user name or "
                    "password in the URL, no '&' and no '#'."
                ),
            },
        },
        "required": ["url"],
        "additionalProperties": False,
        "examples": [{"url": "https://mirror.example.com/repo/"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "reachable": {"type": "boolean"},
            "status_code": {"type": ["integer", "null"]},
            "status_line": {"type": ["string", "null"]},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "curl_exit_code": {"type": "integer"},
            "error": {"type": ["string", "null"]},
        },
        "required": ["url", "reachable", "status_code", "headers", "curl_exit_code"],
        "additionalProperties": True,
    },
    group_key="network",
    tags=("http", "network", "diagnostic", "linux"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to check that the host can reach an upstream URL, for example a package "
            "mirror or a registry, before a change that downloads from it. " + SSH_TRANSPORT_NOTE
        ),
        "preconditions": (
            "The URL is an address the host connects to, so a person approves the call. "
            "Do not put a user name, password or token in the URL."
        ),
        "parameter_hints": {"url": "One http or https URL. No credentials in the URL."},
        "output_shape": (
            "{url, reachable, status_code, status_line, headers, curl_exit_code, error}. "
            "reachable=false means the host could not connect; see error. reachable=true "
            "means a server answered, so read status_code."
        ),
        "next_step": (
            "If reachable is false, call linux.dns.lookup for the host name to see whether "
            "name lookup is the cause."
        ),
    },
)


#: The host-readiness read tier composed onto ``LINUX_OPS``.
DIAGNOSE_OPS: tuple[LinuxOp, ...] = (_TOOL_WHICH_OP, _IDENTITY_SHOW_OP, _HTTP_HEAD_OP)

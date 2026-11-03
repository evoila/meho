# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The FreeIPA JSON-RPC wire format: paths, command names, request body, answer.

Source: FreeIPA's ``doc/api/jsonrpc_usage.md`` and ``API.txt`` of release
4.13.4 (API version 2.257), and the server code that writes the answer
(``ipaserver/rpcserver.py``, ``ipalib/ipajson.py``).

* A call is ``POST /ipa/session/json`` with the body
  ``{"method": <command>, "params": [[<arguments>], {<options>}], "id": 0}``.
  The options carry ``version``, the API version the caller was written for.
  The server checks only the major part of it (``2``), so a newer minor
  version than the server's is accepted.
* The answer is ``{"result", "error", "id", "principal", "version"}``.
  When the command failed, ``result`` is ``null`` and ``error`` holds
  ``code``, ``name``, ``message`` and ``data``. The HTTP status is still 200.
* Dates come as ``{"__datetime__": "YYYYmmddHHMMSSZ"}`` and DNS names as
  ``{"__dns_name__": "..."}``. :func:`decode_values` turns both into plain
  strings.

Every IPA command the connector sends is a module constant whose name ends in
``_COMMAND``. The spec check reads them from this module and compares them
with ``API.txt``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

__all__ = [
    "API_VERSION",
    "GROUP_FIND_COMMAND",
    "GROUP_SHOW_COMMAND",
    "HBACRULE_FIND_COMMAND",
    "HBACRULE_SHOW_COMMAND",
    "HBACTEST_COMMAND",
    "HOSTGROUP_FIND_COMMAND",
    "HOSTGROUP_SHOW_COMMAND",
    "HOST_FIND_COMMAND",
    "HOST_SHOW_COMMAND",
    "JSON_PATH",
    "LOGIN_PATH",
    "PING_COMMAND",
    "PWPOLICY_SHOW_COMMAND",
    "ROLE_FIND_COMMAND",
    "ROLE_SHOW_COMMAND",
    "SUDORULE_FIND_COMMAND",
    "SUDORULE_SHOW_COMMAND",
    "USER_FIND_COMMAND",
    "USER_SHOW_COMMAND",
    "FreeipaAnswer",
    "FreeipaCommandError",
    "build_request",
    "decode_values",
    "parse_ping_summary",
    "unwrap_answer",
]

#: Password login. Form fields ``user`` and ``password``; the answer sets the
#: session cookies.
LOGIN_PATH: Final[str] = "/ipa/session/login_password"

#: The JSON-RPC path for a session login.
JSON_PATH: Final[str] = "/ipa/session/json"

#: The API version the connector was written for (FreeIPA 4.13.4).
API_VERSION: Final[str] = "2.257"

PING_COMMAND: Final[str] = "ping"
HOST_FIND_COMMAND: Final[str] = "host_find"
HOST_SHOW_COMMAND: Final[str] = "host_show"
HOSTGROUP_FIND_COMMAND: Final[str] = "hostgroup_find"
HOSTGROUP_SHOW_COMMAND: Final[str] = "hostgroup_show"
USER_FIND_COMMAND: Final[str] = "user_find"
USER_SHOW_COMMAND: Final[str] = "user_show"
GROUP_FIND_COMMAND: Final[str] = "group_find"
GROUP_SHOW_COMMAND: Final[str] = "group_show"
ROLE_FIND_COMMAND: Final[str] = "role_find"
ROLE_SHOW_COMMAND: Final[str] = "role_show"
SUDORULE_FIND_COMMAND: Final[str] = "sudorule_find"
SUDORULE_SHOW_COMMAND: Final[str] = "sudorule_show"
HBACRULE_FIND_COMMAND: Final[str] = "hbacrule_find"
HBACRULE_SHOW_COMMAND: Final[str] = "hbacrule_show"
HBACTEST_COMMAND: Final[str] = "hbactest"
PWPOLICY_SHOW_COMMAND: Final[str] = "pwpolicy_show"

#: Longest error text from the server kept in an error message.
_ERROR_TEXT_LIMIT: Final[int] = 500

#: The ``ping`` summary: ``IPA server version <v>. API version <v>``
#: (``ipaserver/plugins/ping.py``).
_PING_SUMMARY: Final[re.Pattern[str]] = re.compile(
    r"IPA server version (?P<server>\S+)\. API version (?P<api>\S+)"
)

#: FreeIPA's date format inside ``__datetime__`` (LDAP generalized time).
_IPA_DATETIME_FORMAT: Final[str] = "%Y%m%d%H%M%SZ"


class FreeipaCommandError(RuntimeError):
    """The server answered a command with an ``error``.

    ``name`` is FreeIPA's error class (for example ``NotFound``), ``code``
    its error number. The message names the command, the error and the
    server's text.
    """

    def __init__(self, command: str, *, name: str, code: int | None, message: str) -> None:
        self.command = command
        self.name = name
        self.code = code
        code_text = f" {code}" if code is not None else ""
        super().__init__(f"freeipa {command} failed: {name}{code_text}: {message}")


@dataclass(frozen=True)
class FreeipaAnswer:
    """A successful answer: the command's ``result``, who ran it, and the server version."""

    result: Any
    principal: str | None
    server_version: str | None


def build_request(
    command: str, args: list[Any] | None = None, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the JSON-RPC body for *command*. ``None`` options are left out."""
    sent = {key: value for key, value in (options or {}).items() if value is not None}
    sent["version"] = API_VERSION
    return {"method": command, "params": [list(args or []), sent], "id": 0}


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


def unwrap_answer(payload: Any, command: str) -> FreeipaAnswer:
    """Return the decoded answer, or raise :class:`FreeipaCommandError` on ``error``."""
    if not isinstance(payload, dict):
        raise FreeipaCommandError(
            command, name="BadAnswer", code=None, message="the answer is not a JSON object"
        )
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            code = error.get("code")
            raise FreeipaCommandError(
                command,
                name=str(error.get("name") or "Error"),
                code=code if isinstance(code, int) else None,
                message=_short(error.get("message") or "no message"),
            )
        raise FreeipaCommandError(command, name="Error", code=None, message=_short(error))
    if "result" not in payload:
        raise FreeipaCommandError(
            command, name="BadAnswer", code=None, message="the answer has no result"
        )
    principal = payload.get("principal")
    version = payload.get("version")
    return FreeipaAnswer(
        result=decode_values(payload["result"]),
        principal=principal if isinstance(principal, str) else None,
        server_version=version if isinstance(version, str) else None,
    )


def _decode_datetime(value: str) -> str:
    try:
        parsed = datetime.strptime(value, _IPA_DATETIME_FORMAT)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def decode_values(value: Any) -> Any:
    """Turn FreeIPA's tagged values into plain strings, everywhere in *value*.

    ``{"__datetime__": "20261029120000Z"}`` becomes ``"2026-10-29T12:00:00Z"``
    and ``{"__dns_name__": "example.test."}`` becomes ``"example.test."``.
    Every other value is returned as it is.
    """
    if isinstance(value, list):
        return [decode_values(item) for item in value]
    if isinstance(value, dict):
        if len(value) == 1:
            if isinstance(value.get("__datetime__"), str):
                return _decode_datetime(value["__datetime__"])
            if isinstance(value.get("__dns_name__"), str):
                return value["__dns_name__"]
        return {key: decode_values(item) for key, item in value.items()}
    return value


def parse_ping_summary(summary: Any) -> tuple[str | None, str | None]:
    """Return ``(server version, API version)`` from a ``ping`` summary."""
    if not isinstance(summary, str):
        return None, None
    match = _PING_SUMMARY.search(summary)
    if match is None:
        return None, None
    return match.group("server"), match.group("api")

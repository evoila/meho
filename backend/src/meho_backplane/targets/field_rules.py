# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Format rules for a target's name, aliases, host and fqdn.

The target write models (:class:`~meho_backplane.targets.schemas.TargetCreate`
and :class:`~meho_backplane.targets.schemas.TargetUpdate`) call these checks,
so the REST API, ``meho targets add``, ``meho targets import`` and the
``targets.register`` operation all refuse the same broken values with a 422.

* **Name and alias** -- must not be blank and must not contain a space, a
  tab, a line break, any other control character, or ``/``. The API routes
  take the name as one path segment, so a name with ``/`` could never be
  read, changed or deleted.
* **Host and fqdn** -- the check looks at the host part MEHO will connect
  to (the same part the SSRF guard screens, see
  :func:`~meho_backplane.targets.ssrf_guard._dialed_host`), so a port after
  the host (``db-01:27017``) and a path after the host (``owner/repo``) keep
  working. That host part must be a valid IPv4 address, a valid IPv6
  address, or a valid DNS host name of at most 253 characters (a trailing
  dot not counted). A host whose parts are all numbers is an IPv4 address
  and must be complete (``10.0.0.`` and ``10.0.0`` are refused).

Rows saved before these rules existed are not touched: the rules run only
when a create or update body is built.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Final

from meho_backplane.targets.ssrf_guard import _dialed_host

__all__ = ["check_target_host", "check_target_name"]

#: One DNS label: letters, digits and ``-``, 1 to 63 characters, not
#: starting or ending with ``-``.
_DNS_LABEL: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)

#: Longest DNS host name in its encoded (ASCII) form, without a trailing
#: dot. RFC 1035 caps the wire form at 255 octets, which leaves 253
#: characters of text.
_DNS_NAME_MAX_LEN: Final[int] = 253


def check_target_name(value: str, field: str) -> str:
    """Return *value* when it is a valid target name or alias, else raise.

    *field* is the field name used in the error message (``name`` or
    ``aliases``).
    """
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
    if any(ch == "/" or ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(
            f'{field} must not contain spaces, tabs, line breaks, control characters or "/"'
        )
    return value


def _is_dns_host_name(host: str) -> bool:
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    ascii_host = ascii_host.removesuffix(".")
    if len(ascii_host) > _DNS_NAME_MAX_LEN:
        return False
    return all(_DNS_LABEL.match(label) for label in ascii_host.split("."))


def check_target_host(value: str, field: str) -> str:
    """Return *value* when its host part is a valid address or host name, else raise.

    *field* is the field name used in the error message (``host`` or
    ``fqdn``). The message never shows a resolved address: this check does
    no DNS lookup at all.
    """
    error = (
        f"{field} must be a valid IPv4 address, IPv6 address or DNS host name "
        "(letters, digits and '-' only, at most 253 characters, "
        "for example node-01.example.com)"
    )
    literal = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        return value
    # Raises its own plain message for a value it cannot parse or that
    # carries credentials, a query or a fragment.
    host = _dialed_host(value)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return value
    if all(label.isascii() and label.isdigit() for label in host.removesuffix(".").split(".")):
        raise ValueError(f"{field} looks like an IPv4 address but is not a complete, valid one")
    if not _is_dns_host_name(host):
        raise ValueError(error)
    return value

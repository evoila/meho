# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense safe reads for local users and static routes (#3954).

Two ``safe`` typed ops that read ``/cf/conf/config.xml`` and return only a
short, fixed list of fields:

* ``pfsense.user.list`` -- one row per local user (``<system><user>``):
  ``name``, ``descr``, ``scope``, ``disabled``, ``expires``, ``uid`` and
  ``groups`` (the names of the ``<system><group>`` elements whose
  ``<member>`` list holds the user's uid).
* ``pfsense.route.static.list`` -- one row per static route
  (``<staticroutes><route>``): ``network``, ``gateway``, ``descr`` and
  ``disabled``.

With these, an operator or agent does not need ``pfsense.config.show`` (the
whole file) just to check a VPN user or a static route.

Allow-list, plus the secret-shape check
---------------------------------------

Each row is built from a fixed set of child elements. Nothing else in the
user or route element is read: no other child, no attribute, no text of an
element nested inside an allowed one. So secret fields (password hashes,
the IPsec pre-shared key, SSH authorized keys, OTP data, certificate
references, and any new secret field a future pfSense version adds) are
never read.

An allowed field can still hold a secret that someone put there, most
likely in the free-text ``descr``. So every returned string goes through
:func:`~meho_backplane.connectors.pfsense.redaction.looks_like_secret`, the
same value-shape check ``pfsense.config.show`` uses. A value with a known
secret shape (a private key, a crypt password hash, a password in a URL, a
long hex run, base64 that hides one of these) is replaced with
:data:`~meho_backplane.connectors.pfsense.redaction.REDACTED`. A secret
typed in plain words can still show: the same limit as
``pfsense.config.show``.

Parsing
-------

The file is read with the same ``cat /cf/conf/config.xml`` the other
config reads use (:func:`~meho_backplane.connectors.pfsense.ops_write._read_config_xml`)
and parsed with ``defusedxml`` (no entity expansion, and no DTD:
``forbid_dtd=True``; pfSense never writes one), like
:func:`~meho_backplane.connectors.pfsense.ops_read.parse_gateways_xml`.
Unlike that parser, a file that is empty or cannot be parsed raises
:class:`PfSenseConfigParseError` instead of returning an empty list, so a
broken read never looks like "no users". The error message is fixed text:
it never quotes the file.

Both handlers return ``{rows, total}``, so the dispatcher's default
JSONFlux reducer turns a long list into a result handle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_read import _xml_text
from meho_backplane.connectors.pfsense.ops_write import _read_config_xml
from meho_backplane.connectors.pfsense.redaction import REDACTED, looks_like_secret

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "CONFIG_READ_OPS",
    "ROUTE_ROW_FIELDS",
    "USER_ROW_FIELDS",
    "PfSenseConfigParseError",
    "list_local_users",
    "list_static_routes",
    "pfsense_route_static_list",
    "pfsense_user_list",
]


class PfSenseConfigParseError(ValueError):
    """``config.xml`` is empty or could not be parsed.

    The message is fixed text plus, at most, the parser's exception type
    name. It never holds any part of the file.
    """


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def _parse_config_root(xml_text: str) -> Any:
    """Parse *xml_text* and return the ``<pfsense>`` root element.

    Raises :class:`PfSenseConfigParseError` when the text is empty, is not
    well-formed XML, holds markup ``defusedxml`` refuses (a DTD or an
    entity; ``forbid_dtd=True``), or has a root element other than
    ``<pfsense>``. The original exception is not chained: its message can
    quote the file.
    """
    if not xml_text.strip():
        raise PfSenseConfigParseError("config.xml is empty; no rows were returned")
    try:
        root = fromstring(xml_text, forbid_dtd=True)
    except (ParseError, ValueError) as exc:  # defusedxml's refusals are ValueErrors
        reason = type(exc).__name__
        raise PfSenseConfigParseError(
            f"config.xml could not be parsed ({reason}); no rows were returned"
        ) from None
    if root.tag != "pfsense":
        raise PfSenseConfigParseError(
            "config.xml has no <pfsense> root element; no rows were returned"
        )
    return root


def _text_or_none(element: Any, tag: str) -> str | None:
    """Return the stripped text of the direct child *tag*, or ``None`` when empty.

    Only the child's own text is read (``.text``): never its attributes and
    never the text of elements nested inside it. A value with a known secret
    shape (:func:`~meho_backplane.connectors.pfsense.redaction.looks_like_secret`)
    comes back as :data:`~meho_backplane.connectors.pfsense.redaction.REDACTED`.
    Every string the two reads return goes through here.
    """
    value = _xml_text(element, tag)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return REDACTED if looks_like_secret(value) else value


def _group_names_by_uid(system: Any) -> dict[str, list[str]]:
    """Map each uid to the sorted names of the groups whose ``<member>`` list holds it.

    pfSense keeps group membership on the group, not on the user: each
    ``<system><group>`` carries one ``<member>`` element per member uid.
    Groups without a name are skipped.
    """
    by_uid: dict[str, set[str]] = {}
    for group in system.findall("group"):
        group_name = _text_or_none(group, "name")
        if group_name is None:
            continue
        for member in group.findall("member"):
            uid = (member.text or "").strip()
            if uid:
                by_uid.setdefault(uid, set()).add(group_name)
    return {uid: sorted(names) for uid, names in by_uid.items()}


def list_local_users(xml_text: str) -> list[dict[str, Any]]:
    """Return one allow-listed row per local user in a pfSense ``config.xml``.

    Each row holds exactly these keys:

    .. code-block:: python

        {
            "name": "vpn-user",
            "descr": "VPN User",
            "scope": "user",
            "disabled": False,
            "expires": "12/31/2026",
            "uid": "2001",
            "groups": ["vpn-users"],
        }

    ``disabled`` is ``True`` when the user element has a ``<disabled>``
    child (pfSense checks only that it is there). ``expires`` is the stored
    date text, or ``None`` when empty. ``groups`` lists every group whose
    ``<member>`` list holds the user's uid, sorted (empty when the user has
    no uid). Other text fields are ``None`` when the element is missing or
    empty. No other field of the user element is ever read. A value with a
    known secret shape comes back as ``REDACTED`` (see :func:`_text_or_none`).

    Raises :class:`PfSenseConfigParseError` when the file is empty or cannot
    be parsed. A config without users returns ``[]``.

    >>> xml = (
    ...     "<pfsense><system>"
    ...     "<group><name>admins</name><member>0</member></group>"
    ...     "<user><name>admin</name><uid>0</uid><bcrypt-hash>x</bcrypt-hash></user>"
    ...     "</system></pfsense>"
    ... )
    >>> list_local_users(xml)[0]["groups"]
    ['admins']
    >>> "bcrypt-hash" in list_local_users(xml)[0]
    False
    """
    root = _parse_config_root(xml_text)
    system = root.find("system")
    if system is None:
        return []
    groups_by_uid = _group_names_by_uid(system)
    rows: list[dict[str, Any]] = []
    for user in system.findall("user"):
        uid = _text_or_none(user, "uid")
        rows.append(
            {
                "name": _text_or_none(user, "name"),
                "descr": _text_or_none(user, "descr"),
                "scope": _text_or_none(user, "scope"),
                "disabled": user.find("disabled") is not None,
                "expires": _text_or_none(user, "expires"),
                "uid": uid,
                "groups": list(groups_by_uid.get(uid, [])) if uid is not None else [],
            }
        )
    return rows


def list_static_routes(xml_text: str) -> list[dict[str, Any]]:
    """Return one allow-listed row per static route in a pfSense ``config.xml``.

    Each row holds exactly these keys:

    .. code-block:: python

        {
            "network": "192.0.2.0/24",
            "gateway": "GW_EXAMPLE",
            "descr": "example route",
            "disabled": False,
        }

    ``network`` is the stored destination. For a CIDR destination,
    ``pfsense.route.static.delete`` finds the route by this value; an alias
    destination is rejected by the delete op. ``gateway`` is the gateway
    name. ``disabled`` is ``True`` when the route element has a
    ``<disabled>`` child. Text fields are ``None`` when the element is
    missing or empty. A value with a known secret shape comes back as
    ``REDACTED`` (see :func:`_text_or_none`).

    Raises :class:`PfSenseConfigParseError` when the file is empty or cannot
    be parsed. A config without static routes returns ``[]``.

    >>> xml = (
    ...     "<pfsense><staticroutes><route>"
    ...     "<network>192.0.2.0/24</network><gateway>GW_EXAMPLE</gateway><disabled/>"
    ...     "</route></staticroutes></pfsense>"
    ... )
    >>> list_static_routes(xml)[0]["disabled"]
    True
    """
    root = _parse_config_root(xml_text)
    routes = root.find("staticroutes")
    if routes is None:
        return []
    return [
        {
            "network": _text_or_none(route, "network"),
            "gateway": _text_or_none(route, "gateway"),
            "descr": _text_or_none(route, "descr"),
            "disabled": route.find("disabled") is not None,
        }
        for route in routes.findall("route")
    ]


# ---------------------------------------------------------------------------
# Handler functions (bound-method shims on PfSenseConnector)
# ---------------------------------------------------------------------------


async def pfsense_user_list(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the local users of a pfSense firewall, allow-listed fields only.

    Op-id: ``pfsense.user.list``. Reads ``/cf/conf/config.xml`` over SSH
    and returns ``{rows, total}`` built by :func:`list_local_users`. A
    failed read raises ``RuntimeError``; a file that cannot be parsed
    raises :class:`PfSenseConfigParseError`. Neither error holds file
    content.
    """
    del params  # declared empty; intentionally ignored
    content = await _read_config_xml(self, target, operator)
    rows = list_local_users(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_route_static_list(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the static routes of a pfSense firewall, allow-listed fields only.

    Op-id: ``pfsense.route.static.list``. Reads ``/cf/conf/config.xml``
    over SSH and returns ``{rows, total}`` built by
    :func:`list_static_routes`. A failed read raises ``RuntimeError``; a
    file that cannot be parsed raises :class:`PfSenseConfigParseError`.
    Neither error holds file content.
    """
    del params  # declared empty; intentionally ignored
    content = await _read_config_xml(self, target, operator)
    rows = list_static_routes(content)
    return {"rows": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for ``pfsense.user.list`` (``users`` group).
_WHEN_TO_USE_USERS = (
    "Use to check the local users of a pfSense firewall, for example a VPN "
    "user: whether the account exists, whether it is disabled, when it "
    "expires, and which groups it is in (``pfsense.user.list``). It never "
    "reads secret fields (password hashes, keys, the IPsec pre-shared key, "
    "SSH authorized keys, certificate data), and a returned value with a "
    f"known secret shape is replaced with ``{REDACTED}``. Prefer it over "
    "``pfsense.config.show`` for any question about users."
)

#: Curated ``when_to_use`` for ``pfsense.route.static.list`` (``routing`` group).
_WHEN_TO_USE_ROUTES = (
    "Use to list the static routes of a pfSense firewall "
    "(``pfsense.route.static.list``): each route's destination network, "
    "gateway name, description and disabled flag. Call it before "
    "``pfsense.route.static.add`` (to see what exists) or "
    "``pfsense.route.static.delete`` (pass a row's ``network`` value; this "
    "works for a CIDR destination, an alias destination is rejected by the "
    "delete op). Prefer it over ``pfsense.config.show`` for any question "
    "about static routes."
)

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _nullable_string() -> dict[str, Any]:
    """Return a fresh ``string | null`` property schema (no shared dict between fields)."""
    return {"type": ["string", "null"]}


#: The exact row keys of ``pfsense.user.list`` (the allow-list).
USER_ROW_FIELDS: tuple[str, ...] = (
    "name",
    "descr",
    "scope",
    "disabled",
    "expires",
    "uid",
    "groups",
)

#: The exact row keys of ``pfsense.route.static.list`` (the allow-list).
ROUTE_ROW_FIELDS: tuple[str, ...] = ("network", "gateway", "descr", "disabled")

_USER_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _nullable_string(),
        "descr": _nullable_string(),
        "scope": _nullable_string(),
        "disabled": {"type": "boolean"},
        "expires": _nullable_string(),
        "uid": _nullable_string(),
        "groups": {"type": "array", "items": {"type": "string"}},
    },
    "required": list(USER_ROW_FIELDS),
    "additionalProperties": False,
}

_ROUTE_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "network": _nullable_string(),
        "gateway": _nullable_string(),
        "descr": _nullable_string(),
        "disabled": {"type": "boolean"},
    },
    "required": list(ROUTE_ROW_FIELDS),
    "additionalProperties": False,
}


def _rows_schema(row_schema: dict[str, Any]) -> dict[str, Any]:
    """Return the ``{rows, total}`` response schema for *row_schema*."""
    return {
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": row_schema},
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": True,
    }


CONFIG_READ_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.user.list",
        handler_attr="user_list",
        summary="List pfSense local users (name, disabled, expires, groups) -- no secrets.",
        description=(
            "Reads ``/cf/conf/config.xml`` over SSH and returns one row per "
            "local user with these fields only: ``name``, ``descr`` (full "
            "name), ``scope``, ``disabled``, ``expires``, ``uid`` and "
            "``groups`` (the groups the user is a member of). Secret fields "
            "(password hashes, keys, the IPsec pre-shared key, SSH authorized "
            "keys, OTP and certificate data) are never read. A returned value "
            f"with a known secret shape is replaced with ``{REDACTED}``. A "
            "secret typed in plain words into a text field such as ``descr`` "
            "can still show: the same limit as ``pfsense.config.show``. Use "
            "it instead of ``pfsense.config.show`` to check a user. Returns a "
            "``{rows, total}`` envelope. No params; safe to call on any "
            "healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_rows_schema(_USER_ROW_SCHEMA),
        group_key="users",
        tags=("read-only", "users", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_USERS,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{name, descr, scope, disabled, expires, uid, groups}], "
                "total: N}``. ``disabled`` is true when the account is turned "
                "off. ``expires`` is the expiry date as pfSense stores it "
                "(``MM/DD/YYYY``) or ``null`` for no expiry. ``scope`` is "
                "``system`` for built-in accounts and ``user`` for the rest. "
                "``uid`` is the numeric user id as a string. ``groups`` is a "
                "sorted list of group names (it can include the built-in "
                "``all`` group). ``descr`` is free text an admin typed. A "
                "value with a known secret shape (private key, password hash, "
                f"password in a URL) is replaced with ``{REDACTED}``; a "
                "secret typed in plain words can still show, the same limit "
                "as ``pfsense.config.show``. A "
                "failed read or a file that cannot be parsed fails the call "
                "with an error that holds no file content. A large list comes "
                "back as a JSONFlux handle; read more rows with ``result_query``."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.route.static.list",
        handler_attr="route_static_list",
        summary="List pfSense static routes (network, gateway, descr, disabled).",
        description=(
            "Reads ``/cf/conf/config.xml`` over SSH and returns one row per "
            "static route with these fields only: ``network`` (the "
            "destination), ``gateway`` (the gateway name), ``descr`` and "
            "``disabled``. For a CIDR destination, pass a row's ``network`` "
            "to ``pfsense.route.static.delete`` to delete that route; an "
            "alias destination is rejected by the delete op. A returned "
            f"value with a known secret shape is replaced with ``{REDACTED}``; "
            "a secret typed in plain words into ``descr`` can still show, "
            "the same limit as ``pfsense.config.show``. Use it "
            "instead of ``pfsense.config.show`` to check routes. Returns a "
            "``{rows, total}`` envelope. No params; safe to call on any "
            "healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_rows_schema(_ROUTE_ROW_SCHEMA),
        group_key="routing",
        tags=("read-only", "routing", "static-route", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_ROUTES,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{network, gateway, descr, disabled}], total: N}``. "
                "``network`` is the destination as stored (usually a CIDR "
                "such as ``192.0.2.0/24``, sometimes an alias name). For a "
                "CIDR destination it is the value "
                "``pfsense.route.static.delete`` takes; the delete op rejects "
                "an alias destination. ``gateway`` is the "
                "gateway name (see ``pfsense.gateway.list``). ``disabled`` is "
                "true when the route is turned off. A value with a known "
                f"secret shape is replaced with ``{REDACTED}``. A failed read or a file "
                "that cannot be parsed fails the call with an error that holds "
                "no file content. A large list comes back as a JSONFlux "
                "handle; read more rows with ``result_query``."
            ),
        },
    ),
)

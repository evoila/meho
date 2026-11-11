# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (handlers + PHP fragments + previews + op
# metadata / JSON schemas / llm_instructions) in the single-module-per-op-group shape of
# its pfSense siblings; the functions are small and low-complexity.

"""pfSense VPN users: create one with a stored password, and remove one (#3993).

Two governed ops (``dangerous`` and every call needs a second person's
approval) for the local-database users that OpenVPN servers in the mode
``server_user`` log in. Such a user has no certificate of its own.

* ``pfsense.openvpn.user.create`` -- makes the user with a random password
  that is made ON the firewall and written to Vault. The caller never sees
  the password: the request names a Vault path and field, the answer says
  only that it was stored.
* ``pfsense.openvpn.user.delete`` -- removes the user and the client
  overrides that carry its name. Refused while the user is connected,
  unless ``force`` is given, and always while pass rules tagged with the
  user's name remain.

The create steps
----------------

1. The Vault target is checked against the caller's tenant path, before
   the firewall is touched.
2. ``config.xml`` is read. A user with that name already there is left
   alone (``exists``; safe to run twice and no password is made).
3. One ``pfSsh.php playback`` fragment does what the pfSense web page does
   for a new user: makes a 24-character password from letters and digits
   with ``random_int``, sets it through ``local_user_set_password()``,
   takes the next ``uid``, adds the user to the ``all`` group,
   ``local_user_set()`` and ``write_config()``. It prints the new ``uid``
   and the password once on standard output. The staged script holds no
   password. When a user with the name appeared since step 2, the fragment
   changes nothing and prints ``MEHO_VPN_EXISTS``: the handler then answers
   ``exists`` and touches that user in no way.
4. The password is written to Vault (``patch`` of the one field; a new
   secret when the path does not exist yet), before any other check. When
   that write fails, the handler removes the user it just made (found by
   name AND the printed ``uid``; overrides with the name stay), so no user
   is left whose password nobody knows, and raises. When the removal fails
   too, the error says so and names the user.
5. ``config.xml`` is read again: the user must be there once, with the
   printed ``uid``, and no other user may have changed. When this read or
   check fails, the password is already in Vault; the error says so.

The password is held only in memory. It is never part of the result, an
error message, the approval input or a log line; the exception chain of a
Vault failure is cut so it cannot carry it.

The delete steps
----------------

The handler reads ``config.xml``. A user that is already gone is a normal
answer (``not_found``); a duplicate name (``ambiguous``) and the built-in
admin account (``protected``) are refused. It refuses ``rules_remain`` while a firewall
rule carries the user's tag ``meho-vpn-user:<name>`` (see
:mod:`~meho_backplane.connectors.pfsense.ops_openvpn_rule`), even with
``force``: such a rule grants access by tunnel address, and the delete
frees that address for the next user. Then it asks every enabled user-login OpenVPN server
for its connected clients through the server's management socket
(``status 2``), then plays back one fragment. The fragment counts the tagged rules again
and changes nothing when there are any (``MEHO_VPN_RULES_REMAIN``). Else
it does ``openvpn_delete_csc()`` and
removal of each override with that common name, ``local_user_del()``,
removal of the user, ``write_config()``. A server whose answer cannot be
read counts as unknown, not as "nobody connected": the call is refused
(``connection_unknown``) unless ``force`` is given. After the write the
handler reads ``config.xml`` again and checks that the user and its
overrides are gone and every other user and override is unchanged.

A refusal changes nothing and fails the call: the handler raises
:class:`~meho_backplane.connectors.pfsense.refusal.PfSenseRefusedError` with
the reason as its code. ``pfsense.openvpn.user.create`` does the same for
``ambiguous``; its ``exists`` stays a normal answer (safe to run twice).

Both handlers hold the appliance's write lock
(:func:`~meho_backplane.connectors.pfsense.ops_write.serialized_config_write`).
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Final, NoReturn

import hvac.exceptions

from meho_backplane.auth.vault import vault_client_for_operator
from meho_backplane.connectors.pfsense.openvpn_config import (
    CscEntry,
    parse_csc_entries,
    parse_openvpn_servers,
)
from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_filter_rule import parse_filter_rules_xml
from meho_backplane.connectors.pfsense.ops_openvpn_rule import rule_tag
from meho_backplane.connectors.pfsense.ops_user_write import (
    USER_NAME_RE,
    _refusal,
    _user_rows,
    _validate_user_name,
)
from meho_backplane.connectors.pfsense.ops_write import (
    _apply_playback,
    _php_squote,
    _read_config_xml,
    _validate_descr,
    serialized_config_write,
)
from meho_backplane.connectors.pfsense.refusal import PfSenseRefusedError
from meho_backplane.connectors.vault.tenant_scope import enforce_tenant_scope
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "OPENVPN_USER_OPS",
    "build_user_create_playback",
    "build_user_delete_playback",
    "build_user_undo_playback",
    "pfsense_openvpn_user_create",
    "pfsense_openvpn_user_delete",
    "register_pfsense_openvpn_user_preview_builders",
]

_PASSWORD_LENGTH: Final = 24
_PASSWORD_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_PASSWORD_MARKER: Final = "MEHO_VPN_PASSWORD="
_PASSWORD_RE: Final = re.compile(
    rf"{_PASSWORD_MARKER}([A-Za-z0-9]{{{_PASSWORD_LENGTH}}})(?![A-Za-z0-9])"
)
_CREATED_MARKER: Final = "MEHO_VPN_CREATED_UID="
_CREATED_RE: Final = re.compile(rf"^{_CREATED_MARKER}([0-9]{{1,10}})\s*$", re.MULTILINE)
_EXISTS_MARKER: Final = "MEHO_VPN_EXISTS"
_EXISTS_RE: Final = re.compile(rf"^{_EXISTS_MARKER}\s*$", re.MULTILINE)
_UID_RE: Final = re.compile(r"^[0-9]{1,10}$")
_RULES_REMAIN_MARKER: Final = "MEHO_VPN_RULES_REMAIN="
_RULES_REMAIN_RE: Final = re.compile(rf"^{_RULES_REMAIN_MARKER}([0-9]+)\s*$", re.MULTILINE)

_MOUNT_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VAULT_PATH_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_VAULT_FIELD_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DEFAULT_MOUNT: Final = "secret"
_DEFAULT_FIELD: Final = "password"
_DELETE_OP_ID: Final = "pfsense.openvpn.user.delete"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _validate_vault_target(params: dict[str, Any]) -> tuple[str, str, str]:
    mount = params.get("vault_mount", _DEFAULT_MOUNT)
    path = params.get("vault_path")
    field = params.get("vault_field", _DEFAULT_FIELD)
    if not isinstance(mount, str) or not _MOUNT_RE.fullmatch(mount):
        raise ValueError(
            "vault_mount must be one mount name (letters, digits, dot, dash, underscore)"
        )
    if (
        not isinstance(path, str)
        or not _VAULT_PATH_RE.fullmatch(path)
        or ".." in path
        or "//" in path
        or path.endswith("/")
        or path.startswith(mount + "/")
    ):
        raise ValueError(
            "vault_path must be a plain path below the mount (no '..', no '//', no trailing "
            "slash, and without the mount name in front)"
        )
    if not isinstance(field, str) or not _VAULT_FIELD_RE.fullmatch(field):
        raise ValueError("vault_field must be letters, digits, dot, dash or underscore")
    return mount, path, field


# ---------------------------------------------------------------------------
# PHP fragments
# ---------------------------------------------------------------------------


def build_user_create_playback(name: str, descr: str) -> str:
    """Return the fragment that makes ONE local user with a random password.

    Adds a user only when no user has that name. The password is made with
    ``random_int`` and printed once as ``MEHO_VPN_PASSWORD=<value>``, after
    ``MEHO_VPN_CREATED_UID=<uid>``. When the name is taken, it prints
    ``MEHO_VPN_EXISTS`` and changes nothing.
    """
    name = _validate_user_name(name)
    users = "$config['system']['user']"
    lines = [
        "global $config, $userindex;",
        "require_once('auth.inc');",
        f"$meho_name = {_php_squote(name)};",
        "$meho_found = 0;",
        f"if (isset({users}) && is_array({users})) {{",
        f"  foreach ({users} as $meho_u) {{",
        "    if (isset($meho_u['name']) && (string)$meho_u['name'] === $meho_name) {",
        "      $meho_found++;",
        "    }",
        "  }",
        "}",
        "if ($meho_found === 0) {",
        f"  $meho_alphabet = {_php_squote(_PASSWORD_ALPHABET)};",
        "  $meho_pw = '';",
        f"  for ($meho_k = 0; $meho_k < {_PASSWORD_LENGTH}; $meho_k++) {{",
        "    $meho_pw .= $meho_alphabet[random_int(0, strlen($meho_alphabet) - 1)];",
        "  }",
        "  $meho_user = array('scope' => 'user', 'name' => $meho_name,",
        f"    'descr' => {_php_squote(descr)});",
        "  local_user_set_password($meho_user, $meho_pw);",
        "  $meho_user['uid'] = $config['system']['nextuid']++;",
        "  if (isset($config['system']['group']) && is_array($config['system']['group'])) {",
        "    foreach ($config['system']['group'] as $meho_g => $meho_group) {",
        "      if ($meho_group['name'] === 'all') {",
        "        if (!isset($config['system']['group'][$meho_g]['member'])",
        "            || !is_array($config['system']['group'][$meho_g]['member'])) {",
        "          $config['system']['group'][$meho_g]['member'] = array();",
        "        }",
        "        $config['system']['group'][$meho_g]['member'][] = $meho_user['uid'];",
        "        break;",
        "      }",
        "    }",
        "  }",
        f"  if (!isset({users}) || !is_array({users})) {{",
        f"    {users} = array();",
        "  }",
        f"  {users}[] = $meho_user;",
        f"  usort({users}, function ($meho_a, $meho_b) {{",
        "    return strcmp($meho_a['name'], $meho_b['name']);",
        "  });",
        "  $userindex = index_users();",
        "  local_user_set($meho_user);",
        f"  write_config({_php_squote(f'meho: add VPN user {name}')});",
        f"  echo '{_CREATED_MARKER}' . $meho_user['uid'] . \"\\n\";",
        f"  echo '{_PASSWORD_MARKER}' . $meho_pw . \"\\n\";",
        "} else {",
        f"  echo '{_EXISTS_MARKER}' . \"\\n\";",
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_user_delete_playback(name: str) -> str:
    """Return the fragment that removes ONE local user and the overrides with its name.

    Acts only when exactly one user has the name, it is not uid 0, and no
    firewall rule carries the user's tag. When tagged rules remain, it
    prints ``MEHO_VPN_RULES_REMAIN=<count>`` and changes nothing.
    """
    return _build_user_remove_playback(name, uid=None)


def build_user_undo_playback(name: str, uid: str) -> str:
    """Return the fragment that removes the user this call just made, and nothing else.

    Acts only when exactly one user has the name AND it has *uid* (not 0).
    The client overrides with the name stay: they may be older than the user.
    """
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        raise ValueError(f"uid must be a number; got {uid!r}")
    return _build_user_remove_playback(name, uid=uid)


def _build_user_remove_playback(name: str, *, uid: str | None) -> str:
    name = _validate_user_name(name)
    users = "$config['system']['user']"
    csc = "$config['openvpn']['openvpn-csc']"
    rules = "$config['filter']['rule']"
    guard = f"$meho_found === 1 && (string){users}[$meho_idx]['uid'] !== '0'"
    if uid is not None:
        guard += f" && (string){users}[$meho_idx]['uid'] === {_php_squote(uid)}"
    else:
        guard += " && $meho_tagged === 0"
    tagged = [
        "$meho_tagged = 0;",
        f"if (isset({rules}) && is_array({rules})) {{",
        f"  foreach ({rules} as $meho_r) {{",
        "    if (isset($meho_r['descr'])",
        f"        && trim((string)$meho_r['descr']) === {_php_squote(rule_tag(name))}) {{",
        "      $meho_tagged++;",
        "    }",
        "  }",
        "}",
    ]
    overrides = [
        f"  if (isset({csc}) && is_array({csc})) {{",
        f"    foreach ({csc} as $meho_j => $meho_e) {{",
        "      if (isset($meho_e['common_name'])",
        "          && (string)$meho_e['common_name'] === $meho_name) {",
        "        openvpn_delete_csc($meho_e);",
        f"        unset({csc}[$meho_j]);",
        "      }",
        "    }",
        f"    {csc} = array_values({csc});",
        "  }",
    ]
    what = "delete VPN user" if uid is None else "undo new VPN user"
    lines = [
        "global $config, $userindex;",
        "require_once('auth.inc');",
        *(["require_once('openvpn.inc');"] if uid is None else []),
        f"$meho_name = {_php_squote(name)};",
        "$meho_found = 0;",
        "$meho_idx = null;",
        f"if (isset({users}) && is_array({users})) {{",
        f"  foreach ({users} as $meho_i => $meho_u) {{",
        "    if (isset($meho_u['name']) && (string)$meho_u['name'] === $meho_name) {",
        "      $meho_found++;",
        "      $meho_idx = $meho_i;",
        "    }",
        "  }",
        "}",
        *(tagged if uid is None else []),
        f"if ({guard}) {{",
        f"  $meho_user = {users}[$meho_idx];",
        *(overrides if uid is None else []),
        "  local_user_del($meho_user);",
        f"  unset({users}[$meho_idx]);",
        f"  {users} = array_values({users});",
        "  $userindex = index_users();",
        f"  write_config({_php_squote(f'meho: {what} {name}')});",
        "}",
    ]
    if uid is None:
        lines += [
            "if ($meho_tagged > 0) {",
            f"  echo '{_RULES_REMAIN_MARKER}' . $meho_tagged . \"\\n\";",
            "}",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


async def _store_password(
    operator: Operator, mount: str, path: str, field: str, password: str
) -> None:
    """Write *password* to ``<mount>/<path>`` field *field*; raise without the password.

    Patches the one field. A path that holds no secret yet gets a new
    secret (``cas=0``: a concurrent creator makes this fail, not overwrite).
    The exception chain is cut: no message from the client can reach the
    caller, and none holds the password.
    """
    try:
        async with vault_client_for_operator(operator) as client:
            kv = client.secrets.kv.v2
            try:
                await asyncio.to_thread(
                    kv.patch, path=path, secret={field: password}, mount_point=mount
                )
            except hvac.exceptions.InvalidPath:
                await asyncio.to_thread(
                    kv.create_or_update_secret,
                    path=path,
                    secret={field: password},
                    cas=0,
                    mount_point=mount,
                )
    except Exception as exc:
        raise RuntimeError(f"writing the password to Vault failed ({type(exc).__name__})") from None


# ---------------------------------------------------------------------------
# pfsense.openvpn.user.create
# ---------------------------------------------------------------------------


def _other_users(rows: list[dict[str, Any]], name: str) -> Counter[tuple[str, str, bool]]:
    return Counter((r["name"], r["uid"], r["disabled"]) for r in rows if r["name"] != name)


@serialized_config_write
async def pfsense_openvpn_user_create(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.user.create`` (VPN user, password to Vault)."""
    name = _validate_user_name(params.get("name"))
    descr = _validate_descr(params.get("description"), f"meho: VPN user {name}")
    mount, path, field = _validate_vault_target(params)
    if operator is None:
        raise ValueError("pfsense.openvpn.user.create needs an operator to write to Vault")
    enforce_tenant_scope(operator, mount=mount, path=path, read_only=False)

    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "openvpn_user",
        "status": "created",
        "name": name,
        "nothing_to_do": False,
        "applied": False,
        "verified": False,
        "vault": {"mount": mount, "path": path, "field": field, "stored": False},
        "guidance": None,
    }
    before = _user_rows(await _read_config_xml(self, target, operator))
    same = [r for r in before if r["name"] == name]
    if len(same) > 1:
        raise PfSenseRefusedError(
            "pfsense.openvpn.user.create",
            "ambiguous",
            f"{len(same)} local users share the name {name!r}; nothing changed",
        )
    if same:
        return {
            **result,
            "status": "exists",
            "nothing_to_do": True,
            "verified": True,
            "guidance": "the user already exists; it and its password were left alone",
        }

    played = await _apply_playback(
        self,
        target,
        script_name=f"meho_openvpn_user_create_{re.sub(r'[^A-Za-z0-9]', '_', name)}",
        script_body=build_user_create_playback(name, descr),
        operator=operator,
    )
    created = _CREATED_RE.search(played)
    if created is None:
        if _EXISTS_RE.search(played):
            return {
                **result,
                "status": "exists",
                "nothing_to_do": True,
                "guidance": (
                    "a user with this name was made by someone else while this call ran; "
                    "it and its password were left alone"
                ),
            }
        raise RuntimeError(
            f"openvpn.user.create: the firewall did not say whether it made {name!r}; "
            "nothing was undone. Check with pfsense.user.list."
        )
    uid = created.group(1)
    match = _PASSWORD_RE.search(played)
    if match is None:
        await _undo_create(
            self, target, name, uid, operator, "the firewall did not return a password"
        )
    try:
        await _store_password(operator, mount, path, field, match.group(1))
    except RuntimeError as exc:
        await _undo_create(self, target, name, uid, operator, str(exc))
    stored = {**result["vault"], "stored": True}
    await _verify_create(self, target, operator, name, uid, before, f"{mount}/{path}")
    return {**result, "applied": True, "verified": True, "vault": stored}


async def _verify_create(
    self: PfSenseConnector,
    target: Any,
    operator: Operator | None,
    name: str,
    uid: str,
    before: list[dict[str, Any]],
    where: str,
) -> None:
    """Read ``config.xml`` again; raise when it does not show just the new user.

    The password is already in Vault here, so every error says where it is.
    """
    stored = f"The user was made and its password is stored in Vault at {where}"
    try:
        after = _user_rows(await _read_config_xml(self, target, operator))
    except Exception as exc:
        raise RuntimeError(
            f"openvpn.user.create: {stored}, but config.xml could not be read again to "
            f"check it ({type(exc).__name__}). Check {name!r} with pfsense.user.list."
        ) from None
    made = [r for r in after if r["name"] == name]
    if len(made) != 1 or made[0]["uid"] != uid:
        raise RuntimeError(
            f"openvpn.user.create verification failed for {name!r}: config.xml does not hold "
            f"exactly the one new user with uid {uid} after write_config. {stored}."
        )
    if _other_users(after, name) != _other_users(before, name):
        raise RuntimeError(
            f"openvpn.user.create verification failed for {name!r}: other users changed "
            f"while the op ran. {stored}. Check the pfSense config history."
        )


async def _undo_create(
    self: PfSenseConnector,
    target: Any,
    name: str,
    uid: str,
    operator: Operator | None,
    why: str,
) -> NoReturn:
    """Remove the user this call made (by name and uid), then raise. Never returns."""
    try:
        await _apply_playback(
            self,
            target,
            script_name=f"meho_openvpn_user_undo_{re.sub(r'[^A-Za-z0-9]', '_', name)}",
            script_body=build_user_undo_playback(name, uid),
            operator=operator,
        )
    except Exception:
        raise RuntimeError(
            f"openvpn.user.create: {why}; the new user {name!r} could not be removed again. "
            "Its password is lost: delete the user or set a new password by hand."
        ) from None
    raise RuntimeError(f"openvpn.user.create: {why}; the new user {name!r} was removed again.")


# ---------------------------------------------------------------------------
# pfsense.openvpn.user.delete
# ---------------------------------------------------------------------------

_CLIENT_HEADER: Final = "HEADER,CLIENT_LIST,"
_CLIENT_ROW: Final = "CLIENT_LIST,"


def _sessions_of(out: str, name: str) -> int | None:
    """Count the rows of ``status 2`` output where *name* is the login name or the common name.

    The ``HEADER,CLIENT_LIST`` line says which column is ``Username``. On a
    server that checks a certificate too, the certificate's common name can
    differ from the login name, so both columns count. Returns ``None`` when
    a client row is there but the header names no ``Username`` column.
    """
    lines = [line.strip() for line in out.splitlines()]
    username_col: int | None = None
    for line in lines:
        if line.startswith(_CLIENT_HEADER):
            columns = line.split(",")[1:]
            if "Username" in columns:
                username_col = columns.index("Username")
    count = 0
    for line in lines:
        if not line.startswith(_CLIENT_ROW):
            continue
        if username_col is None:
            return None
        row = line.split(",")
        username = row[username_col] if len(row) > username_col else None
        if name in (row[1], username):
            count += 1
    return count


async def _connected_sessions(
    self: PfSenseConnector, target: Any, name: str, operator: Operator | None, xml_text: str
) -> int | None:
    """Count the live sessions of *name* on every enabled user-login server.

    Returns ``None`` when any server's answer cannot be read: unknown is not "no sessions".
    """
    total = 0
    for server in parse_openvpn_servers(xml_text):
        if not server.user_auth or server.disabled:
            continue
        if not re.fullmatch(r"[0-9]{1,5}", server.vpnid):
            return None
        proc = await self._run_command(
            target,
            f"printf 'status 2\\nquit\\n' | nc -U -w 3 /var/etc/openvpn/server{server.vpnid}/sock",
            operator=operator,
        )
        out = proc.stdout if isinstance(proc.stdout, str) else ""
        if proc.exit_status != 0 or not re.search(r"^END\s*$", out, re.MULTILINE):
            return None
        sessions = _sessions_of(out, name)
        if sessions is None:
            return None
        total += sessions
    return total


def _tagged_rules(xml_text: str, name: str) -> list[str]:
    """Return the ``ridentifier`` of every firewall rule tagged as *name*'s VPN rule.

    Raises when the rule list cannot be read: unknown is not "no rules".
    """
    try:
        stored = parse_filter_rules_xml(xml_text)
    except ValueError as exc:
        raise RuntimeError(
            f"openvpn.user.delete: the firewall rules could not be read: {exc}"
        ) from exc
    tag = rule_tag(name)
    return [str(r.get("tracker")) for r in stored if (r.get("descr") or "").strip() == tag]


def _rules_remain(name: str, count: int, trackers: list[str]) -> PfSenseRefusedError:
    ids = f" (ridentifier {', '.join(trackers)})" if trackers else ""
    return PfSenseRefusedError(
        _DELETE_OP_ID,
        "rules_remain",
        f"{count} firewall rule(s) tagged {rule_tag(name)!r}{ids} still grant access by the "
        "user's tunnel address; nothing changed. Remove them with pfsense.filter.rule.delete "
        "first. force does not skip this check.",
    )


def _csc_rows(entries: list[CscEntry], name: str) -> list[dict[str, Any]]:
    return [
        {
            "server_list": ",".join(e.server_list),
            "tunnel_network": e.tunnel_raw or None,
            "routes": list(e.routes),
        }
        for e in entries
        if e.common_name == name
    ]


@serialized_config_write
async def pfsense_openvpn_user_delete(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.user.delete`` -- remove a VPN user and its overrides."""
    name = _validate_user_name(params.get("name"))
    force = bool(params.get("force", False))
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "openvpn_user",
        "status": "deleted",
        "name": name,
        "force": force,
        "connected_sessions": None,
        "overrides_removed": 0,
        "applied": False,
        "verified": False,
        "guidance": None,
    }
    before_xml = await _read_config_xml(self, target, operator)
    rows = _user_rows(before_xml)
    status, guidance = _refusal(rows, name)
    if status == "not_found":
        return {**result, "status": status, "guidance": guidance}
    if status is not None:
        raise PfSenseRefusedError(_DELETE_OP_ID, status, guidance or "")
    trackers = _tagged_rules(before_xml, name)
    if trackers:
        raise _rules_remain(name, len(trackers), trackers)
    entries = parse_csc_entries(before_xml)
    mine = [e for e in entries if e.common_name == name]
    result["overrides_removed"] = len(mine)
    if not force:
        sessions = await _connected_sessions(self, target, name, operator, before_xml)
        result["connected_sessions"] = sessions
        if sessions is None:
            raise PfSenseRefusedError(
                _DELETE_OP_ID,
                "connection_unknown",
                "could not read the connected clients of every OpenVPN server; nothing "
                "changed. Use force to delete anyway.",
            )
        if sessions > 0:
            raise PfSenseRefusedError(
                _DELETE_OP_ID,
                "connected",
                f"{name!r} is connected ({sessions} session); nothing changed. "
                "Use force to delete anyway.",
            )

    played = await _apply_playback(
        self,
        target,
        script_name=f"meho_openvpn_user_delete_{re.sub(r'[^A-Za-z0-9]', '_', name)}",
        script_body=build_user_delete_playback(name),
        operator=operator,
    )
    remain = _RULES_REMAIN_RE.search(played)
    if remain is not None:
        raise _rules_remain(name, int(remain.group(1)), [])
    after_xml = await _read_config_xml(self, target, operator)
    after_rows = _user_rows(after_xml)
    others_ok = _other_users(after_rows, name) == _other_users(rows, name)
    after_entries = parse_csc_entries(after_xml)
    keep = Counter((e.common_name, tuple(e.server_list)) for e in entries if e.common_name != name)
    kept = Counter(
        (e.common_name, tuple(e.server_list)) for e in after_entries if e.common_name != name
    )
    if (
        any(r["name"] == name for r in after_rows)
        or any(e.common_name == name for e in after_entries)
        or not others_ok
        or keep != kept
    ):
        raise RuntimeError(
            f"openvpn.user.delete verification failed for {name!r}: config.xml does not show "
            "exactly the removal of this user and its overrides after write_config; check "
            "config.xml"
        )
    return {**result, "applied": True, "verified": True}


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------


async def _create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    try:
        name = _validate_user_name(ctx.params.get("name"))
        mount, path, field = _validate_vault_target(ctx.params)
    except ValueError:
        return None
    preview: dict[str, Any] = {
        "user": name,
        "vault": {"mount": mount, "path": path, "field": field},
        "exists": None,
        "note": "The password is made on the firewall and written to Vault. It is never shown.",
    }
    if ctx.connector_instance is None:
        return preview
    rows = _user_rows(
        await _read_config_xml(
            ctx.connector_instance,  # type: ignore[arg-type]  # PfSenseConnector at runtime
            ctx.target,
            ctx.operator,
        )
    )
    return {**preview, "exists": any(r["name"] == name for r in rows)}


async def _delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    try:
        name = _validate_user_name(ctx.params.get("name"))
    except ValueError:
        return None
    preview: dict[str, Any] = {
        "user": name,
        "force": bool(ctx.params.get("force", False)),
        "refusal": None,
        "overrides_removed": None,
        "note": (
            "Deletes the login and the client overrides with this name. Refused while the "
            "user is connected unless force is set (checked when the op runs), and while "
            "firewall rules tagged with the user remain."
        ),
    }
    if ctx.connector_instance is None:
        return preview
    xml_text = await _read_config_xml(
        ctx.connector_instance,  # type: ignore[arg-type]  # PfSenseConnector at runtime
        ctx.target,
        ctx.operator,
    )
    status, _ = _refusal(_user_rows(xml_text), name)
    if status is not None:
        return {**preview, "refusal": status}
    if _tagged_rules(xml_text, name):
        return {**preview, "refusal": "rules_remain"}
    return {**preview, "overrides_removed": _csc_rows(parse_csc_entries(xml_text), name)}


def register_pfsense_openvpn_user_preview_builders() -> None:
    """Wire the approval-card previews for both ops. Called at import time."""
    register_preview_builder("pfsense.openvpn.user.create", _create_preview)
    register_preview_builder("pfsense.openvpn.user.delete", _delete_preview)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "pattern": USER_NAME_RE.pattern,
    "description": "Exact VPN user name. Letters, digits, dot, dash, underscore; at most 32.",
}

_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROPERTY,
        "description": {
            "type": "string",
            "description": "Optional full name or note for the user (one line, at most 200).",
        },
        "vault_path": {
            "type": "string",
            "description": (
                "Where to store the password: a path below the mount, inside your tenant "
                "path. The password is never returned."
            ),
        },
        "vault_mount": {"type": "string", "description": "KV v2 mount. Default 'secret'."},
        "vault_field": {"type": "string", "description": "Field name. Default 'password'."},
    },
    "required": ["name", "vault_path"],
    "additionalProperties": False,
}

_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROPERTY,
        "force": {
            "type": "boolean",
            "description": "Delete even when the user is connected or the check cannot run.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["openvpn_user"]},
        "status": {"type": "string", "enum": ["created", "exists"]},
        "name": {"type": "string"},
        "nothing_to_do": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "vault": {
            "type": "object",
            "properties": {
                "mount": {"type": "string"},
                "path": {"type": "string"},
                "field": {"type": "string"},
                "stored": {"type": "boolean"},
            },
            "required": ["mount", "path", "field", "stored"],
            "additionalProperties": False,
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "name",
        "nothing_to_do",
        "applied",
        "verified",
        "vault",
        "guidance",
    ],
    "additionalProperties": False,
}

_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["openvpn_user"]},
        "status": {
            "type": "string",
            "enum": ["deleted", "not_found"],
        },
        "name": {"type": "string"},
        "force": {"type": "boolean"},
        "connected_sessions": {"type": ["integer", "null"]},
        "overrides_removed": {"type": "integer"},
        "applied": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "name",
        "force",
        "connected_sessions",
        "overrides_removed",
        "applied",
        "verified",
        "guidance",
    ],
    "additionalProperties": False,
}


def _guidance(texts: dict[str, Any]) -> dict[str, Any]:
    """Return *texts* plus ``when_to_use``, the older name of ``when_to_call``."""
    return {**texts, "when_to_use": texts["when_to_call"]}


OPENVPN_USER_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.openvpn.user.create",
        handler_attr="openvpn_user_create",
        summary="Create a VPN user; its password goes to Vault (needs approval).",
        description=(
            "Creates one local user for OpenVPN servers in the mode server_user. The "
            "password is made on the firewall and written to the Vault path in the "
            "request. It is never returned. An existing user is left alone. If the Vault "
            "write fails, the new user is removed again."
        ),
        parameter_schema={
            **_CREATE_PARAMETER_SCHEMA,
            "examples": [{"name": "<vpn-user>", "vault_path": "<vault-path>"}],
        },
        response_schema=_CREATE_RESPONSE_SCHEMA,
        group_key="vpn",
        tags=("write", "vpn", "users", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_guidance(
            {
                "when_to_call": (
                    "Use to give a person VPN access: create the user here, then the fixed "
                    "address with ``pfsense.openvpn.csc.sync``. Safe to run twice: an "
                    "existing user and its password are left alone."
                ),
                "preconditions": (
                    "The name is free (check with ``pfsense.user.list``). The Vault path is "
                    "inside your tenant path and you may write to it."
                ),
                "parameter_hints": {
                    "name": "Required. The VPN user name.",
                    "vault_path": "Required. Path for the password, inside your tenant path.",
                    "vault_mount": "Optional. Default 'secret'.",
                    "vault_field": "Optional. Default 'password'.",
                },
                "output_shape": (
                    "``{status, name, nothing_to_do, applied, verified, vault: {mount, path, "
                    "field, stored}, guidance}``. The password is never returned; read it "
                    "from Vault."
                ),
                "next_step": (
                    "Find an address with ``pfsense.openvpn.address.next_free`` and set it "
                    "with ``pfsense.openvpn.csc.sync``."
                ),
            }
        ),
    ),
    PfSenseOp(
        op_id="pfsense.openvpn.user.delete",
        handler_attr="openvpn_user_delete",
        summary="Delete a VPN user and its overrides (needs approval; refused while connected).",
        description=(
            "Removes one local user and the OpenVPN client overrides with its name. "
            "A user already gone is not_found. Fails, changing nothing, for a duplicate "
            "name, the admin account, a connected user or an unknown state (unless "
            "force), and while tagged firewall rules remain."
        ),
        parameter_schema={
            **_DELETE_PARAMETER_SCHEMA,
            "examples": [{"name": "<vpn-user>"}],
        },
        response_schema=_DELETE_RESPONSE_SCHEMA,
        group_key="vpn",
        tags=("write", "vpn", "users", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_guidance(
            {
                "when_to_call": (
                    "Use to remove a VPN login that is no longer needed. To only stop a "
                    "login and keep the account, use ``pfsense.user.disable``."
                ),
                "preconditions": (
                    "The user is known (``pfsense.user.list``). The user's tagged pass rules "
                    "are removed first (``pfsense.filter.rule.delete``). Use ``force`` only "
                    "when the user may be cut off."
                ),
                "parameter_hints": {
                    "name": "Required. Exact user name.",
                    "force": "Optional. Delete even when connected or when that is unknown.",
                },
                "output_shape": (
                    "``{status, name, force, connected_sessions, overrides_removed, "
                    "applied, verified, guidance}``. ``status`` is ``deleted`` after a "
                    "verified removal, or ``not_found`` when the user is already gone. A "
                    "refusal (``ambiguous``, ``protected``, ``connected``, "
                    "``connection_unknown``, ``rules_remain``) fails the call and changes "
                    "nothing; the error names the reason and, for ``rules_remain``, the "
                    "``ridentifier`` of each tagged rule."
                ),
                "next_step": "Check the result with ``pfsense.user.list``.",
            }
        ),
    ),
)

register_pfsense_openvpn_user_preview_builders()

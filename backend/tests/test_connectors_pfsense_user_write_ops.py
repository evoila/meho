# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``pfsense.user.disable`` / ``pfsense.user.enable`` (#3937).

Three layers:

* **Unit** (mocked ``_run_command``): name allowlist before any SSH call, the
  PHP fragment (a hostile name can never change its meaning; only the one
  user's ``disabled`` element is touched), the handler paths (change,
  ``nothing_to_do``, ``not_found`` / ``ambiguous`` / ``protected``, failed
  read-back), and that no secret field reaches the result.
* **PHP run** (needs the ``php`` command; CI installs it, elsewhere the tests
  skip): the fragment runs for real against a synthetic ``$config`` array,
  with stand-ins for the pfSense functions. For both ops, every user record
  is compared before and after: only the one user's ``disabled`` element
  changes; every password hash, key, group and certificate stays the same.
  The fragment's own guards (uid 0, unknown name, duplicate name) write
  nothing.
* **Metadata**: ``dangerous`` + requires approval, ``users`` group, the
  response schema, and the broadcast class.
* **Governed flow** (full ``call_operation`` dispatch): the call waits for
  approval with a preview that shows the user, the current state and the new
  state; a second person approves; the write runs and is verified.

All config.xml fixtures are synthetic: no lab names, hosts or addresses.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.ops_user_write import (
    USER_WRITE_OPS,
    build_user_flag_playback,
)
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003937")
_DISABLE_OP = "pfsense.user.disable"
_ENABLE_OP = "pfsense.user.enable"

# Marker values in the secret fields of the fixture. None may ever reach a result.
_SECRET_HASH = "hash-value-that-must-not-leak"
_SECRET_KEY = "key-value-that-must-not-leak"
_SECRET_PSK = "psk-value-that-must-not-leak"


def _user(name: str, uid: str, *, scope: str = "user", disabled: bool = False) -> str:
    return (
        f"<user><scope>{scope}</scope><name>{name}</name><descr>{name} account</descr>"
        f"<uid>{uid}</uid><bcrypt-hash>{_SECRET_HASH}</bcrypt-hash>"
        f"<authorizedkeys>{_SECRET_KEY}</authorizedkeys><ipsecpsk>{_SECRET_PSK}</ipsecpsk>"
        f"{'<disabled></disabled>' if disabled else ''}</user>"
    )


def _config(*users: str) -> str:
    return (
        "<pfsense><system>"
        "<group><name>all</name><member>0</member><member>2000</member></group>"
        f"{''.join(users)}"
        "</system></pfsense>"
    )


_ADMIN = _user("admin", "0", scope="system")
_VPN_ON = _user("vpn.guest-1", "2000")
_VPN_OFF = _user("vpn.guest-1", "2000", disabled=True)
_OTHER_OFF = _user("old_user", "2001", disabled=True)
_OTHER_ON = _user("old_user", "2001")

_CONFIG_VPN_ON = _config(_ADMIN, _VPN_ON, _OTHER_OFF)
_CONFIG_VPN_OFF = _config(_ADMIN, _VPN_OFF, _OTHER_OFF)


# ---------------------------------------------------------------------------
# Environment fixtures + stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _cmds(mock_cmd: AsyncMock) -> list[str]:
    return [call.args[1] for call in mock_cmd.await_args_list]


def _played(mock_cmd: AsyncMock) -> bool:
    return any(c.startswith("pfSsh.php playback") for c in _cmds(mock_cmd))


# ===========================================================================
# Part A -- input allowlist and the PHP fragment
# ===========================================================================

_HOSTILE_NAMES = [
    "x'; system('id'); //",
    "x\\'); write_config(); //",
    "vpn\nwrite_config();",
    "!id",
    "=id",
    "-rf",
    "a b",
    "$config",
    "",
    "a" * 33,
    "vpn.guest-1\n",
]


@pytest.mark.parametrize("name", [*_HOSTILE_NAMES, None, 7, ["vpn"]])
@pytest.mark.parametrize("op", ["user_disable", "user_enable"])
async def test_bad_name_rejected_before_any_ssh_call(name: Any, op: str) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError, match="name must match"),
    ):
        await getattr(connector, op)(None, {"name": name})
    mock_cmd.assert_not_awaited()


@pytest.mark.parametrize("name", _HOSTILE_NAMES)
def test_hostile_name_never_reaches_the_fragment(name: str) -> None:
    with pytest.raises(ValueError, match="name must match"):
        build_user_flag_playback(name, disable=True)


@pytest.mark.parametrize("name", ["vpn.guest-1", "a", "_svc", "A.b_c-9", "x" * 32])
@pytest.mark.parametrize("disable", [True, False])
def test_fragment_shape_is_the_same_for_every_allowed_name(name: str, disable: bool) -> None:
    """An allowed name only fills the two string literals; the code around them never changes."""
    reference = build_user_flag_playback("REFNAME", disable=disable)
    assert build_user_flag_playback(name, disable=disable) == reference.replace("REFNAME", name)


@pytest.mark.parametrize("disable", [True, False])
def test_fragment_touches_only_the_one_users_disabled_element(disable: bool) -> None:
    body = build_user_flag_playback("vpn.guest-1", disable=disable)
    lines = body.splitlines()
    # pfSsh.php playback runs a line that starts with "!" or "=" as a shell command.
    assert all(not line.lstrip().startswith(("!", "=")) for line in lines)
    # The only write into $config is the one user's ``disabled`` element.
    config_writes = [
        line.strip()
        for line in lines
        if re.search(r"\$config\[[^;]*\]\s*=(?!=)", line) or line.strip().startswith("unset(")
    ]
    expected = (
        "$config['system']['user'][$meho_idx]['disabled'] = true;"
        if disable
        else "unset($config['system']['user'][$meho_idx]['disabled']);"
    )
    assert config_writes == [expected]
    # Changed only when exactly one user matched and it is not uid 0.
    assert "$meho_matched === 1 && (string)$config['system']['user'][$meho_idx]['uid'] !== '0'" in (
        body
    )
    assert "local_user_set($config['system']['user'][$meho_idx]);" in body
    assert body.count("write_config(") == 1
    assert "filter_configure" not in body
    assert "$meho_name = 'vpn.guest-1';" in body


# ===========================================================================
# Part A2 -- the PHP fragment, run for real
# ===========================================================================
#
# ``pfSsh.php playback`` evals the fragment inside a function; that is why
# pfSense's own playback scripts, and ours, start with ``global $config;``.
# The harness below does the same with the ``php`` command-line interpreter.
# It loads a synthetic ``$config`` array in the shape pfSense's parser gives
# (``user``, ``group``, ``cert`` and ``member`` are always lists), replaces
# the three pfSense functions the fragment calls with stand-ins that record
# their calls, runs the fragment, and prints the config back as JSON.

_PHP = shutil.which("php")
_needs_php = pytest.mark.skipif(
    _PHP is None and os.environ.get("MEHO_REQUIRE_PHP") != "1",
    reason="php not installed; set MEHO_REQUIRE_PHP=1 to fail instead of skip",
)

_PHP_AUTH_INC_STUB = """<?php
// Stand-ins for the pfSense functions the fragment calls. Each records its call.
function index_users() {
    global $config;
    $index = array();
    foreach ($config['system']['user'] as $i => $user) {
        $index[$user['name']] = $i;
    }
    return $index;
}
function local_user_set(&$user, $force_password = false, $userattributes = array()) {
    global $meho_calls;
    $meho_calls[] = array('local_user_set', $user['name'], isset($user['disabled']));
}
function write_config($desc = 'Unknown', $backup = true, $write_config_only = false) {
    global $meho_calls;
    $meho_calls[] = array('write_config', $desc);
    return true;
}
"""

_PHP_HARNESS = """<?php
$config = json_decode(stream_get_contents(STDIN), true);
$meho_calls = array();
$userindex = null;
function meho_playback($file) {
    eval(file_get_contents($file));
}
meho_playback($argv[1]);
echo json_encode(array('config' => $config, 'calls' => $meho_calls, 'userindex' => $userindex));
"""


def _php_user(
    name: str, uid: str, *, scope: str = "user", disabled: bool = False
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scope": scope,
        "name": name,
        "descr": f"{name} account",
        "uid": uid,
        "bcrypt-hash": f"{_SECRET_HASH}-{uid}",
        "authorizedkeys": f"{_SECRET_KEY}-{uid}",
        "ipsecpsk": f"{_SECRET_PSK}-{uid}",
        "cert": [f"cert-{uid}"],
        "priv": ["user-shell-access"],
    }
    if disabled:
        row["disabled"] = ""  # <disabled></disabled> parses to an empty string
    return row


def _php_config(*users: dict[str, Any]) -> dict[str, Any]:
    """Synthetic parsed config: users, two groups with members, and one certificate per user."""
    uids = [u["uid"] for u in users]
    return {
        "system": {
            "group": [
                {"name": "all", "scope": "system", "gid": "1998", "member": uids},
                {
                    "name": "vpn-users",
                    "scope": "local",
                    "gid": "2000",
                    "member": uids[1:],
                    "priv": ["user-shell-access"],
                },
            ],
            "user": list(users),
        },
        "cert": [
            {
                "refid": f"cert-{uid}",
                "descr": f"certificate {uid}",
                "crt": f"crt-{uid}",
                "prv": f"prv-{uid}",
            }
            for uid in uids
        ],
    }


_PHP_USERS = (
    _php_user("admin", "0", scope="system"),
    _php_user("vpn.guest-1", "2000"),
    _php_user("old_user", "2001", disabled=True),
)


def _run_fragment_in_php(tmp_path: Path, config: dict[str, Any], fragment: str) -> dict[str, Any]:
    """Run *fragment* against *config* in php and return ``{config, calls, userindex}``."""
    (tmp_path / "auth.inc").write_text(_PHP_AUTH_INC_STUB)
    (tmp_path / "run.php").write_text(_PHP_HARNESS)
    (tmp_path / "fragment").write_text(fragment)
    proc = subprocess.run(
        [
            _PHP or "php",
            "-n",
            "-d",
            f"include_path={tmp_path}",
            "-d",
            "display_errors=stderr",
            "-d",
            "error_reporting=E_ALL",
            str(tmp_path / "run.php"),
            str(tmp_path / "fragment"),
        ],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr  # no PHP notice or warning either
    return json.loads(proc.stdout)


def _without_disabled(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "disabled"}


@_needs_php
@pytest.mark.parametrize(("name", "disable"), [("vpn.guest-1", True), ("old_user", False)])
def test_fragment_run_in_php_changes_only_that_users_disabled_element(
    tmp_path: Path, name: str, disable: bool
) -> None:
    """Both ops, run for real: one ``disabled`` element changes; every other value stays."""
    before = _php_config(*_PHP_USERS)
    out = _run_fragment_in_php(tmp_path, before, build_user_flag_playback(name, disable=disable))

    # The whole config, before and after, differs in exactly one element.
    expected = copy.deepcopy(before)
    target = next(u for u in expected["system"]["user"] if u["name"] == name)
    if disable:
        target["disabled"] = True
    else:
        del target["disabled"]
    assert out["config"] == expected

    # The same check spelled out per record: hashes, keys, groups and certificates.
    users_before = {u["name"]: u for u in before["system"]["user"]}
    users_after = {u["name"]: u for u in out["config"]["system"]["user"]}
    assert list(users_after) == list(users_before)
    assert ("disabled" in users_after[name]) is disable
    assert _without_disabled(users_after[name]) == _without_disabled(users_before[name])
    for other in set(users_before) - {name}:
        assert users_after[other] == users_before[other]
    assert out["config"]["system"]["group"] == before["system"]["group"]
    assert out["config"]["cert"] == before["cert"]

    # Same calls as the pfSense web page, in order, and local_user_set saw the new flag.
    action = "disable" if disable else "enable"
    assert out["calls"] == [
        ["local_user_set", name, disable],
        ["write_config", f"meho: {action} user {name}"],
    ]
    assert out["userindex"] == {"admin": 0, "vpn.guest-1": 1, "old_user": 2}


@_needs_php
@pytest.mark.parametrize(
    ("users", "name"),
    [
        (_PHP_USERS, "admin"),  # uid 0: refused by the fragment itself, not only by the handler
        (_PHP_USERS, "nobody"),
        ((_PHP_USERS[0], _PHP_USERS[1], _PHP_USERS[1]), "vpn.guest-1"),  # two users, one name
    ],
)
@pytest.mark.parametrize("disable", [True, False])
def test_fragment_run_in_php_writes_nothing_when_its_own_guard_fails(
    tmp_path: Path, users: tuple[dict[str, Any], ...], name: str, disable: bool
) -> None:
    before = _php_config(*users)
    out = _run_fragment_in_php(tmp_path, before, build_user_flag_playback(name, disable=disable))
    assert out["config"] == before
    assert out["calls"] == []


# ===========================================================================
# Part B -- handlers (mocked SSH)
# ===========================================================================


async def test_disable_changes_one_user_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),  # read before
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_VPN_OFF),  # read-back
        ]
        result = await connector.user_disable(None, {"name": "vpn.guest-1"})
    assert result["status"] == "disabled"
    assert result["previous_state"] == "enabled"
    assert result["new_state"] == "disabled"
    assert result["applied"] is True
    assert result["verified"] is True
    assert result["nothing_to_do"] is False
    cmds = _cmds(mock_cmd)
    assert cmds[0] == "cat /cf/conf/config.xml"
    assert cmds[1].startswith("cat > /etc/phpshellsessions/meho_user_disable_vpn_guest_1 <<'")
    assert "$config['system']['user'][$meho_idx]['disabled'] = true;" in cmds[1]
    assert cmds[2] == "pfSsh.php playback meho_user_disable_vpn_guest_1"
    assert cmds[3] == "rm -f /etc/phpshellsessions/meho_user_disable_vpn_guest_1"
    assert cmds[4] == "cat /cf/conf/config.xml"


async def test_enable_changes_one_user_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_OFF),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_VPN_ON),
        ]
        result = await connector.user_enable(None, {"name": "vpn.guest-1"})
    assert result["status"] == "enabled"
    assert result["previous_state"] == "disabled"
    assert result["applied"] is True
    assert "unset($config['system']['user'][$meho_idx]['disabled']);" in _cmds(mock_cmd)[1]


@pytest.mark.parametrize(
    ("op", "config"),
    [("user_disable", _CONFIG_VPN_OFF), ("user_enable", _CONFIG_VPN_ON)],
)
async def test_second_run_is_nothing_to_do_and_writes_nothing(op: str, config: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(config)
        result = await getattr(connector, op)(None, {"name": "vpn.guest-1"})
    assert result["status"] == "nothing_to_do"
    assert result["nothing_to_do"] is True
    assert result["applied"] is False
    assert _cmds(mock_cmd) == ["cat /cf/conf/config.xml"]


@pytest.mark.parametrize(
    ("config", "name", "status"),
    [
        (_CONFIG_VPN_ON, "nobody", "not_found"),
        (_config(_ADMIN, _VPN_ON, _VPN_ON), "vpn.guest-1", "ambiguous"),
        (_CONFIG_VPN_ON, "admin", "protected"),
        (_config(_user("renamed_admin", "0")), "renamed_admin", "protected"),
        (_config(_user("builtin", "5", scope="system")), "builtin", "protected"),
    ],
)
@pytest.mark.parametrize("op", ["user_disable", "user_enable"])
async def test_refusals_change_nothing(config: str, name: str, status: str, op: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(config)
        result = await getattr(connector, op)(None, {"name": name})
    assert result["status"] == status
    assert result["applied"] is False
    assert result["guidance"]
    assert not _played(mock_cmd)


async def test_read_back_without_the_new_flag_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_VPN_ON),  # flag did not persist
        ]
        with pytest.raises(RuntimeError, match="did not persist"):
            await connector.user_disable(None, {"name": "vpn.guest-1"})


async def test_read_back_with_another_user_changed_raises() -> None:
    """Success needs the one flag changed AND every other user's flag as it was."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_config(_ADMIN, _VPN_OFF, _OTHER_ON)),  # old_user turned on too
        ]
        with pytest.raises(RuntimeError, match="another user's"):
            await connector.user_disable(None, {"name": "vpn.guest-1"})


async def test_playback_failure_raises_and_cleans_up() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),
            _proc("", 0),
            _proc("", 1),  # playback fails
            _proc("", 0),  # rm still runs
        ]
        with pytest.raises(RuntimeError, match="failed"):
            await connector.user_disable(None, {"name": "vpn.guest-1"})
    assert _cmds(mock_cmd)[-1].startswith("rm -f /etc/phpshellsessions/")


async def test_results_never_carry_secret_fields() -> None:
    connector = PfSenseConnector()
    results: list[dict[str, Any]] = []
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_VPN_OFF),
            _proc(_CONFIG_VPN_OFF),
            _proc(_CONFIG_VPN_OFF),
        ]
        results.append(await connector.user_disable(None, {"name": "vpn.guest-1"}))
        results.append(await connector.user_disable(None, {"name": "vpn.guest-1"}))
        results.append(await connector.user_enable(None, {"name": "admin"}))
    text = json.dumps(results)
    for secret in (_SECRET_HASH, _SECRET_KEY, _SECRET_PSK, "bcrypt", "authorizedkeys", "ipsecpsk"):
        assert secret not in text


# ===========================================================================
# Part C -- metadata
# ===========================================================================


def test_ops_are_governed_and_in_the_users_group() -> None:
    by_id = {op.op_id: op for op in PFSENSE_OPS}
    for op in USER_WRITE_OPS:
        assert by_id[op.op_id] is op
        assert op.safety_level == "dangerous"
        assert op.requires_approval is True
        assert op.group_key == "users"
        assert "write" in op.tags
        assert op.parameter_schema["additionalProperties"] is False
        assert hasattr(PfSenseConnector, op.handler_attr)


def test_ops_classify_as_write_on_the_broadcast_feed() -> None:
    assert classify_op(_DISABLE_OP) == "write"
    assert classify_op(_ENABLE_OP) == "write"


def test_parameter_schema_pattern_matches_the_handler_allowlist() -> None:
    pattern = re.compile(USER_WRITE_OPS[0].parameter_schema["properties"]["name"]["pattern"])
    for name in _HOSTILE_NAMES:
        assert not pattern.fullmatch(name)
    assert pattern.fullmatch("vpn.guest-1")


async def test_response_schema_accepts_every_outcome() -> None:
    validator = Draft202012Validator(USER_WRITE_OPS[0].response_schema)
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_VPN_ON),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_VPN_OFF),
            _proc(_CONFIG_VPN_OFF),
            _proc(_CONFIG_VPN_OFF),
        ]
        outcomes = [
            await connector.user_disable(None, {"name": "vpn.guest-1"}),
            await connector.user_disable(None, {"name": "vpn.guest-1"}),
            await connector.user_disable(None, {"name": "nobody"}),
        ]
    for outcome in outcomes:
        validator.validate(outcome)


# ===========================================================================
# Part D -- governed flow (full dispatch)
# ===========================================================================


def _make_operator(
    *, sub: str = "op-pf", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="pfSense User Write Conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _RecordingPfSense(PfSenseConnector):
    """Replays ``config_before`` until a playback runs, then ``config_after``."""

    def __init__(self, *, config_before: str, config_after: str | None = None) -> None:
        super().__init__()
        self._config_before = config_before
        self._config_after = config_after if config_after is not None else config_before
        self._played = False
        self.commands: list[str] = []

    async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {"username": "admin", "client_keys": [], "known_hosts": None}

    async def _run_command(self, target: Any, command: str, operator: Any = None) -> Any:
        self.commands.append(command)
        if command == "cat /cf/conf/config.xml":
            return _proc(self._config_after if self._played else self._config_before)
        if command.startswith("pfSsh.php playback"):
            self._played = True
        return _proc("", 0)

    @property
    def playback_ran(self) -> bool:
        return any(c.startswith("pfSsh.php playback") for c in self.commands)


async def _seed_target(name: str = "pf-users") -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product="pfsense",
                host="pf.test",
                port=22,
                fqdn=None,
                secret_ref="kv/dev/pfsense/users",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2.7"},
                preferred_impl_id="pfsense-ssh",
                notes="seeded by test_connectors_pfsense_user_write_ops",
            )
        )
        await s.commit()


async def _bootstrap(recorder: _RecordingPfSense) -> None:
    if ("pfsense", "2.7", "pfsense-ssh") not in all_connectors_v2():
        register_connector_v2(
            product="pfsense", version="2.7", impl_id="pfsense-ssh", cls=PfSenseConnector
        )
    await PfSenseConnector.register_operations()
    _CONNECTOR_INSTANCE_CACHE[PfSenseConnector] = recorder  # type: ignore[assignment]


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


@pytest.mark.asyncio
async def test_ops_registered_dangerous_requires_approval() -> None:
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_VPN_ON))
    async with get_sessionmaker()() as s:
        for op_id in (_DISABLE_OP, _ENABLE_OP):
            row = (
                await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
            ).scalar_one()
            assert row.safety_level == "dangerous", op_id
            assert row.requires_approval is True, op_id
            assert row.source_kind == "typed", op_id


@pytest.mark.asyncio
async def test_full_governed_flow_disable() -> None:
    """Call waits for approval (preview: user, current, new); a second person approves; it runs."""
    recorder = _RecordingPfSense(config_before=_CONFIG_VPN_ON, config_after=_CONFIG_VPN_OFF)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    call = await call_operation(
        requester,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _DISABLE_OP,
            "target": "pf-users",
            "params": {"name": "vpn.guest-1"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert not recorder.playback_ran

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    preview = effect["preview"]
    assert preview["user"] == "vpn.guest-1"
    assert preview["current_state"] == "enabled"
    assert preview["new_state"] == "disabled"
    assert preview["nothing_to_do"] is False
    assert preview["refusal"] is None
    assert _SECRET_HASH not in json.dumps(effect)

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "disabled"
    assert resume.result["verified"] is True
    assert recorder.playback_ran


@pytest.mark.asyncio
async def test_preview_names_the_refusal_for_the_admin_account() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_VPN_ON)
    await _bootstrap(recorder)
    await _seed_target()

    call = await call_operation(
        _make_operator(sub="op-requester"),
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _DISABLE_OP,
            "target": "pf-users",
            "params": {"name": "admin"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(call["extras"]["approval_request_id"]))
        assert row is not None
        preview = dict(row.proposed_effect)["preview"]
    assert preview["refusal"] == "protected"
    assert not recorder.playback_ran


@pytest.mark.asyncio
async def test_agent_principal_without_grant_is_denied() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_VPN_ON)
    await _bootstrap(recorder)
    await _seed_target()

    call = await call_operation(
        _make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _DISABLE_OP,
            "target": "pf-users",
            "params": {"name": "vpn.guest-1"},
        },
    )
    assert call["status"] == "denied", call
    assert not recorder.playback_ran
    assert await _pending_count() == 0

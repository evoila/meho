# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``pfsense.openvpn.rule.sync``, the tagged per-user VPN pass rules (#3993).

* The plan: create, unchanged, not_wanted, refused; an untagged rule never counts.
* Compare-only writes nothing; one refusal stops the call.
* The write: rollback copy first, one playback, read-back of every new rule
  and of the rest of ``config.xml``; a rule change after the plan stops it.
* Hostile and bad input is refused before any SSH call.
* The PHP fragment run for real against stubs (needs ``php``).

All fixtures are synthetic: made-up names and documentation addresses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
import meho_backplane.connectors.pfsense.ops_openvpn_rule as rule_module
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.ops_openvpn_rule import (
    OPENVPN_RULE_OPS,
    build_rule_sync_playback,
    rule_tag,
)
from meho_backplane.connectors.pfsense.refusal import PfSenseRefusedError
from meho_backplane.op_classes import is_root_shell

_OP = OPENVPN_RULE_OPS[0]
_RID_A = "123456789"
_RID_B = "123456790"


def _rule_xml(
    tracker: str,
    source: str,
    destination: str,
    *,
    descr: str = "",
    protocol: str = "",
    port: str = "",
    interface: str = "openvpn",
) -> str:
    return (
        f"<rule><tracker>{tracker}</tracker><type>pass</type>"
        f"<interface>{interface}</interface><ipprotocol>inet</ipprotocol>"
        f"{f'<protocol>{protocol}</protocol>' if protocol else ''}"
        f"<source><address>{source}</address></source>"
        f"<destination><address>{destination}</address>"
        f"{f'<port>{port}</port>' if port else ''}</destination>"
        f"<descr>{descr}</descr></rule>"
    )


def _mine(tracker: str, source: str, destination: str, name: str = "guest-1", **kw: Any) -> str:
    return _rule_xml(tracker, source, destination, descr=rule_tag(name), **kw)


_LAN_RULE = _rule_xml("1700000001", "192.0.2.0/24", "any", interface="lan", descr="lan out")


def _config(
    rules: str = "", users: str = "<user><name>guest-1</name><uid>2000</uid></user>"
) -> str:
    return (
        "<pfsense><system><user><name>admin</name><uid>0</uid></user>"
        f"{users}</system><filter>{_LAN_RULE}{rules}</filter></pfsense>"
    )


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


class _Fake(PfSenseConnector):
    """A scripted pfSense: config reads replay a list, the playback prints *playback_out*."""

    def __init__(self, configs: list[str], *, playback_out: str = "") -> None:
        super().__init__()
        self._configs = list(configs)
        self._playback_out = playback_out
        self.commands: list[str] = []
        self.staged: list[str] = []
        self.played: list[str] = []

    async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {"username": "admin", "client_keys": [], "known_hosts": None}

    async def _run_command(self, target: Any, command: str, operator: Any = None) -> Any:
        self.commands.append(command)
        if command == "cat /cf/conf/config.xml":
            return _proc(self._configs.pop(0) if len(self._configs) > 1 else self._configs[0])
        if command.startswith("cat > /etc/phpshellsessions/"):
            self.staged.append(command)
        if command.startswith("pfSsh.php playback"):
            self.played.append(command)
            return _proc(self._playback_out)
        return _proc()


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    values = iter([int(_RID_A) - 100000000, int(_RID_B) - 100000000])
    monkeypatch.setattr(rule_module.secrets, "randbelow", lambda n: next(values))
    yield


def _want(name: str = "guest-1", source: str = "198.51.100.2", **kw: Any) -> dict[str, Any]:
    return {"name": name, "source": source, "destination": "192.0.2.0/24", **kw}


async def _sync(fake: _Fake, rules: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    out = await fake.openvpn_rule_sync(None, {"rules": rules, **kw}, None)  # type: ignore[arg-type]
    Draft202012Validator(_OP.response_schema or {}).validate(out)
    return out


# ===========================================================================
# The plan
# ===========================================================================


async def test_compare_only_shows_create_unchanged_and_not_wanted() -> None:
    rules = _mine("100000011", "198.51.100.2", "192.0.2.0/24") + _mine(
        "100000012", "198.51.100.2", "10.9.0.0/16"
    )
    fake = _Fake([_config(rules)])
    out = await _sync(
        fake,
        [_want(), _want(destination="192.0.2.80", protocol="tcp", destination_port=443)],
        compare_only=True,
    )
    assert out["status"] == "compared"
    assert out["counts"] == {"create": 1, "unchanged": 1, "not_wanted": 1, "refused": 0}
    by_action = {r["action"]: r for r in out["rules"]}
    assert by_action["unchanged"]["ridentifier"] == "100000011"
    assert by_action["not_wanted"]["ridentifier"] == "100000012"
    assert by_action["create"]["destination"] == "192.0.2.80"
    assert by_action["create"]["destination_port"] == "443"
    assert not fake.played


async def test_an_untagged_rule_that_looks_the_same_does_not_count() -> None:
    fake = _Fake([_config(_rule_xml("100000011", "198.51.100.2", "192.0.2.0/24", descr="hand"))])
    out = await _sync(fake, [_want()], compare_only=True)
    assert out["counts"]["create"] == 1 and out["counts"]["not_wanted"] == 0


async def test_a_tagged_rule_of_another_user_is_not_reported() -> None:
    fake = _Fake([_config(_mine("100000011", "198.51.100.9", "192.0.2.0/24", name="other"))])
    out = await _sync(fake, [_want()], compare_only=True)
    assert out["counts"] == {"create": 1, "unchanged": 0, "not_wanted": 0, "refused": 0}


async def test_a_tagged_rule_with_another_port_is_not_the_same_rule() -> None:
    fake = _Fake(
        [_config(_mine("100000011", "198.51.100.2", "192.0.2.0/24", protocol="tcp", port="22"))]
    )
    out = await _sync(fake, [_want(protocol="tcp", destination_port="443")], compare_only=True)
    assert out["counts"]["create"] == 1 and out["counts"]["not_wanted"] == 1


async def test_an_unknown_user_is_refused_and_nothing_is_written() -> None:
    fake = _Fake([_config()])
    with pytest.raises(PfSenseRefusedError) as info:
        await _sync(fake, [_want(), _want(name="nobody")])
    assert info.value.code == "refused"
    assert "nobody (" in info.value.guidance and "user_not_found" in info.value.guidance
    assert "guest-1 (" not in info.value.guidance
    assert not fake.played


async def test_compare_only_with_an_unknown_user_is_a_normal_answer() -> None:
    fake = _Fake([_config()])
    out = await _sync(fake, [_want(), _want(name="nobody")], compare_only=True)
    assert out["status"] == "refused"
    assert [r["reason"] for r in out["rules"]] == [None, "user_not_found"]
    assert not fake.played


async def test_nothing_to_do_when_every_rule_is_there() -> None:
    fake = _Fake([_config(_mine("100000011", "198.51.100.2", "192.0.2.0/24"))])
    out = await _sync(fake, [_want()])
    assert out["status"] == "nothing_to_do" and out["verified"] is True
    assert not fake.played


@pytest.mark.parametrize(
    "rules",
    [
        [],
        "x",
        [_want(name="x'; //")],
        [_want(source="any")],
        [_want(source="2001:db8::1")],
        [_want(destination="lan_hosts")],
        [_want(destination="192.0.2.0/24 ")],
        [_want(destination_port=443)],  # a port needs tcp or udp
        [_want(protocol="gre")],
        [_want(protocol="tcp", destination_port="70000")],
        [{**_want(), "interface": "wan"}],
        [_want(), _want()],
    ],
)
async def test_bad_input_is_refused_before_any_ssh_call(rules: Any) -> None:
    fake = _Fake([_config()])
    with pytest.raises(ValueError):
        await fake.openvpn_rule_sync(None, {"rules": rules}, None)  # type: ignore[arg-type]
    assert fake.commands == []


# ===========================================================================
# The write
# ===========================================================================


def _after(*new: str) -> str:
    return _config(_mine("100000011", "198.51.100.2", "10.9.0.0/16") + "".join(new))


async def test_sync_saves_a_rollback_copy_writes_and_verifies() -> None:
    before = _config(_mine("100000011", "198.51.100.2", "10.9.0.0/16"))
    after = _after(
        _mine(_RID_A, "198.51.100.2", "192.0.2.0/24"),
        _mine(_RID_B, "198.51.100.2", "192.0.2.80", protocol="tcp", port="443"),
    )
    fake = _Fake([before, after])
    out = await _sync(
        fake, [_want(), _want(destination="192.0.2.80", protocol="tcp", destination_port="443")]
    )
    assert out["status"] == "synced" and out["applied"] and out["verified"]
    assert re.fullmatch(r"/cf/conf/meho-rollback-rule-sync-\d{8}T\d{6}Z\.xml", out["rollback_copy"])
    created = [r["ridentifier"] for r in out["rules"] if r["action"] == "create"]
    assert created == [_RID_A, _RID_B]
    # The rule nobody asked for is reported, never removed.
    assert [r["ridentifier"] for r in out["rules"] if r["action"] == "not_wanted"] == ["100000011"]
    copy_at = next(i for i, c in enumerate(fake.commands) if "cp /cf/conf/config.xml" in c)
    play_at = next(i for i, c in enumerate(fake.commands) if c.startswith("pfSsh.php"))
    assert copy_at < play_at and "umask 077" in fake.commands[copy_at]
    staged = fake.staged[0]
    assert "unset(" not in staged and staged.count("write_config(") == 1


async def test_sync_writes_nothing_when_the_rules_changed_after_the_plan() -> None:
    fake = _Fake([_config()], playback_out=f"MEHO_RULES_CHANGED={rule_tag('guest-1')}\n")
    with pytest.raises(RuntimeError, match="changed after the plan was made"):
        await _sync(fake, [_want()])
    assert fake.commands.count("cat /cf/conf/config.xml") == 1


async def test_sync_fails_when_the_read_back_does_not_hold_the_rule() -> None:
    fake = _Fake([_config()])
    with pytest.raises(RuntimeError, match="verification failed"):
        await _sync(fake, [_want()])


async def test_sync_fails_when_the_new_rule_is_not_as_asked() -> None:
    after = _config(_mine(_RID_A, "198.51.100.3", "192.0.2.0/24"))
    fake = _Fake([_config(), after])
    with pytest.raises(RuntimeError, match="verification failed"):
        await _sync(fake, [_want()])


async def test_sync_fails_when_something_else_changed_during_the_write() -> None:
    after = _config(
        _mine(_RID_A, "198.51.100.2", "192.0.2.0/24"),
        users="<user><name>guest-1</name><uid>2000</uid></user><user><name>x</name></user>",
    )
    fake = _Fake([_config(), after])
    with pytest.raises(RuntimeError, match=r"other parts of config\.xml"):
        await _sync(fake, [_want()])


async def test_sync_fails_when_the_rollback_copy_cannot_be_saved() -> None:
    class _NoCopy(_Fake):
        async def _run_command(self, target: Any, command: str, operator: Any = None) -> Any:
            if "cp /cf/conf/config.xml" in command:
                return _proc("", 1)
            return await super()._run_command(target, command, operator)

    fake = _NoCopy([_config()])
    with pytest.raises(RuntimeError, match="rollback copy"):
        await _sync(fake, [_want()])
    assert not fake.played


async def test_sync_fails_when_the_config_has_no_filter_section() -> None:
    fake = _Fake(["<pfsense><system></system></pfsense>"])
    with pytest.raises(RuntimeError, match="could not be read"):
        await _sync(fake, [_want()], compare_only=True)


# ===========================================================================
# The PHP fragment, run for real
# ===========================================================================

_PHP = shutil.which("php")
_needs_php = pytest.mark.skipif(
    _PHP is None and os.environ.get("MEHO_REQUIRE_PHP") != "1",
    reason="php not installed; set MEHO_REQUIRE_PHP=1 to fail instead of skip",
)

_RUN_PHP = """<?php
function config_get_path($path, $default = null) {
    global $config;
    $cur = $config;
    foreach (explode('/', $path) as $key) {
        if (!is_array($cur) || !array_key_exists($key, $cur)) { return $default; }
        $cur = $cur[$key];
    }
    return $cur;
}
function config_set_path($path, $value) {
    global $config;
    $ref = &$config;
    foreach (explode('/', $path) as $key) {
        if (!isset($ref[$key]) || !is_array($ref[$key])) { $ref[$key] = array(); }
        $ref = &$ref[$key];
    }
    $ref = $value;
}
function get_interface_ruleindex($if) {
    $first = null;
    $last = null;
    foreach (config_get_path('filter/rule', []) as $i => $rule) {
        if (!isset($rule['floating']) && $rule['interface'] === $if) {
            if ($first === null) { $first = $i; }
            $last = $i;
        }
    }
    return array('first' => $first, 'last' => $last);
}
function filter_rules_sort() { global $meho_calls; $meho_calls[] = array('sort'); }
function filter_configure() { global $meho_calls; $meho_calls[] = array('filter_configure'); }
function write_config($desc) { global $meho_calls; $meho_calls[] = array('write_config', $desc); }
$config = json_decode(stream_get_contents(STDIN), true);
$meho_calls = array();
function meho_playback($file) { eval(file_get_contents($file)); }
meho_playback($argv[1]);
echo "\\n@@CONFIG@@" . json_encode(array('config' => $config, 'calls' => $meho_calls));
"""


def _run_php(tmp_path: Path, config: dict[str, Any], fragment: str) -> tuple[str, dict[str, Any]]:
    (tmp_path / "run.php").write_text(_RUN_PHP)
    (tmp_path / "fragment").write_text(fragment)
    proc = subprocess.run(
        [
            _PHP or "php",
            "-n",
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
    assert proc.stderr == "", proc.stderr
    printed, _, tail = proc.stdout.partition("\n@@CONFIG@@")
    return printed, json.loads(tail)


def _php_rule(tracker: str, interface: str, descr: str) -> dict[str, Any]:
    return {
        "tracker": tracker,
        "type": "pass",
        "interface": interface,
        "source": {"any": ""},
        "destination": {"any": ""},
        "descr": descr,
    }


def _plan_row(name: str, rid: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "action": "create",
        "ridentifier": rid,
        "source": "198.51.100.2",
        "destination": "192.0.2.0/24",
        "protocol": "any",
        "destination_port": None,
        **kw,
    }


_PHP_BEFORE = {
    "filter": {
        "rule": [
            _php_rule("1", "lan", "lan out"),
            _php_rule("2", "openvpn", "block guests"),
            _php_rule("3", "wan", "wan in"),
        ]
    },
    "cert": [{"refid": "c1", "prv": "private"}],
}


@_needs_php
def test_fragment_adds_the_rules_below_the_openvpn_rules(tmp_path: Path) -> None:
    plan = [
        _plan_row("guest-1", _RID_A),
        _plan_row(
            "guest-1", _RID_B, destination="192.0.2.80", protocol="tcp", destination_port="443"
        ),
    ]
    fragment = build_rule_sync_playback(plan, {rule_tag("guest-1"): 0})
    printed, out = _run_php(tmp_path, _PHP_BEFORE, fragment)
    assert printed == ""
    rules = out["config"]["filter"]["rule"]
    assert [r["tracker"] for r in rules] == ["1", "2", _RID_A, _RID_B, "3"]
    assert rules[2] == {
        "tracker": _RID_A,
        "type": "pass",
        "interface": "openvpn",
        "ipprotocol": "inet",
        "source": {"address": "198.51.100.2"},
        "destination": {"address": "192.0.2.0/24"},
        "descr": "meho-vpn-user:guest-1",
    }
    assert rules[3]["protocol"] == "tcp" and rules[3]["destination"]["port"] == "443"
    assert out["config"]["cert"] == _PHP_BEFORE["cert"]
    assert out["calls"] == [
        ["sort"],
        ["write_config", "meho: sync VPN user rules"],
        ["filter_configure"],
    ]


@_needs_php
def test_fragment_appends_when_the_openvpn_tab_is_empty(tmp_path: Path) -> None:
    before = {"filter": {"rule": [_php_rule("1", "lan", "lan out")]}}
    fragment = build_rule_sync_playback([_plan_row("guest-1", _RID_A)], {rule_tag("guest-1"): 0})
    _, out = _run_php(tmp_path, before, fragment)
    assert [r["tracker"] for r in out["config"]["filter"]["rule"]] == ["1", _RID_A]


@_needs_php
@pytest.mark.parametrize(
    "extra",
    [
        # A tagged rule of the user was added in the web page after the plan.
        _php_rule("9", "openvpn", "meho-vpn-user:guest-1"),
        # The new ridentifier is taken meanwhile.
        _php_rule(_RID_A, "lan", "other"),
    ],
)
def test_fragment_writes_nothing_when_the_rules_changed(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    before = {"filter": {"rule": [*_PHP_BEFORE["filter"]["rule"], extra]}}
    fragment = build_rule_sync_playback([_plan_row("guest-1", _RID_A)], {rule_tag("guest-1"): 0})
    printed, out = _run_php(tmp_path, before, fragment)
    assert printed.startswith("MEHO_RULES_CHANGED=")
    assert out["config"] == before and out["calls"] == []


def test_fragment_shape() -> None:
    fragment = build_rule_sync_playback(
        [_plan_row("guest-1", _RID_A)], {rule_tag("guest-1"): 2, rule_tag("other"): 0}
    )
    assert all(not line.lstrip().startswith(("!", "=")) for line in fragment.splitlines())
    assert fragment.count("write_config(") == 1 and "unset(" not in fragment
    assert "'meho-vpn-user:other' => 0," in fragment


# ===========================================================================
# Metadata and preview
# ===========================================================================


def test_the_op_is_registered_and_needs_approval() -> None:
    (op,) = [o for o in PFSENSE_OPS if o.op_id == "pfsense.openvpn.rule.sync"]
    assert (op.safety_level, op.requires_approval, op.group_key) == ("dangerous", True, "vpn")
    Draft202012Validator.check_schema(op.parameter_schema)
    Draft202012Validator.check_schema(op.response_schema or {})
    assert callable(getattr(PfSenseConnector, op.handler_attr))
    assert classify_op(op.op_id) == "write"
    assert is_root_shell(op.op_id)


async def test_the_preview_shows_the_whole_plan() -> None:
    from meho_backplane.operations._preview import PreviewContext

    users = "".join(f"<user><name>u{i}</name><uid>{2000 + i}</uid></user>" for i in range(60))
    fake = _Fake([_config(users=users)])
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=fake,
        operator=MagicMock(),
        target=None,
        params={"rules": [_want(name=f"u{i}") for i in range(60)]},
    )
    preview = await rule_module._rule_preview(ctx)
    assert preview is not None
    assert preview["counts"]["create"] == 60 and len(preview["rules"]) == 60
    assert not fake.played


async def test_the_preview_declines_bad_input() -> None:
    from meho_backplane.operations._preview import PreviewContext

    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=None,
        operator=MagicMock(),
        target=None,
        params={"rules": "x"},
    )
    assert await rule_module._rule_preview(ctx) is None

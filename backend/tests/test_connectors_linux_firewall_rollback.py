# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the firewall read with a login that is not root, the timed
``linux.firewall.load``, and ``linux.firewall.confirm``.

The load and confirm scripts also run for real here, under ``bash``, with
fake ``nft`` / ``systemd-run`` / ``systemctl`` programs that write each call
to a log. So the tests check the order of the steps on the host, not only
the text of the script.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux import ops_firewall_rollback as rollback
from meho_backplane.connectors.linux.ops import PathConfinementError
from meho_backplane.connectors.linux.ops_firewall import (
    FIREWALL_SHOW_COMMAND,
    LinuxFirewallReadError,
)
from meho_backplane.connectors.linux.ops_firewall_rollback import (
    FIREWALL_ROLLBACK_OPS,
    LinuxFirewallConfirmError,
    bound_rollback_seconds,
    build_firewall_confirm_script,
    build_firewall_load_with_rollback_script,
    build_restore_command,
    linux_firewall_confirm,
    parse_confirm_output,
    parse_load_with_rollback_output,
)
from meho_backplane.connectors.linux.ops_write import (
    LinuxWriteError,
    LinuxWriteSafetyError,
    build_firewall_load_script,
    linux_firewall_load,
)
from meho_backplane.settings import get_settings

_SUDO_CANARY = "linux-fw-sudo-canary-xyz-517"  # gitleaks:allow -- synthetic
_ROLLBACK_ID = "0123456789abcdef"
_ROLLBACK_MODULE = "meho_backplane.connectors.linux.ops_firewall_rollback"
_NEW_RULES = "table inet filter {\n  chain input { type filter hook input priority 0; }\n}\n"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="linux-fw-test",
    host="linux-host.test.invalid",
    port=22,
    secret_ref="meho/testing/linux/fw-test",
)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _connector(secret: dict[str, Any]) -> Any:
    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(return_value=secret)  # type: ignore[method-assign]
    return connector


# ---------------------------------------------------------------------------
# linux.firewall.show
# ---------------------------------------------------------------------------


def test_show_command_keeps_the_error_text() -> None:
    assert "2>/dev/null; " not in FIREWALL_SHOW_COMMAND.replace(">/dev/null 2>&1", "")
    assert "nft list ruleset;" in FIREWALL_SHOW_COMMAND


@pytest.mark.asyncio
async def test_show_non_root_reads_through_sudo() -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    sudo = AsyncMock(return_value=_proc(stdout="MEHO_BACKEND=nftables\ntable inet filter {\n}\n"))
    with (
        patch("meho_backplane.connectors.linux.ops_firewall.run_remote_bash_with_sudo", sudo),
        patch.object(connector, "_run_command", new_callable=AsyncMock) as plain,
    ):
        result = await connector.firewall_show(_TARGET, {})
    assert result == {"rows": ["table inet filter {", "}"], "total": 2, "backend": "nftables"}
    plain.assert_not_awaited()
    assert sudo.await_args is not None
    assert sudo.await_args.args[2] == FIREWALL_SHOW_COMMAND
    assert sudo.await_args.kwargs["sudo_password"] == _SUDO_CANARY


@pytest.mark.asyncio
async def test_show_non_root_without_password_sends_an_empty_one() -> None:
    connector = _connector({"username": "ops", "ssh_private_key": "k"})
    sudo = AsyncMock(return_value=_proc(stdout="MEHO_BACKEND=iptables\n*filter\nCOMMIT\n"))
    with (
        patch("meho_backplane.connectors.linux.ops_firewall.run_remote_bash_with_sudo", sudo),
        patch.object(connector, "_run_command", new_callable=AsyncMock) as plain,
    ):
        result = await connector.firewall_show(_TARGET, {})
    assert result["backend"] == "iptables"
    assert result["total"] == 2
    plain.assert_not_awaited()
    assert sudo.await_args is not None
    assert sudo.await_args.kwargs["sudo_password"] == ""


@pytest.mark.asyncio
async def test_show_failed_read_is_an_error_not_empty_rows() -> None:
    connector = _connector({"username": "root", "ssh_private_key": "k"})
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as plain:
        plain.return_value = _proc(
            stdout="MEHO_BACKEND=nftables\n",
            stderr="netlink: Error: cache initialization failed: Operation not permitted\n",
            exit_status=1,
        )
        with pytest.raises(LinuxFirewallReadError) as caught:
            await connector.firewall_show(_TARGET, {})
    message = str(caught.value)
    assert message.startswith("permission_denied")
    assert "nftables" in message


@pytest.mark.asyncio
async def test_show_sudo_refusal_is_permission_denied() -> None:
    connector = _connector({"username": "ops", "ssh_private_key": "k"})
    sudo = AsyncMock(
        return_value=_proc(stderr="sudo: 1 incorrect password attempt\n", exit_status=1)
    )
    with (
        patch("meho_backplane.connectors.linux.ops_firewall.run_remote_bash_with_sudo", sudo),
        pytest.raises(LinuxFirewallReadError, match=r"^permission_denied"),
    ):
        await connector.firewall_show(_TARGET, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_status", [2, None])
async def test_show_root_failed_read_is_read_failed(exit_status: int | None) -> None:
    connector = _connector({"username": "root", "password": _SUDO_CANARY})
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as plain:
        plain.return_value = _proc(stdout="MEHO_BACKEND=nftables\n", exit_status=exit_status)
        with pytest.raises(LinuxFirewallReadError, match=r"^read_failed") as caught:
            await connector.firewall_show(_TARGET, {})
    assert _SUDO_CANARY not in str(caught.value)


@pytest.mark.asyncio
async def test_show_host_without_tools_still_answers_backend_none() -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    with patch(
        "meho_backplane.connectors.linux.ops_firewall.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="MEHO_BACKEND=none\n")),
    ):
        result = await connector.firewall_show(_TARGET, {})
    assert result == {"rows": [], "total": 0, "backend": "none"}


# ---------------------------------------------------------------------------
# linux.firewall.load -- parameters and the plain path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_without_timer_builds_the_same_script_as_before() -> None:
    connector = _connector({"username": "root", "sudo_password": _SUDO_CANARY})
    sudo = AsyncMock(return_value=_proc(stdout="===MEHO_FW_OK===\n"))
    with patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", sudo):
        result = await linux_firewall_load(connector, _TARGET, {"ruleset": _NEW_RULES})
    assert result == {"applied": True, "backend": "nft", "validated": True}
    assert sudo.await_args is not None
    assert sudo.await_args.args[2] == build_firewall_load_script(_NEW_RULES, "nft")
    assert "systemd-run" not in sudo.await_args.args[2]


@pytest.mark.parametrize("value", [59, 1801, True, "120", 120.0])
def test_rollback_seconds_bounds(value: Any) -> None:
    with pytest.raises(LinuxWriteSafetyError):
        bound_rollback_seconds(value)


def test_rollback_seconds_edges_pass() -> None:
    assert bound_rollback_seconds(60) == 60
    assert bound_rollback_seconds(1800) == 1800


@pytest.mark.asyncio
async def test_load_with_bad_timer_value_is_refused_before_ssh() -> None:
    connector = _connector({"username": "root", "sudo_password": _SUDO_CANARY})
    sudo = AsyncMock()
    with (
        patch(
            "meho_backplane.connectors.linux.ops_firewall_rollback.run_remote_bash_with_sudo", sudo
        ),
        pytest.raises(LinuxWriteSafetyError),
    ):
        await linux_firewall_load(
            connector, _TARGET, {"ruleset": _NEW_RULES, "rollback_after_seconds": 5}
        )
    sudo.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_with_timer_returns_rollback_id_and_due_time() -> None:
    connector = _connector({"username": "root", "sudo_password": _SUDO_CANARY})
    sudo = AsyncMock(
        return_value=_proc(stdout="MEHO_FW_DUE=1793000000\n===MEHO_FW_OK===\n", exit_status=0)
    )
    with patch(
        "meho_backplane.connectors.linux.ops_firewall_rollback.run_remote_bash_with_sudo", sudo
    ):
        result = await linux_firewall_load(
            connector,
            _TARGET,
            {"ruleset": _NEW_RULES, "backend": "nft", "rollback_after_seconds": 300},
        )
    assert result["applied"] is True
    assert result["validated"] is True
    assert len(result["rollback_id"]) == 16
    assert datetime.fromisoformat(result["rollback_due_at"]).timestamp() == 1793000000
    assert sudo.await_args is not None
    script = sudo.await_args.args[2]
    assert "--on-active=300" in script
    assert result["rollback_id"] in script
    assert _SUDO_CANARY not in script


def test_load_output_without_due_line_is_not_success() -> None:
    with pytest.raises(LinuxWriteError, match="cannot tell how far"):
        parse_load_with_rollback_output(_proc(stdout="===MEHO_FW_OK===\n"), "nft", _ROLLBACK_ID)


# ---------------------------------------------------------------------------
# The scripts, run under bash with fake host tools
# ---------------------------------------------------------------------------


_FAKE_TOOL = """#!/bin/bash
echo "$(basename "$0") $*" >> "$FAKE_LOG"
name="$(basename "$0")"
case "$name $1" in
  "nft list") echo "table inet old {}"; exit "${NFT_LIST_RC:-0}" ;;
  "nft -c")
    case "$3" in
      *new.rules)
        if [ -n "${DUE_ON_CHECK:-}" ]; then printf %s "$DUE_ON_CHECK" > "$(dirname "$3")/due"; fi
        exit "${NFT_CHECK_NEW_RC:-0}" ;;
    esac
    exit "${NFT_CHECK_SAVED_RC:-0}" ;;
  "nft -f")
    case "$2" in *new.rules) exit "${NFT_LOAD_RC:-0}" ;; esac
    exit "${NFT_LOAD_SAVED_RC:-0}" ;;
  "iptables-save "*) printf '*filter\\n:INPUT DROP [0:0]\\nCOMMIT\\n' ;;
  "iptables-restore --test") cat > /dev/null ;;
  "iptables-restore "*)
    data="$(cat)"; printf '%s\\n' "$data" >> "$FAKE_LOG.loaded"
    case "$data" in *"${IPT_FAIL_ON:-@none@}"*) exit 1 ;; esac ;;
  "systemd-run "*) exit "${SYSTEMD_RUN_RC:-0}" ;;
  "systemctl is-active") exit "${SERVICE_ACTIVE_RC:-3}" ;;
  "systemctl stop") exit "${STOP_RC:-0}" ;;
esac
exit 0
"""


@dataclass
class _Host:
    root: Path
    state_root: Path
    systemd_dir: Path
    bin_dir: Path
    log: Path

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        script = script.replace("/run/systemd/system", str(self.systemd_dir))
        full_env = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_LOG": str(self.log),
            **env,
        }
        return subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            env=full_env,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    state_root = tmp_path / "state"
    monkeypatch.setattr(rollback, "ROLLBACK_STATE_ROOT", str(state_root))
    systemd_dir = tmp_path / "run-systemd"
    systemd_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("nft", "iptables-save", "iptables-restore", "systemd-run", "systemctl"):
        path = bin_dir / tool
        path.write_text(_FAKE_TOOL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return _Host(tmp_path, state_root, systemd_dir, bin_dir, tmp_path / "calls.log")


def _load_script(seconds: int = 300, rollback_id: str = _ROLLBACK_ID) -> str:
    return build_firewall_load_with_rollback_script(
        _NEW_RULES, "nft", rollback_id=rollback_id, seconds=seconds
    )


def _index(calls: list[str], prefix: str) -> int:
    return next(i for i, call in enumerate(calls) if call.startswith(prefix))


def test_timed_load_runs_save_check_timer_load_in_order(host: _Host) -> None:
    done = host.run(_load_script())
    assert done.returncode == 0, done.stderr
    calls = host.calls()
    save = _index(calls, "nft list ruleset")
    check_saved = _index(calls, "nft -c -f")
    check_new = next(
        i for i, c in enumerate(calls) if c.startswith("nft -c -f") and "new.rules" in c
    )
    timer = _index(calls, "systemd-run")
    load = next(i for i, c in enumerate(calls) if c.startswith("nft -f") and "new.rules" in c)
    assert save < check_saved < check_new < timer < load
    assert "saved.rules" in calls[check_saved]
    state = host.state_root / _ROLLBACK_ID
    assert (state / "saved.rules").read_text().startswith("flush ruleset\n")
    assert (state / "new.rules").read_text() == _NEW_RULES
    assert (state / "backend").read_text() == "nft"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert "--on-active=300" in calls[timer]
    assert f"meho-fw-rollback-{_ROLLBACK_ID}" in calls[timer]
    result = parse_load_with_rollback_output(_proc(stdout=done.stdout), "nft", _ROLLBACK_ID)
    assert result["rollback_id"] == _ROLLBACK_ID
    assert datetime.fromisoformat(result["rollback_due_at"]).timestamp() == int(
        (state / "due").read_text()
    )


def test_timed_load_timer_failure_loads_nothing(host: _Host) -> None:
    done = host.run(_load_script(), SYSTEMD_RUN_RC="1")
    assert done.returncode == 4
    assert not any(c.startswith("nft -f") for c in host.calls())
    assert not (host.state_root / _ROLLBACK_ID).exists()
    with pytest.raises(LinuxWriteError, match=r"timer.*nothing was changed"):
        parse_load_with_rollback_output(
            _proc(stdout=done.stdout, stderr=done.stderr, exit_status=4), "nft", _ROLLBACK_ID
        )


@pytest.mark.parametrize(
    "env,stage",
    [({"NFT_LIST_RC": "1"}, "save the live rules"), ({"NFT_CHECK_SAVED_RC": "1"}, "saved copy")],
)
def test_timed_load_save_failure_starts_no_timer(
    host: _Host, env: dict[str, str], stage: str
) -> None:
    done = host.run(_load_script(), **env)
    assert done.returncode == 4
    assert not any(c.startswith(("systemd-run", "nft -f")) for c in host.calls())
    with pytest.raises(LinuxWriteError, match=stage):
        parse_load_with_rollback_output(
            _proc(stdout=done.stdout, stderr=done.stderr, exit_status=4), "nft", _ROLLBACK_ID
        )


def test_timed_load_bad_new_rules_start_no_timer(host: _Host) -> None:
    done = host.run(_load_script(), NFT_CHECK_NEW_RC="1")
    assert done.returncode == 3
    assert not any(c.startswith(("systemd-run", "nft -f")) for c in host.calls())
    result = parse_load_with_rollback_output(
        _proc(stdout=done.stdout, stderr=done.stderr, exit_status=3), "nft", _ROLLBACK_ID
    )
    assert result["validated"] is False
    assert result["applied"] is False


def test_timed_load_failed_load_restores_the_saved_rules_then_stops_the_timer(
    host: _Host,
) -> None:
    done = host.run(_load_script(), NFT_LOAD_RC="1")
    assert done.returncode == 4
    calls = host.calls()
    restore = next(i for i, c in enumerate(calls) if c.startswith("nft -f") and "saved" in c)
    stop = calls.index(f"systemctl stop meho-fw-rollback-{_ROLLBACK_ID}.timer")
    assert restore < stop
    assert not (host.state_root / _ROLLBACK_ID).exists()
    result = parse_load_with_rollback_output(
        _proc(stdout=done.stdout, stderr=done.stderr, exit_status=4), "nft", _ROLLBACK_ID
    )
    assert result["applied"] is False
    assert "loaded the saved rules again" in result["error"]


def test_timed_load_failed_load_and_failed_restore_keeps_the_timer(host: _Host) -> None:
    done = host.run(_load_script(), NFT_LOAD_RC="1", NFT_LOAD_SAVED_RC="1")
    assert done.returncode == 4
    assert not any(c.startswith("systemctl stop") for c in host.calls())
    state = host.state_root / _ROLLBACK_ID
    assert (state / "saved.rules").exists()
    assert (state / "due").exists()
    with pytest.raises(LinuxWriteError, match="timer still runs"):
        parse_load_with_rollback_output(
            _proc(stdout=done.stdout, stderr=done.stderr, exit_status=4), "nft", _ROLLBACK_ID
        )


def test_timed_load_refuses_a_host_without_systemd(host: _Host) -> None:
    host.systemd_dir.rmdir()
    done = host.run(_load_script())
    assert done.returncode == 5
    assert host.calls() == []
    with pytest.raises(LinuxWriteError, match="no systemd"):
        parse_load_with_rollback_output(
            _proc(stdout=done.stdout, stderr=done.stderr, exit_status=5), "nft", _ROLLBACK_ID
        )


def test_timed_load_iptables_saves_with_iptables_save(host: _Host) -> None:
    script = build_firewall_load_with_rollback_script(
        "*filter\nCOMMIT\n", "iptables", rollback_id=_ROLLBACK_ID, seconds=60
    )
    assert 'iptables-save > "$saved"' in script
    assert 'iptables-restore --test < "$saved"' in script
    assert "iptables-restore < " in script


def test_timed_load_iptables_saved_copy_clears_a_table_the_new_rules_add(host: _Host) -> None:
    new_rules = "*filter\n:INPUT ACCEPT [0:0]\nCOMMIT\n*raw\n-A PREROUTING -j DROP\nCOMMIT\n"
    script = build_firewall_load_with_rollback_script(
        new_rules, "iptables", rollback_id=_ROLLBACK_ID, seconds=60
    )
    done = host.run(script)
    assert done.returncode == 0, done.stderr
    saved = (host.state_root / _ROLLBACK_ID / "saved.rules").read_text()
    assert saved.count("*filter") == 1
    assert "*raw\n:PREROUTING ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\nCOMMIT\n" in saved
    loaded_log = Path(f"{host.log}.loaded")
    loaded_log.unlink()
    restored = host.run(build_restore_command("iptables", _ROLLBACK_ID))
    assert restored.returncode == 0, restored.stderr
    assert loaded_log.read_text() == saved
    assert (host.state_root / _ROLLBACK_ID / "restored").exists()


def test_timed_load_iptables_partial_load_is_undone_with_the_saved_rules(host: _Host) -> None:
    # Legacy iptables commits table by table: here filter is in, then raw fails.
    new_rules = "*filter\n:INPUT ACCEPT [0:0]\nCOMMIT\n*raw\n-A PREROUTING -j DROP\nCOMMIT\n"
    script = build_firewall_load_with_rollback_script(
        new_rules, "iptables", rollback_id=_ROLLBACK_ID, seconds=60
    )
    done = host.run(script, IPT_FAIL_ON="-j DROP")
    assert done.returncode == 4
    loaded = Path(f"{host.log}.loaded").read_text()
    last_load = loaded.rsplit("*filter\n:INPUT DROP", 1)[1]
    assert "-j DROP" not in last_load
    assert "*raw\n:PREROUTING ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\nCOMMIT" in last_load
    assert f"systemctl stop meho-fw-rollback-{_ROLLBACK_ID}.timer" in host.calls()
    result = parse_load_with_rollback_output(
        _proc(stdout=done.stdout, stderr=done.stderr, exit_status=4), "iptables", _ROLLBACK_ID
    )
    assert "loaded the saved rules again" in result["error"]


def test_timed_load_refuses_an_iptables_table_it_cannot_put_back() -> None:
    with pytest.raises(LinuxWriteSafetyError, match="not one MEHO can put back"):
        build_firewall_load_with_rollback_script(
            "*broute\nCOMMIT\n", "iptables", rollback_id=_ROLLBACK_ID, seconds=60
        )


_OTHER_ID = "fedcba9876543210"


def _parse_load(done: subprocess.CompletedProcess[str], rollback_id: str) -> dict[str, Any]:
    return parse_load_with_rollback_output(
        _proc(stdout=done.stdout, stderr=done.stderr, exit_status=done.returncode),
        "nft",
        rollback_id,
    )


def test_timed_load_refuses_while_another_change_waits(host: _Host) -> None:
    assert host.run(_load_script()).returncode == 0
    calls_before = len(host.calls())
    second = host.run(_load_script(rollback_id=_OTHER_ID))
    assert second.returncode == 4
    assert host.calls()[calls_before:] == []
    assert not (host.state_root / _OTHER_ID).exists()
    assert (host.state_root / _ROLLBACK_ID / "saved.rules").exists()
    with pytest.raises(LinuxWriteError, match="still waits"):
        _parse_load(second, _OTHER_ID)


def test_timed_load_works_again_after_the_first_change_is_confirmed(host: _Host) -> None:
    assert host.run(_load_script()).returncode == 0
    assert _confirm(host)["confirmed"] is True
    second = host.run(_load_script(rollback_id=_OTHER_ID))
    assert second.returncode == 0, second.stderr
    assert _parse_load(second, _OTHER_ID)["rollback_id"] == _OTHER_ID


def test_timed_load_refuses_while_the_restore_of_another_change_runs(host: _Host) -> None:
    _prepare_state(host, seconds_left=-120)
    second = host.run(_load_script(rollback_id=_OTHER_ID), SERVICE_ACTIVE_RC="0")
    assert second.returncode == 4
    with pytest.raises(LinuxWriteError, match="still waits"):
        _parse_load(second, _OTHER_ID)
    # Once that restore is done, a new load may start.
    (host.state_root / _ROLLBACK_ID / "restored").write_text("")
    assert host.run(_load_script(rollback_id=_OTHER_ID)).returncode == 0


def _hold_lock(host: _Host) -> subprocess.Popen[bytes]:
    host.state_root.mkdir(parents=True, exist_ok=True)
    lock = host.state_root / ".lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    for _ in range(100):
        probe = subprocess.run(["flock", "-n", str(lock), "true"], check=False)
        if probe.returncode != 0:
            return holder
        subprocess.run(["sleep", "0.05"], check=True)
    holder.kill()
    raise AssertionError("the lock holder did not start")


def test_timed_load_waits_for_the_host_lock(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rollback, "LOCK_WAIT_SECONDS", 1)
    holder = _hold_lock(host)
    try:
        done = host.run(_load_script())
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 4
    assert not any(c.startswith(("nft", "systemd-run")) for c in host.calls())
    with pytest.raises(LinuxWriteError, match="holds the lock"):
        _parse_load(done, _ROLLBACK_ID)


def _prepare_state(host: _Host, *, seconds_left: int, restored: bool = False) -> Path:
    state = host.state_root / _ROLLBACK_ID
    state.mkdir(parents=True)
    (state / "new.rules").write_text(_NEW_RULES)
    (state / "saved.rules").write_text("flush ruleset\n")
    (state / "backend").write_text("nft")
    now = int(datetime.now().timestamp())
    (state / "due").write_text(str(now + seconds_left))
    if restored:
        (state / "restored").write_text("")
    return state


def _confirm(host: _Host, install_path: str | None = None, **env: str) -> dict[str, Any]:
    done = host.run(build_firewall_confirm_script(_ROLLBACK_ID, install_path), **env)
    return parse_confirm_output(
        _proc(stdout=done.stdout, stderr=done.stderr, exit_status=done.returncode),
        _ROLLBACK_ID,
        install_path,
    )


def test_confirm_stops_the_timer_and_installs_the_rules(host: _Host) -> None:
    _prepare_state(host, seconds_left=200)
    boot_file = host.root / "etc" / "nftables.conf"
    boot_file.parent.mkdir()
    boot_file.write_text("old\n")
    boot_file.chmod(0o755)
    result = _confirm(host, str(boot_file))
    assert result == {
        "confirmed": True,
        "rollback_id": _ROLLBACK_ID,
        "installed": True,
        "install_path": str(boot_file),
    }
    assert boot_file.read_text() == _NEW_RULES
    assert stat.S_IMODE(boot_file.stat().st_mode) == 0o755
    assert f"systemctl stop meho-fw-rollback-{_ROLLBACK_ID}.timer" in host.calls()
    assert not (host.state_root / _ROLLBACK_ID).exists()


def test_confirm_without_install_only_stops_the_timer(host: _Host) -> None:
    _prepare_state(host, seconds_left=200)
    result = _confirm(host)
    assert result["confirmed"] is True
    assert result["installed"] is False
    assert "error" not in result


def test_confirm_refuses_close_to_the_end_of_the_timer(host: _Host) -> None:
    _prepare_state(host, seconds_left=10)
    with pytest.raises(LinuxFirewallConfirmError, match="old rules are coming back"):
        _confirm(host)
    assert not any("stop" in c for c in host.calls())


def test_confirm_refuses_an_unknown_rollback_id(host: _Host) -> None:
    with pytest.raises(LinuxFirewallConfirmError, match="no timed firewall change"):
        _confirm(host)
    assert host.calls() == []


def test_confirm_after_the_timer_ran_says_the_old_rules_are_back(host: _Host) -> None:
    _prepare_state(host, seconds_left=-5, restored=True)
    with pytest.raises(LinuxFirewallConfirmError, match="old rules are back"):
        _confirm(host)


def test_confirm_after_the_timer_ran_without_restore_mark_is_unclear(host: _Host) -> None:
    _prepare_state(host, seconds_left=-5)
    with pytest.raises(LinuxFirewallConfirmError, match="cannot see"):
        _confirm(host)


def test_confirm_while_the_restore_waits_to_run_says_it_is_too_late(host: _Host) -> None:
    _prepare_state(host, seconds_left=-5)
    with pytest.raises(LinuxFirewallConfirmError, match="old rules are coming back"):
        _confirm(host, SERVICE_ACTIVE_RC="0")
    assert not any(c.startswith("systemctl stop") for c in host.calls())


def test_confirm_checks_the_time_again_after_the_rules_check(host: _Host) -> None:
    state = _prepare_state(host, seconds_left=200)
    almost_over = str(int(datetime.now().timestamp()) + 5)
    with pytest.raises(LinuxFirewallConfirmError, match="old rules are coming back"):
        _confirm(host, str(host.root / "nftables.conf"), DUE_ON_CHECK=almost_over)
    assert not any(c.startswith("systemctl stop") for c in host.calls())
    assert not (state / "keep").exists()
    restored = host.run(build_restore_command("nft", _ROLLBACK_ID))
    assert restored.returncode == 0, restored.stderr
    assert (state / "restored").exists()


def test_restore_after_confirm_does_nothing(host: _Host) -> None:
    _prepare_state(host, seconds_left=200)
    assert _confirm(host)["confirmed"] is True
    done = host.run(build_restore_command("nft", _ROLLBACK_ID))
    assert done.returncode == 0, done.stderr
    assert not any(c.startswith("nft -f") for c in host.calls())


def test_restore_skips_a_change_marked_keep(host: _Host) -> None:
    state = _prepare_state(host, seconds_left=200)
    (state / "keep").write_text("")
    host.run(build_restore_command("nft", _ROLLBACK_ID))
    assert not any(c.startswith("nft -f") for c in host.calls())
    assert not (state / "restored").exists()


def test_restore_waits_for_a_confirm_that_holds_the_lock(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollback, "_RESTORE_LOCK_WAIT_SECONDS", 1)
    _prepare_state(host, seconds_left=200)
    holder = _hold_lock(host)
    try:
        assert "flock -w 1 9" in build_restore_command("nft", _ROLLBACK_ID)
        started = datetime.now()
        host.run(build_restore_command("nft", _ROLLBACK_ID))
        assert (datetime.now() - started).total_seconds() >= 0.9
    finally:
        holder.kill()
        holder.wait()


def test_confirm_waits_for_the_host_lock(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rollback, "LOCK_WAIT_SECONDS", 1)
    _prepare_state(host, seconds_left=200)
    holder = _hold_lock(host)
    try:
        with pytest.raises(LinuxFirewallConfirmError, match="holds the lock"):
            _confirm(host)
    finally:
        holder.kill()
        holder.wait()
    assert not any(c.startswith("systemctl stop") for c in host.calls())


def test_confirm_install_check_failure_keeps_the_timer(host: _Host) -> None:
    _prepare_state(host, seconds_left=200)
    with pytest.raises(LinuxFirewallConfirmError, match="did not stop the timer"):
        _confirm(host, str(host.root / "nftables.conf"), NFT_CHECK_NEW_RC="1")
    assert not any(c.startswith("systemctl stop") for c in host.calls())


def test_confirm_stop_failure_is_an_error(host: _Host) -> None:
    state = _prepare_state(host, seconds_left=200)
    with pytest.raises(LinuxFirewallConfirmError, match="could not stop the timer"):
        _confirm(host, STOP_RC="1")
    assert not (state / "keep").exists()


def test_confirm_install_failure_reports_the_timer_is_stopped(host: _Host) -> None:
    _prepare_state(host, seconds_left=200)
    result = _confirm(host, str(host.root / "missing-dir" / "nftables.conf"))
    assert result["confirmed"] is True
    assert result["installed"] is False
    assert "not installed" in result["error"]


# ---------------------------------------------------------------------------
# linux.firewall.confirm -- the handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_opens_a_new_connection_and_closes_it() -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    connector._auth_config = AsyncMock(  # type: ignore[method-assign]
        return_value={"username": "ops", "known_hosts": None, "password": "x"}
    )
    connector._connect = AsyncMock()  # type: ignore[method-assign]
    conn = MagicMock()
    conn.wait_closed = AsyncMock()
    dial = AsyncMock(return_value=conn)
    run = AsyncMock(return_value=_proc(stdout="MEHO_FWC=stopped\nMEHO_FWC=done\n"))
    with (
        patch("meho_backplane.connectors.linux.ops_firewall_rollback._dial", dial),
        patch("meho_backplane.connectors.linux.ops_firewall_rollback.run_capped", run),
    ):
        result = await connector.firewall_confirm(_TARGET, {"rollback_id": _ROLLBACK_ID})
    assert result["confirmed"] is True
    dial.assert_awaited_once()
    connector._connect.assert_not_awaited()
    conn.close.assert_called_once()
    assert run.await_args is not None
    stdin = run.await_args.kwargs["stdin"]
    assert stdin.endswith(f"{_SUDO_CANARY}\n")
    assert _SUDO_CANARY not in run.await_args.args[1]
    assert _SUDO_CANARY not in repr(result)


@pytest.mark.asyncio
async def test_confirm_closes_the_connection_when_the_run_fails() -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    connector._auth_config = AsyncMock(  # type: ignore[method-assign]
        return_value={"username": "ops", "known_hosts": None, "password": "x"}
    )
    conn = MagicMock()
    conn.wait_closed = AsyncMock()
    with (
        patch(
            "meho_backplane.connectors.linux.ops_firewall_rollback._dial",
            AsyncMock(return_value=conn),
        ),
        patch(
            "meho_backplane.connectors.linux.ops_firewall_rollback.run_capped",
            AsyncMock(side_effect=OSError("connection reset")),
        ),
        pytest.raises(OSError),
    ):
        await connector.firewall_confirm(_TARGET, {"rollback_id": _ROLLBACK_ID})
    conn.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/home/ops/rules.conf", "/etc/../root/rules", "etc/x"])
async def test_confirm_refuses_install_path_outside_the_write_roots(path: str) -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    dial = AsyncMock()
    with (
        patch("meho_backplane.connectors.linux.ops_firewall_rollback._dial", dial),
        pytest.raises(PathConfinementError),
    ):
        await linux_firewall_confirm(
            connector, _TARGET, {"rollback_id": _ROLLBACK_ID, "install_path": path}
        )
    dial.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_id", ["", "../../etc", "0123456789ABCDEF", "abc", 7])
async def test_confirm_refuses_a_bad_rollback_id_before_ssh(rollback_id: Any) -> None:
    connector = _connector({"username": "ops", "sudo_password": _SUDO_CANARY})
    dial = AsyncMock()
    with (
        patch("meho_backplane.connectors.linux.ops_firewall_rollback._dial", dial),
        pytest.raises(LinuxWriteSafetyError),
    ):
        await linux_firewall_confirm(connector, _TARGET, {"rollback_id": rollback_id})
    dial.assert_not_awaited()


def test_confirm_op_is_caution_and_waits_for_approval() -> None:
    (op,) = FIREWALL_ROLLBACK_OPS
    assert op.op_id == "linux.firewall.confirm"
    assert op.group_key == "firewall"
    assert op.safety_level == "caution"
    assert op.requires_approval is True
    assert op.parameter_schema["required"] == ["rollback_id"]

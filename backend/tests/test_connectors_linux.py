# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the linux-ssh connector T1 read floor (#3360).

Coverage matrix (per Task #3360 acceptance criteria):

* :func:`parse_os_release` / :func:`_derive_vendor` /
  :func:`parse_fingerprint_output` -- identity parsing from the single
  fixed fingerprint round-trip.
* :meth:`LinuxSshConnector.fingerprint` -- reachable identity + the #986
  unreachable guard (transport / credential failure → ``reachable=False``
  + ``extras["error"]``, never an unhandled raise).
* :meth:`LinuxSshConnector.probe` -- the five-reason matrix
  (``tcp_unreachable`` / ``ssh_auth_failed`` / ``command_failed`` /
  ``os_release_unreadable`` / ``systemd_absent``).
* :meth:`LinuxSshConnector.about` -- identity snapshot + the #986
  ConnectorUnreachableError on an unreachable target.
* The six read handlers -- command construction, output parsing, the
  ``{rows, total}`` envelope for set-shaped verbs, and the
  systemctl-absent connector error.
* ``shlex.quote`` injection-safety on every parameterized command, and
  path-confinement (:func:`confine_read_path` / :func:`ensure_path_under_root`).
* ``LINUX_OPS`` registration shape -- op count, all safe / read-only /
  no-approval, namespaced, closed parameter schemas, SSH-transport
  ``when_to_use`` on every op, handler attrs on the class.
* The schema-level secret-hygiene invariant (no ``password`` / ``secret``
  parameter) and group coverage (every declared group has a curated
  ``when_to_use``; registration fails closed without one).
* The registry triple + wildcard, and JSONFlux collection detection over
  the ``{rows, total}`` envelope.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.adapters.ssh import ConnectorUnreachableError, SshConnector
from meho_backplane.connectors.linux import LINUX_OPS, LinuxSshConnector
from meho_backplane.connectors.linux.connector import (
    _derive_vendor,
    parse_fingerprint_output,
    parse_os_release,
)
from meho_backplane.connectors.linux.ops import (
    LINUX_READ_ROOTS,
    LINUX_WHEN_TO_USE_BY_GROUP,
    PathConfinementError,
    confine_read_path,
    ensure_path_under_root,
    normalise_json_rows,
)
from meho_backplane.connectors.linux.ops_file import (
    build_file_read_command,
    build_log_tail_command,
)
from meho_backplane.connectors.linux.ops_firewall import parse_firewall_output
from meho_backplane.connectors.linux.ops_host import (
    LinuxServiceStatusProbeError,
    build_service_status_command,
    build_sysctl_read_command,
    validate_sysctl_key,
    validate_unit_name,
)
from meho_backplane.connectors.linux.ops_storage import (
    parse_export_line,
    parse_mount_line,
    parse_mount_table,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer, _detect_collection
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

# ---------------------------------------------------------------------------
# Environment fixture (settings cache requires the env vars to resolve)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


_CANARY_PASSWORD = "linux-canary-pw-xyz-771"  # gitleaks:allow NOSONAR -- synthetic canary
# Synthetic key-shaped canary that does not trip the detect-private-key hook.
_CANARY_SSH_KEY = "LINUX-CANARY-KEY-MARKER-QWER5678ZX"  # gitleaks:allow -- synthetic canary


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str  # a Vault KV-v2 path STRING (#2155)


_TARGET_SECRET_PATH = "meho/testing/linux/host-test"

_TARGET = _StubTarget(
    name="linux-host-test",
    host="linux-host.test.invalid",
    port=22,
    secret_ref=_TARGET_SECRET_PATH,
)


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets(
        {
            _TARGET_SECRET_PATH: {
                "username": "root",
                "password": _CANARY_PASSWORD,
                "ssh_private_key": _CANARY_SSH_KEY,
            }
        }
    ):
        yield


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _cmd_of(mock: AsyncMock) -> str:
    """Return the command string of the single ``_run_command`` call."""
    return mock.await_args_list[0].args[1]


_OS_RELEASE_UBUNTU = (
    'NAME="Ubuntu"\n'
    'VERSION="22.04.3 LTS (Jammy Jellyfish)"\n'
    "ID=ubuntu\n"
    "ID_LIKE=debian\n"
    'PRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
    'VERSION_ID="22.04"\n'
)


def _fingerprint_stdout(
    *,
    hostname: str = "router01",
    kernel: str = "5.15.0-88-generic",
    init: str = "systemd",
    os_release: str = _OS_RELEASE_UBUNTU,
) -> str:
    return (
        "===MEHO_HOSTNAME===\n"
        f"{hostname}\n"
        "===MEHO_KERNEL===\n"
        f"{kernel}\n"
        "===MEHO_INIT===\n"
        f"{init}\n"
        "===MEHO_OSRELEASE===\n"
        f"{os_release}"
    )


# ---------------------------------------------------------------------------
# parse_os_release / _derive_vendor / parse_fingerprint_output
# ---------------------------------------------------------------------------


def test_parse_os_release_strips_quotes_and_ignores_junk() -> None:
    osr = parse_os_release(_OS_RELEASE_UBUNTU + "# comment\n\nMALFORMED LINE\n")
    assert osr["ID"] == "ubuntu"
    assert osr["VERSION_ID"] == "22.04"
    assert osr["ID_LIKE"] == "debian"
    assert osr["PRETTY_NAME"] == "Ubuntu 22.04.3 LTS"


def test_derive_vendor_prefers_id_like_family() -> None:
    assert _derive_vendor({"ID": "ubuntu", "ID_LIKE": "debian"}) == "debian"
    # Multi-token ID_LIKE (Rocky) → first token.
    assert _derive_vendor({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}) == "rhel"
    # No ID_LIKE → the distro is its own family.
    assert _derive_vendor({"ID": "debian"}) == "debian"
    # Neither present → generic linux.
    assert _derive_vendor({}) == "linux"


def test_parse_fingerprint_output_splits_sections() -> None:
    parsed = parse_fingerprint_output(_fingerprint_stdout())
    assert parsed["hostname"] == "router01"
    assert parsed["kernel"] == "5.15.0-88-generic"
    assert parsed["init_system"] == "systemd"
    assert parsed["os_release"]["VERSION_ID"] == "22.04"


def test_parse_fingerprint_output_missing_sections_are_none() -> None:
    parsed = parse_fingerprint_output("no markers here at all")
    assert parsed["hostname"] is None
    assert parsed["kernel"] is None
    assert parsed["init_system"] is None
    assert parsed["os_release"] == {}


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_returns_distro_version_kernel() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_fingerprint_stdout())
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is True
    assert result.vendor == "debian"
    assert result.product == "linux"
    assert result.version == "22.04"
    assert result.build == "5.15.0-88-generic"
    assert result.probe_method == "ssh: cat /etc/os-release"
    assert result.extras["hostname"] == "router01"
    assert result.extras["os_pretty"] == "Ubuntu 22.04.3 LTS"
    assert result.extras["init_system"] == "systemd"
    assert result.extras["distro_id"] == "ubuntu"
    # A single fixed round-trip.
    assert mock_cmd.await_count == 1


@pytest.mark.asyncio
async def test_fingerprint_version_none_when_os_release_absent() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_fingerprint_stdout(os_release="", init="unknown"))
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is True
    assert result.version is None
    assert result.vendor == "linux"
    assert result.extras["init_system"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [
        OSError("Connection refused"),
        asyncssh.PermissionDenied(reason="publickey auth failed"),
        TimeoutError("fingerprint timed out"),
        ValueError("secret carries neither key nor password"),
    ],
)
async def test_fingerprint_unreachable_maps_to_reachable_false(boom: Exception) -> None:
    """AC (#986): a transport/credential failure → reachable=False + error, no raise."""
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", AsyncMock(side_effect=boom)):
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is False
    assert result.vendor == "linux"
    assert result.product == "linux"
    assert result.probe_method == "ssh: cat /etc/os-release"
    assert "error" in result.extras


# ---------------------------------------------------------------------------
# probe -- the five-reason matrix (AC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_tcp_unreachable() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("refused"))):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "tcp_unreachable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [
        asyncssh.PermissionDenied("bad creds"),
        asyncssh.DisconnectError(asyncssh.DISC_PROTOCOL_ERROR, "handshake"),
        ValueError("no usable secret"),
    ],
)
async def test_probe_ssh_auth_failed(boom: Exception) -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=boom)):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "ssh_auth_failed"


@pytest.mark.asyncio
async def test_probe_command_failed_after_connect() -> None:
    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(
            connector, "_run_command", AsyncMock(side_effect=asyncssh.ConnectionLost("x"))
        ),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "command_failed"


@pytest.mark.asyncio
async def test_probe_os_release_unreadable() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock(return_value=_proc(stdout="", exit_status=1))
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "os_release_unreadable"


@pytest.mark.asyncio
async def test_probe_systemd_absent() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock(
        side_effect=[
            _proc(stdout=_OS_RELEASE_UBUNTU, exit_status=0),  # os-release ok
            _proc(stdout="other\n", exit_status=0),  # systemd check → not systemd
        ]
    )
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "systemd_absent"


@pytest.mark.asyncio
async def test_probe_ok_when_reachable_systemd_host() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock(
        side_effect=[
            _proc(stdout=_OS_RELEASE_UBUNTU, exit_status=0),
            _proc(stdout="systemd\n", exit_status=0),
        ]
    )
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None
    assert result.latency_ms is not None and result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# about shim (identity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_returns_identity_snapshot() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_fingerprint_stdout())
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "debian"
    assert result["product"] == "linux"
    assert result["version"] == "22.04"
    assert result["kernel"] == "5.15.0-88-generic"
    assert result["os_pretty"] == "Ubuntu 22.04.3 LTS"
    assert result["hostname"] == "router01"
    assert result["init_system"] == "systemd"


@pytest.mark.asyncio
async def test_about_unreachable_raises_connector_error() -> None:
    """AC (#986): an unreachable host maps to ConnectorUnreachableError, not a hollow ok."""
    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", AsyncMock(side_effect=OSError("refused"))),
        pytest.raises(ConnectorUnreachableError),
    ):
        await connector.about(_TARGET, {})


# ---------------------------------------------------------------------------
# file.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_read_returns_content_and_confines_path() -> None:
    connector = LinuxSshConnector()
    stdout = "42\n===MEHO_CONTENT===\nsentinel-ok\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.file_read(_TARGET, {"path": "/var/lib/firstboot.done"})
    assert result["path"] == "/var/lib/firstboot.done"
    assert result["content"] == "sentinel-ok\n"
    assert result["size_bytes"] == 42
    assert result["exists"] is True
    assert result["truncated"] is False
    cmd = _cmd_of(mock_cmd)
    assert shlex.quote("/var/lib/firstboot.done") in cmd
    assert "head -c 65536" in cmd


@pytest.mark.asyncio
async def test_file_read_missing_file_reports_absent() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="===MEHO_FILE_MISSING===\n")
        result = await connector.file_read(_TARGET, {"path": "/var/lib/firstboot.done"})
    assert result["exists"] is False
    assert result["content"] == ""
    assert result["size_bytes"] is None


@pytest.mark.asyncio
async def test_file_read_truncated_when_size_exceeds_cap() -> None:
    connector = LinuxSshConnector()
    stdout = "999999\n===MEHO_CONTENT===\nfirst-chunk"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.file_read(_TARGET, {"path": "/var/log/syslog", "max_bytes": 10})
    assert result["truncated"] is True
    assert "head -c 10" in _cmd_of(mock_cmd)


@pytest.mark.asyncio
async def test_file_read_rejects_traversal_before_ssh() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock()
    with patch.object(connector, "_run_command", run_mock), pytest.raises(PathConfinementError):
        await connector.file_read(_TARGET, {"path": "/etc/../../root/.ssh/id_rsa"})
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_read_rejects_path_outside_allowlist() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock()
    with patch.object(connector, "_run_command", run_mock), pytest.raises(PathConfinementError):
        await connector.file_read(_TARGET, {"path": "/tmp/evil"})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# log.tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_tail_returns_rows_total_envelope() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="line1\nline2\nline3\n")
        result = await connector.log_tail(_TARGET, {"path": "/var/log/cloud-init.log", "lines": 3})
    assert result["rows"] == ["line1", "line2", "line3"]
    assert result["total"] == 3
    assert result["path"] == "/var/log/cloud-init.log"
    cmd = _cmd_of(mock_cmd)
    assert "tail -n 3" in cmd
    assert shlex.quote("/var/log/cloud-init.log") in cmd


@pytest.mark.asyncio
async def test_log_tail_default_lines_and_confinement() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="")
        await connector.log_tail(_TARGET, {"path": "/var/log/syslog"})
    assert "tail -n 200" in _cmd_of(mock_cmd)


@pytest.mark.asyncio
async def test_log_tail_rejects_path_outside_allowlist() -> None:
    connector = LinuxSshConnector()
    run_mock = AsyncMock()
    with patch.object(connector, "_run_command", run_mock), pytest.raises(PathConfinementError):
        await connector.log_tail(_TARGET, {"path": "/home/user/secrets"})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# service.status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_status_parses_active_enabled_substate() -> None:
    connector = LinuxSshConnector()
    stdout = "active=active\nenabled=enabled\nsub=running\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout, exit_status=0)
        result = await connector.service_status(_TARGET, {"unit": "sshd.service"})
    assert result == {
        "unit": "sshd.service",
        "active": "active",
        "enabled": "enabled",
        "sub_state": "running",
    }
    cmd = _cmd_of(mock_cmd)
    assert "systemctl is-active" in cmd
    assert shlex.quote("sshd.service") in cmd


@pytest.mark.asyncio
async def test_service_status_inactive_unit_reports_verdicts() -> None:
    connector = LinuxSshConnector()
    stdout = "active=inactive\nenabled=disabled\nsub=dead\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout, exit_status=0)
        result = await connector.service_status(_TARGET, {"unit": "nginx"})
    assert result["active"] == "inactive"
    assert result["enabled"] == "disabled"


@pytest.mark.asyncio
async def test_service_status_raises_when_systemctl_absent() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", exit_status=127)
        with pytest.raises(LinuxServiceStatusProbeError):
            await connector.service_status(_TARGET, {"unit": "nginx"})


def test_validate_unit_name_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError):
        validate_unit_name("nginx; reboot")
    with pytest.raises(ValueError):
        validate_unit_name("$(id)")
    assert validate_unit_name("getty@tty1.service") == "getty@tty1.service"


# ---------------------------------------------------------------------------
# sysctl.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sysctl_read_returns_key_value() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="1\n", exit_status=0)
        result = await connector.sysctl_read(_TARGET, {"key": "net.ipv4.ip_forward"})
    assert result == {"key": "net.ipv4.ip_forward", "value": "1"}
    cmd = _cmd_of(mock_cmd)
    assert "sysctl -n" in cmd
    assert shlex.quote("net.ipv4.ip_forward") in cmd


@pytest.mark.asyncio
async def test_sysctl_read_unknown_key_returns_null_value() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", exit_status=255)
        result = await connector.sysctl_read(_TARGET, {"key": "net.ipv4.does_not_exist"})
    assert result == {"key": "net.ipv4.does_not_exist", "value": None}


def test_validate_sysctl_key_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError):
        validate_sysctl_key("net.ipv4.ip_forward; cat /etc/shadow")
    assert validate_sysctl_key("net/ipv4/ip_forward") == "net/ipv4/ip_forward"


# ---------------------------------------------------------------------------
# firewall.show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firewall_show_nftables_backend() -> None:
    connector = LinuxSshConnector()
    stdout = "MEHO_BACKEND=nftables\ntable inet filter {\n  chain input { }\n}\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.firewall_show(_TARGET, {})
    assert result["backend"] == "nftables"
    assert result["total"] == 3
    assert result["rows"][0] == "table inet filter {"


@pytest.mark.asyncio
async def test_firewall_show_none_backend_empty_rows() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="MEHO_BACKEND=none\n")
        result = await connector.firewall_show(_TARGET, {})
    assert result == {"rows": [], "total": 0, "backend": "none"}


def test_parse_firewall_output_iptables() -> None:
    parsed = parse_firewall_output("MEHO_BACKEND=iptables\n*filter\n-A INPUT -j DROP\n")
    assert parsed["backend"] == "iptables"
    assert parsed["rows"] == ["*filter", "-A INPUT -j DROP"]


# ---------------------------------------------------------------------------
# mount.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_list_returns_mount_and_export_rows() -> None:
    connector = LinuxSshConnector()
    stdout = (
        "MEHO_SECTION=mounts\n"
        "/dev/sda1 / ext4 rw,relatime\n"
        "tmpfs /run tmpfs rw,nosuid\n"
        "MEHO_SECTION=exports\n"
        "/srv/nfs 10.0.0.0/24(rw,sync)\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.mount_list(_TARGET, {})
    kinds = [row["kind"] for row in result["rows"]]
    assert kinds == ["mount", "mount", "export"]
    assert result["total"] == 3
    assert result["rows"][0] == {
        "kind": "mount",
        "source": "/dev/sda1",
        "target": "/",
        "fstype": "ext4",
        "options": "rw,relatime",
    }
    assert result["rows"][2] == {
        "kind": "export",
        "path": "/srv/nfs",
        "clients": "10.0.0.0/24(rw,sync)",
    }


def test_parse_mount_line_handles_mount8_format() -> None:
    row = parse_mount_line("/dev/sda1 on / type ext4 (rw,relatime)")
    assert row == {
        "kind": "mount",
        "source": "/dev/sda1",
        "target": "/",
        "fstype": "ext4",
        "options": "rw,relatime",
    }


def test_parse_export_line_skips_showmount_header() -> None:
    assert parse_export_line("Export list for localhost:") is None
    assert parse_export_line("") is None
    assert parse_export_line("/srv/data *") == {
        "kind": "export",
        "path": "/srv/data",
        "clients": "*",
    }


def test_parse_mount_table_sections() -> None:
    rows = parse_mount_table(
        "MEHO_SECTION=mounts\n/dev/sda1 / ext4 rw\nMEHO_SECTION=exports\n/srv x(rw)\n"
    )
    assert [r["kind"] for r in rows] == ["mount", "export"]


# ---------------------------------------------------------------------------
# path confinement (unit)
# ---------------------------------------------------------------------------


def test_ensure_path_under_root_accepts_descendant_rejects_escape() -> None:
    assert ensure_path_under_root("/etc/os-release", "/etc") == "/etc/os-release"
    assert ensure_path_under_root("/etc/../etc/hosts", "/etc") == "/etc/hosts"
    with pytest.raises(PathConfinementError):
        ensure_path_under_root("/etc/../root/.ssh", "/etc")
    # Trailing-slash sentinel: /etc-evil is not under /etc.
    with pytest.raises(PathConfinementError):
        ensure_path_under_root("/etc-evil/x", "/etc")


def test_ensure_path_under_root_rejects_control_bytes() -> None:
    with pytest.raises(PathConfinementError):
        ensure_path_under_root("/etc/x\n/passwd", "/etc")


def test_confine_read_path_tries_every_root() -> None:
    assert confine_read_path("/var/log/syslog") == "/var/log/syslog"
    assert confine_read_path("/proc/1/cmdline") == "/proc/1/cmdline"
    with pytest.raises(PathConfinementError):
        confine_read_path("/home/user/x")
    # A relative path is ambiguous across roots → rejected.
    with pytest.raises(PathConfinementError):
        confine_read_path("os-release")


def test_confine_read_path_roots_are_the_documented_set() -> None:
    assert LINUX_READ_ROOTS == ("/etc", "/var/log", "/var/lib", "/run", "/proc", "/sys")


# ---------------------------------------------------------------------------
# injection safety -- every parameterized command shlex-quotes its value
# ---------------------------------------------------------------------------


def test_build_file_read_command_quotes_metacharacter_path() -> None:
    # A path that is lexically under /var/log but carries shell metacharacters.
    confined = confine_read_path("/var/log/a b;rm -rf /.log")
    cmd = build_file_read_command(confined, 100)
    assert shlex.quote(confined) in cmd
    # The raw metacharacter run never appears unquoted.
    assert " b;rm -rf /.log" not in cmd.replace(shlex.quote(confined), "")


def test_build_log_tail_command_quotes_path() -> None:
    confined = confine_read_path("/var/log/a b.log")
    cmd = build_log_tail_command(confined, 50)
    assert shlex.quote(confined) in cmd


def test_build_service_status_command_quotes_unit() -> None:
    cmd = build_service_status_command("getty@tty1.service")
    assert shlex.quote("getty@tty1.service") in cmd


def test_build_sysctl_read_command_quotes_key() -> None:
    cmd = build_sysctl_read_command("net.ipv4.ip_forward")
    assert shlex.quote("net.ipv4.ip_forward") in cmd


# ---------------------------------------------------------------------------
# normalise_json_rows
# ---------------------------------------------------------------------------


def test_normalise_json_rows_envelope() -> None:
    assert normalise_json_rows(["a", "b"]) == {"rows": ["a", "b"], "total": 2}
    assert normalise_json_rows([]) == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# LINUX_OPS registration shape
# ---------------------------------------------------------------------------


_EXPECTED_OP_IDS: frozenset[str] = frozenset(
    {
        "linux.about",
        "linux.file.read",
        "linux.log.tail",
        "linux.service.status",
        "linux.sysctl.read",
        "linux.firewall.show",
        "linux.mount.list",
    }
)


def test_linux_ops_count_matches_expected() -> None:
    # about + six read verbs = seven.
    assert len(LINUX_OPS) == len(_EXPECTED_OP_IDS)


def test_linux_ops_about_is_first() -> None:
    assert LINUX_OPS[0].op_id == "linux.about"


def test_linux_ops_covers_expected_op_ids() -> None:
    assert {op.op_id for op in LINUX_OPS} == _EXPECTED_OP_IDS


def test_linux_ops_all_namespaced() -> None:
    for op in LINUX_OPS:
        assert op.op_id.startswith("linux."), f"{op.op_id!r} lacks linux. prefix"


def test_linux_ops_all_safe_read_only_no_approval() -> None:
    """AC: every T1 op is safe-tier, read-only, requires no approval."""
    for op in LINUX_OPS:
        assert op.safety_level == "safe", f"{op.op_id!r} is not safe-tier"
        assert op.requires_approval is False, f"{op.op_id!r} requires approval"
        assert "read-only" in op.tags, f"{op.op_id!r} missing read-only tag"


def test_linux_ops_parameter_schemas_closed() -> None:
    for op in LINUX_OPS:
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id


def test_linux_parameterless_ops_declare_no_properties() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    for op_id in ("linux.about", "linux.firewall.show", "linux.mount.list"):
        assert by_id[op_id].parameter_schema.get("properties") == {}, op_id


def test_linux_path_ops_require_path() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    for op_id in ("linux.file.read", "linux.log.tail"):
        schema = by_id[op_id].parameter_schema
        assert schema.get("required") == ["path"], op_id


def test_linux_ops_have_ssh_transport_when_to_use() -> None:
    for op in LINUX_OPS:
        assert op.llm_instructions, f"{op.op_id!r} missing llm_instructions"
        when_to_use = op.llm_instructions.get("when_to_use", "")
        assert when_to_use.strip(), f"{op.op_id!r} empty when_to_use"
        assert "SSH" in when_to_use, f"{op.op_id!r} when_to_use lacks SSH transport note"


def test_linux_ops_handler_attrs_exist_on_connector() -> None:
    for op in LINUX_OPS:
        assert callable(getattr(LinuxSshConnector, op.handler_attr, None)), (
            f"{op.op_id!r}: LinuxSshConnector has no handler {op.handler_attr!r}"
        )


# ---------------------------------------------------------------------------
# secret-hygiene schema guard
# ---------------------------------------------------------------------------


def test_no_op_declares_a_secret_value_parameter() -> None:
    """AC: no op accepts a plaintext secret-value param (the sysctl 'key' is fine)."""
    secret_fields = {
        "password",
        "passwd",
        "secret",
        "credential",
        "token",
        "ssh_private_key",
        "private_key",
        "api_key",
    }
    for op in LINUX_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


# ---------------------------------------------------------------------------
# group coverage + fail-closed registration
# ---------------------------------------------------------------------------


def test_every_declared_group_has_a_curated_when_to_use() -> None:
    declared = {op.group_key for op in LINUX_OPS if op.group_key is not None}
    assert declared == {"system", "file", "log", "service", "firewall", "storage"}
    for key in declared:
        blurb = LINUX_WHEN_TO_USE_BY_GROUP.get(key)
        assert blurb and blurb.strip(), f"group {key!r} has no curated when_to_use"
        assert "Operations grouped under" not in blurb, key
        assert "SSH" in blurb, key


@pytest.mark.asyncio
async def test_register_operations_fails_closed_on_missing_group_blurb() -> None:
    """AC: registration raises ValueError if a declared group_key has no blurb."""
    incomplete = dict(LINUX_WHEN_TO_USE_BY_GROUP)
    del incomplete["storage"]
    with (
        patch(
            "meho_backplane.connectors.linux.connector.LINUX_WHEN_TO_USE_BY_GROUP",
            incomplete,
        ),
        patch(
            "meho_backplane.operations.typed_register.register_typed_operation",
            new_callable=AsyncMock,
        ),
        pytest.raises(ValueError, match="storage"),
    ):
        await LinuxSshConnector.register_operations()


# ---------------------------------------------------------------------------
# registry triple + wildcard
# ---------------------------------------------------------------------------


def test_linux_connector_registry_triple_and_wildcard() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry.get(("linux", "1.x", "linux-ssh")) is LinuxSshConnector
    assert registry.get(("linux", "", "")) is LinuxSshConnector


def test_linux_connector_id_round_trips() -> None:
    """The separator-free product token round-trips through parse_connector_id."""
    from meho_backplane.operations._lookup import parse_connector_id

    product, version, impl_id = parse_connector_id("linux-ssh-1.x")
    assert product == "linux"
    assert impl_id == "linux-ssh"
    assert version == "1.x"


# ---------------------------------------------------------------------------
# transport -- subclass discipline
# ---------------------------------------------------------------------------


def test_linux_connector_subclasses_ssh_base_and_inherits_transport() -> None:
    assert issubclass(LinuxSshConnector, SshConnector)
    # The connector overrides only fingerprint/probe/execute (+about/handlers),
    # never the shared auth / transport / pool seams.
    for inherited in ("_auth_config", "_run_command", "_connect", "aclose"):
        assert getattr(LinuxSshConnector, inherited) is getattr(SshConnector, inherited), (
            f"{inherited} must not be overridden"
        )


# ---------------------------------------------------------------------------
# JSONFlux -- the {rows, total} envelope is a detected collection
# ---------------------------------------------------------------------------


def test_rows_total_envelope_is_a_detected_collection() -> None:
    payload = {"rows": ["a", "b", "c"], "total": 3}
    envelope_key, rows = _detect_collection(payload)
    assert envelope_key == "rows"
    assert rows == ["a", "b", "c"]


def test_rows_total_envelope_spills_over_threshold() -> None:
    reducer = JsonFluxReducer()
    big = {"rows": [f"line-{i}" for i in range(60)], "total": 60}
    small = {"rows": ["a", "b"], "total": 2}
    _, big_rows = _detect_collection(big)
    _, small_rows = _detect_collection(small)
    assert reducer._over_threshold(big_rows, big) is True
    assert reducer._over_threshold(small_rows, small) is False

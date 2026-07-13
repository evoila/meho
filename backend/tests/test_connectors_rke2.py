# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the RKE2 node connector scaffold + posture tier (#2221).

Coverage matrix (per Task #2221 acceptance criteria):

* :func:`parse_rke2_version` / :func:`parse_os_pretty_name` -- identity
  parsing from ``rke2 --version`` + ``/etc/os-release``.
* :func:`parse_stat_output` / :func:`parse_posture` -- the posture
  envelope: config-file modes + owner/group, missing paths reported
  ``present: false``, and the redacted token entry.
* Bound-method shims on :class:`Rke2SshConnector` -- ``about`` (identity)
  and ``posture_show`` (posture) run the correct plain-SSH commands and
  return the expected envelope shape.
* Redaction invariant -- ``posture_show`` issues a single ``stat`` (never
  a ``cat`` of the token path); the token entry carries ``redacted: true``
  and no secret material bleeds into the result envelope or logs.
* ``RKE2_OPS`` registration shape -- 3 ops, all ``safety_level='safe'``
  / ``requires_approval=false``, ``additionalProperties=False`` on every
  parameter schema, non-empty SSH-transport ``when_to_use``, and ``rke2.``
  namespace op_ids with handler methods on the class. The two read-tier
  ops (``rke2.about`` / ``rke2.posture.show``) take no params; the safe
  non-gated ``rke2.etcd-snapshot.save`` (T4 #2431) takes an optional,
  charset-bounded ``name``.
* ``rke2.etcd-snapshot.save`` handler -- name charset re-check
  (fail-closed), the embedded-etcd-server precondition guard, and a
  bounded-name save parsed from the RKE2 ``Snapshot <name> saved.`` log.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.rke2  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.rke2 import RKE2_OPS, Rke2SshConnector
from meho_backplane.connectors.rke2.connector import (
    parse_os_pretty_name,
    parse_rke2_version,
)
from meho_backplane.connectors.rke2.ops_read import (
    POSTURE_CONFIG_PATHS,
    RKE2_TOKEN_PATH,
    parse_posture,
    parse_stat_output,
)
from meho_backplane.connectors.rke2.ops_snapshot import (
    SNAPSHOT_DEFAULT_DIR,
    Rke2SnapshotNameError,
    Rke2SnapshotPreconditionError,
    parse_saved_snapshot_name,
)
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


_CANARY_PASSWORD = "rke2-canary-pw-xyz-771"  # gitleaks:allow NOSONAR -- synthetic canary
# Synthetic key-shaped canary that does not trip the detect-private-key
# hook (the regex keys on the literal ``BEGIN ... PRIVATE KEY`` opener).
_CANARY_SSH_KEY = "RKE2-CANARY-KEY-MARKER-QWER5678ZX"  # gitleaks:allow -- synthetic canary
# A token *value* canary. The posture tier must NEVER read the token
# content, so this string must never surface anywhere.
_CANARY_TOKEN_VALUE = "K10rke2canarytokenvalueDONOTLEAK::server:abc123"  # gitleaks:allow NOSONAR


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str  # a Vault KV-v2 path STRING (#2155)


_TARGET_SECRET_PATH = "meho/testing/rke2/node-test"

_TARGET = _StubTarget(
    name="rke2-node-test",
    host="rke2-node.test.invalid",
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


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


# ---------------------------------------------------------------------------
# parse_rke2_version / parse_os_pretty_name
# ---------------------------------------------------------------------------


def test_parse_rke2_version_extracts_release_string() -> None:
    banner = "rke2 version v1.28.5+rke2r1 (abc1234)\ngo version go1.21.6\n"
    assert parse_rke2_version(banner) == "v1.28.5+rke2r1"


def test_parse_rke2_version_absent_returns_none() -> None:
    assert parse_rke2_version("") is None
    assert parse_rke2_version("command not found\n") is None


def test_parse_os_pretty_name_quoted() -> None:
    content = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\nID=ubuntu\n'
    assert parse_os_pretty_name(content) == "Ubuntu 22.04.3 LTS"


def test_parse_os_pretty_name_unquoted_and_absent() -> None:
    assert parse_os_pretty_name("PRETTY_NAME=Fedora Linux 39\n") == "Fedora Linux 39"
    assert parse_os_pretty_name("NAME=Ubuntu\n") is None
    assert parse_os_pretty_name("") is None


# ---------------------------------------------------------------------------
# parse_stat_output
# ---------------------------------------------------------------------------


def test_parse_stat_output_parses_and_normalises_mode() -> None:
    stdout = (
        "/etc/rancher/rke2/config.yaml|600|root|root\n/etc/rancher/rke2/rke2.yaml|644|root|rke2\n"
    )
    parsed = parse_stat_output(stdout)
    assert parsed["/etc/rancher/rke2/config.yaml"] == {
        "mode": "0600",
        "owner": "root",
        "group": "root",
    }
    # 3-digit mode left-padded to the canonical 4-digit octal form.
    assert parsed["/etc/rancher/rke2/rke2.yaml"]["mode"] == "0644"


def test_parse_stat_output_skips_malformed_lines() -> None:
    stdout = "garbage banner line\n/etc/rancher/rke2/config.yaml|600|root|root\n\n"
    parsed = parse_stat_output(stdout)
    assert set(parsed) == {"/etc/rancher/rke2/config.yaml"}


# ---------------------------------------------------------------------------
# parse_posture
# ---------------------------------------------------------------------------


def test_parse_posture_present_and_redacted_token() -> None:
    stat_map = {
        "/etc/rancher/rke2/config.yaml": {"mode": "0600", "owner": "root", "group": "root"},
        "/etc/rancher/rke2/rke2.yaml": {"mode": "0600", "owner": "root", "group": "root"},
        RKE2_TOKEN_PATH: {"mode": "0600", "owner": "root", "group": "root"},
    }
    posture = parse_posture(stat_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cfg = {c["path"]: c for c in posture["config_files"]}
    assert cfg["/etc/rancher/rke2/config.yaml"]["present"] is True
    assert cfg["/etc/rancher/rke2/config.yaml"]["mode"] == "0600"
    # Token entry is present, carries its mode, and is explicitly redacted.
    token = posture["token"]
    assert token["path"] == RKE2_TOKEN_PATH
    assert token["present"] is True
    assert token["mode"] == "0600"
    assert token["redacted"] is True
    # No token VALUE field exists anywhere in the envelope.
    assert "value" not in token
    assert "token" not in {k for c in posture["config_files"] for k in c}


def test_parse_posture_missing_paths_report_absent() -> None:
    # Agent node: no server token, config.yaml only.
    stat_map = {
        "/etc/rancher/rke2/config.yaml": {"mode": "0600", "owner": "root", "group": "root"},
    }
    posture = parse_posture(stat_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cfg = {c["path"]: c for c in posture["config_files"]}
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["present"] is False
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["mode"] is None
    token = posture["token"]
    assert token["present"] is False
    assert token["mode"] is None
    assert token["redacted"] is True


# ---------------------------------------------------------------------------
# about shim (identity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_returns_identity_snapshot() -> None:
    connector = Rke2SshConnector()
    # fingerprint order: os-release, then rke2 --version.
    sequence = [
        _proc(stdout='PRETTY_NAME="Ubuntu 22.04.3 LTS"\n'),
        _proc(stdout="rke2 version v1.29.3+rke2r1 (deadbee)\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "rancher"
    assert result["product"] == "rke2"
    assert result["version"] == "v1.29.3+rke2r1"
    assert result["node_os"] == "Ubuntu 22.04.3 LTS"
    issued = [call.args[1] for call in mock_cmd.await_args_list]
    assert issued[0] == "cat /etc/os-release"
    assert "rke2 --version" in issued[1]


@pytest.mark.asyncio
async def test_about_version_none_when_binary_absent() -> None:
    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout='PRETTY_NAME="RHEL 9.3"\n'),
        _proc(stdout=""),  # `|| true` swallows a missing rke2 binary
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.about(_TARGET, {})
    assert result["version"] is None
    assert result["node_os"] == "RHEL 9.3"


@pytest.mark.asyncio
async def test_about_unreachable_raises_connector_error() -> None:
    """An unreachable node maps to ConnectorUnreachableError, not a hollow ok."""
    from meho_backplane.connectors.adapters.ssh import ConnectorUnreachableError

    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = OSError("connection refused")
        with pytest.raises(ConnectorUnreachableError):
            await connector.about(_TARGET, {})


# ---------------------------------------------------------------------------
# posture_show shim (posture tier) + redaction invariant
# ---------------------------------------------------------------------------


_STAT_STDOUT_FULL = (
    "/etc/rancher/rke2/config.yaml|600|root|root\n"
    "/etc/rancher/rke2/rke2.yaml|600|root|root\n"
    "/var/lib/rancher/rke2/server/token|600|root|root\n"
)


@pytest.mark.asyncio
async def test_posture_show_returns_redacted_envelope() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_STAT_STDOUT_FULL)
        result = await connector.posture_show(_TARGET, {})
    # Single SSH round-trip; the command is a `stat`, never a `cat`.
    mock_cmd.assert_awaited_once()
    cmd = mock_cmd.await_args.args[1]
    assert cmd.startswith("stat -c '%n|%a|%U|%G' --")
    assert "cat" not in cmd
    # Every measured path appears as a stat argument.
    for path in (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH):
        assert path in cmd
    # Envelope shape + redaction.
    assert result["token"]["present"] is True
    assert result["token"]["mode"] == "0600"
    assert result["token"]["redacted"] is True
    cfg = {c["path"]: c for c in result["config_files"]}
    assert cfg["/etc/rancher/rke2/config.yaml"]["mode"] == "0600"


@pytest.mark.asyncio
async def test_posture_show_reports_missing_token_as_absent() -> None:
    connector = Rke2SshConnector()
    # Agent node: config.yaml only in stat stdout (missing paths omitted).
    stdout = "/etc/rancher/rke2/config.yaml|640|root|root\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.posture_show(_TARGET, {})
    assert result["token"]["present"] is False
    assert result["token"]["redacted"] is True
    cfg = {c["path"]: c for c in result["config_files"]}
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["present"] is False


@pytest.mark.asyncio
async def test_posture_show_propagates_ssh_failure() -> None:
    """A transport failure escapes so the dispatcher reports connector_error."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = OSError("connection refused")
        with pytest.raises(OSError, match="connection refused"):
            await connector.posture_show(_TARGET, {})


@pytest.mark.asyncio
async def test_posture_show_never_leaks_secret_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The posture envelope + logs carry no credential or token-value material."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_STAT_STDOUT_FULL)
        with caplog.at_level("DEBUG"):
            result = await connector.posture_show(_TARGET, {})
    rendered = repr(result)
    for canary in (_CANARY_PASSWORD, _CANARY_SSH_KEY, _CANARY_TOKEN_VALUE):
        assert canary not in rendered
        assert canary not in caplog.text


# ---------------------------------------------------------------------------
# RKE2_OPS registration shape
# ---------------------------------------------------------------------------


_READ_OP_IDS: frozenset[str] = frozenset({"rke2.about", "rke2.posture.show"})
_EXPECTED_OP_IDS: frozenset[str] = _READ_OP_IDS | {"rke2.etcd-snapshot.save"}


def test_rke2_ops_has_three_entries() -> None:
    assert len(RKE2_OPS) == 3


def test_rke2_ops_about_is_first() -> None:
    assert RKE2_OPS[0].op_id == "rke2.about"


def test_rke2_ops_covers_expected_op_ids() -> None:
    assert {op.op_id for op in RKE2_OPS} == _EXPECTED_OP_IDS


def test_rke2_ops_all_namespaced() -> None:
    for op in RKE2_OPS:
        assert op.op_id.startswith("rke2."), f"{op.op_id!r} lacks rke2. prefix"


def test_rke2_ops_all_safe_no_approval() -> None:
    """AC: every op is safe-tier and requires no approval (incl. the snapshot op)."""
    for op in RKE2_OPS:
        assert op.safety_level == "safe", f"{op.op_id!r} is not safe-tier"
        assert op.requires_approval is False, f"{op.op_id!r} requires approval"


def test_rke2_read_ops_tagged_read_only() -> None:
    """The read-tier ops carry the read-only tag; the snapshot op does not."""
    by_id = {op.op_id: op for op in RKE2_OPS}
    for op_id in _READ_OP_IDS:
        assert "read-only" in by_id[op_id].tags, f"{op_id!r} missing read-only tag"
    # The snapshot op is active (copies etcd to disk), so it is NOT read-only.
    assert "read-only" not in by_id["rke2.etcd-snapshot.save"].tags


def test_rke2_ops_parameter_schemas_closed() -> None:
    by_id = {op.op_id: op for op in RKE2_OPS}
    for op in RKE2_OPS:
        assert op.parameter_schema.get("additionalProperties") is False
    # The read tier takes no operator parameters -- fixed paths only.
    for op_id in _READ_OP_IDS:
        assert by_id[op_id].parameter_schema.get("properties") == {}
    # The snapshot op exposes exactly one optional, charset-bounded param.
    save_schema = by_id["rke2.etcd-snapshot.save"].parameter_schema
    props = save_schema.get("properties", {})
    assert set(props) == {"name"}
    assert props["name"].get("pattern") == r"^[A-Za-z0-9._-]+$"
    assert save_schema.get("required", []) == []  # name is optional


def test_rke2_ops_have_ssh_transport_when_to_use() -> None:
    for op in RKE2_OPS:
        assert op.llm_instructions, f"{op.op_id!r} missing llm_instructions"
        when_to_use = op.llm_instructions.get("when_to_use", "")
        assert when_to_use.strip(), f"{op.op_id!r} empty when_to_use"
        assert "SSH" in when_to_use, f"{op.op_id!r} when_to_use lacks SSH transport note"


def test_rke2_ops_handler_attrs_exist_on_connector() -> None:
    for op in RKE2_OPS:
        assert callable(getattr(Rke2SshConnector, op.handler_attr, None)), (
            f"{op.op_id!r}: Rke2SshConnector has no handler {op.handler_attr!r}"
        )


def test_rke2_connector_registry_triple() -> None:
    """The v2 registry advertises this class under (rke2, 1.x, rke2-ssh)."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry.get(("rke2", "1.x", "rke2-ssh")) is Rke2SshConnector


# ---------------------------------------------------------------------------
# etcd-snapshot.save handler (T4 #2431)
# ---------------------------------------------------------------------------


_SAVE_LOG_OK = "INFO[0000] Snapshot on-demand-rke2-node-1754907117 saved.\n"


def test_parse_saved_snapshot_name_variants() -> None:
    assert parse_saved_snapshot_name(_SAVE_LOG_OK) == "on-demand-rke2-node-1754907117"
    assert parse_saved_snapshot_name("Snapshot my-snap-42 saved.") == "my-snap-42"
    assert parse_saved_snapshot_name("nothing here") is None
    assert parse_saved_snapshot_name("") is None


@pytest.mark.asyncio
async def test_etcd_snapshot_save_success_default_name() -> None:
    connector = Rke2SshConnector()
    # side_effect order: precondition guard, then the save command.
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stderr=_SAVE_LOG_OK, exit_status=0),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.etcd_snapshot_save(_TARGET, {})
    assert result["snapshot_name"] == "on-demand-rke2-node-1754907117"
    assert result["path"] == f"{SNAPSHOT_DEFAULT_DIR}/on-demand-rke2-node-1754907117"
    assert result["exit_status"] == 0
    issued = [call.args[1] for call in mock_cmd.await_args_list]
    # Guard runs first under sudo -n; then the save under sudo -n by
    # absolute binary path, with no --name flag when none was supplied.
    assert issued[0].startswith("sudo -n -- sh -c ")
    assert "datastore-endpoint" in issued[0]
    assert issued[1] == ("sudo -n -- /var/lib/rancher/rke2/bin/rke2 etcd-snapshot save")


@pytest.mark.asyncio
async def test_etcd_snapshot_save_bounded_name_quoted_in_argv() -> None:
    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stderr="INFO[0000] Snapshot pre_up-grade.1-node-9 saved.\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.etcd_snapshot_save(_TARGET, {"name": "pre_up-grade.1"})
    save_cmd = mock_cmd.await_args_list[1].args[1]
    assert save_cmd == (
        "sudo -n -- /var/lib/rancher/rke2/bin/rke2 etcd-snapshot save --name pre_up-grade.1"
    )
    assert result["snapshot_name"] == "pre_up-grade.1-node-9"


@pytest.mark.asyncio
async def test_etcd_snapshot_save_rejects_bad_name_before_any_command() -> None:
    """A name outside ^[A-Za-z0-9._-]+$ fails closed before any SSH command."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        for bad in ("../etc", "has space", "semi;colon", "$(inject)", ""):
            with pytest.raises(Rke2SnapshotNameError):
                await connector.etcd_snapshot_save(_TARGET, {"name": bad})
        mock_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_etcd_snapshot_save_refuses_external_datastore() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="external-datastore\n")
        with pytest.raises(Rke2SnapshotPreconditionError, match="external datastore"):
            await connector.etcd_snapshot_save(_TARGET, {})
    # Guard ran; the save command never did (single await).
    mock_cmd.assert_awaited_once()


@pytest.mark.asyncio
async def test_etcd_snapshot_save_refuses_non_server_node() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="no-embedded-etcd\n")
        with pytest.raises(Rke2SnapshotPreconditionError, match="embedded-etcd server"):
            await connector.etcd_snapshot_save(_TARGET, {})
    mock_cmd.assert_awaited_once()


@pytest.mark.asyncio
async def test_etcd_snapshot_save_nonzero_exit_raises() -> None:
    from meho_backplane.connectors.rke2.ops_snapshot import Rke2SnapshotError

    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stderr="FATA[0000] etcd is not running\n", exit_status=1),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        with pytest.raises(Rke2SnapshotError, match="exited 1"):
            await connector.etcd_snapshot_save(_TARGET, {})


@pytest.mark.asyncio
async def test_etcd_snapshot_save_never_leaks_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No SSH/sudo credential material appears in the result or logs."""
    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stderr=_SAVE_LOG_OK, exit_status=0),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        with caplog.at_level("DEBUG"):
            result = await connector.etcd_snapshot_save(_TARGET, {})
    rendered = repr(result)
    for canary in (_CANARY_PASSWORD, _CANARY_SSH_KEY, _CANARY_TOKEN_VALUE):
        assert canary not in rendered
        assert canary not in caplog.text

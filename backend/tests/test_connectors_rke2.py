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
* ``RKE2_OPS`` registration shape -- 2 ops, both ``safety_level='safe'``
  / ``requires_approval=false`` / read-only, ``additionalProperties=False``
  on the parameter schema, non-empty SSH-transport ``when_to_use``, and
  ``rke2.`` namespace op_ids with handler methods on the class.
"""

from __future__ import annotations

import uuid
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
_EXPECTED_OP_IDS: frozenset[str] = _READ_OP_IDS | {"rke2.token.rotate"}


def test_rke2_ops_has_three_entries() -> None:
    # Two read ops (#2221) + the approval-gated rke2.token.rotate write (#2429).
    assert len(RKE2_OPS) == 3


def test_rke2_ops_about_is_first() -> None:
    assert RKE2_OPS[0].op_id == "rke2.about"


def test_rke2_ops_covers_expected_op_ids() -> None:
    assert {op.op_id for op in RKE2_OPS} == _EXPECTED_OP_IDS


def test_rke2_ops_all_namespaced() -> None:
    for op in RKE2_OPS:
        assert op.op_id.startswith("rke2."), f"{op.op_id!r} lacks rke2. prefix"


def test_rke2_read_ops_all_safe_read_only_no_approval() -> None:
    """AC: every READ op is safe-tier, read-only, and requires no approval."""
    read_ops = [op for op in RKE2_OPS if op.op_id in _READ_OP_IDS]
    assert {op.op_id for op in read_ops} == _READ_OP_IDS
    for op in read_ops:
        assert op.safety_level == "safe", f"{op.op_id!r} is not safe-tier"
        assert op.requires_approval is False, f"{op.op_id!r} requires approval"
        assert "read-only" in op.tags, f"{op.op_id!r} missing read-only tag"


def test_rke2_ops_parameter_schemas_closed() -> None:
    for op in RKE2_OPS:
        assert op.parameter_schema.get("additionalProperties") is False
        # The read tier takes no operator parameters -- fixed paths only.
        assert op.parameter_schema.get("properties") == {}


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


# ===========================================================================
# rke2.token.rotate write op (#2429)
# ===========================================================================

import contextlib  # noqa: E402 -- grouped with the write-op section it serves

from meho_backplane.auth.operator import Operator, TenantRole  # noqa: E402
from meho_backplane.connectors.rke2.ops_write import (  # noqa: E402
    WRITE_OPS,
    parse_rke2_release,
    rke2_token_rotate,
    rke2_version_rotate_verdict,
)

# A minted-token canary. The handler must NEVER surface the minted token in
# its result envelope (the raw result is persisted on the audit row).
_CANARY_NEW_TOKEN = "K10CANARYnewtokenvalueMUSTNOTLEAK0000deadbeef"  # gitleaks:allow NOSONAR

_OP_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000024f9")
_OPERATOR = Operator(
    sub="rke2-token-rotate-test",
    name="RKE2 Rotate Test",
    email=None,
    raw_jwt="<rke2-rotate-raw-jwt>",
    tenant_id=_OP_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)


def _preflight_stdout(
    *,
    unit: str = "rke2-server.service",
    active: str = "active",
    version: str = "rke2 version v1.28.5+rke2r2 (abc)",
) -> str:
    return f"ACTIVE={active}\nUNIT={unit}\nVERSION={version}\n"


@contextlib.contextmanager
def _patch_vault_write(version: int = 7, raises: bool = False):
    """Patch vault_client_for_operator to a fake async-CM KV-v2 writer.

    Yields the create_or_update_secret mock so a test can inspect what the
    handler wrote (the minted token lands in Vault, not in the result).
    """
    write_mock = MagicMock(return_value={"data": {"version": version}})
    if raises:
        write_mock.side_effect = RuntimeError("vault down")
    client = MagicMock()
    client.secrets.kv.v2.create_or_update_secret = write_mock

    @contextlib.asynccontextmanager
    async def _fake_client(operator: Any):
        yield client

    with patch("meho_backplane.auth.vault.vault_client_for_operator", _fake_client):
        yield write_mock


def _write_op() -> Any:
    return next(op for op in WRITE_OPS if op.op_id == "rke2.token.rotate")


# --- Registration shape ----------------------------------------------------


def test_token_rotate_is_dangerous_and_requires_approval() -> None:
    op = _write_op()
    assert op.safety_level == "dangerous"
    assert op.requires_approval is True
    assert "write" in op.tags


def test_token_rotate_schema_has_no_token_param() -> None:
    """AC: the schema takes no token field and is closed (no free-form input)."""
    schema = _write_op().parameter_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {}


def test_token_rotate_when_to_use_mentions_ssh_and_approval() -> None:
    instr = _write_op().llm_instructions or {}
    when = instr.get("when_to_use", "")
    assert "SSH" in when
    assert "approval-gated" in when


# --- Version fingerprint gate (pure) ---------------------------------------


@pytest.mark.parametrize(
    "version",
    ["v1.28.3+rke2r2", "v1.25.15+rke2r2", "v1.27.10+rke2r2", "v1.29.0+rke2r1", "1.30.2+rke2r1"],
)
def test_version_verdict_accepts_patched(version: str) -> None:
    ok, _ = rke2_version_rotate_verdict(version)
    assert ok is True


@pytest.mark.parametrize(
    "version",
    ["v1.28.2+rke2r2", "v1.27.10+rke2r1", "v1.24.17+rke2r1", "v1.28.3+rke2r1", "junk", "v1.28.3"],
)
def test_version_verdict_refuses_below_floor_and_known_bad(version: str) -> None:
    ok, reason = rke2_version_rotate_verdict(version)
    assert ok is False
    assert reason


def test_parse_rke2_release_shapes() -> None:
    assert parse_rke2_release("v1.28.3+rke2r2") == ((1, 28, 3), 2)
    assert parse_rke2_release("1.27.10+rke2r1") == ((1, 27, 10), 1)
    assert parse_rke2_release("v1.28.3") is None
    assert parse_rke2_release(None) is None


# --- Handler: happy path, no token leak ------------------------------------


@pytest.mark.asyncio
async def test_token_rotate_happy_path_returns_pointer_never_token() -> None:
    connector = Rke2SshConnector()
    sudo_proc = _proc(stdout="", exit_status=0)
    with (
        patch.object(connector, "_resolve_secret", new_callable=AsyncMock) as mock_secret,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        patch(
            "meho_backplane.connectors.rke2.ops_write.run_remote_bash_with_sudo",
            new_callable=AsyncMock,
        ) as mock_sudo,
        patch("secrets.token_hex", return_value=_CANARY_NEW_TOKEN),
        _patch_vault_write(version=9) as write_mock,
    ):
        mock_secret.return_value = {"password": _CANARY_PASSWORD}
        mock_cmd.return_value = _proc(stdout=_preflight_stdout())
        mock_sudo.return_value = sudo_proc
        result = await rke2_token_rotate(connector, _TARGET, {}, _OPERATOR)

    assert result["rotated"] is True
    assert result["exit_status"] == 0
    ref = result["token_ref"]
    assert ref["backend"] == "vault"
    assert ref["kv_version"] == 9
    assert f"tenants/{_OP_TENANT}/rke2/" in ref["path"]
    # THE audit rule: the minted token never appears in the returned result.
    assert _CANARY_NEW_TOKEN not in repr(result)
    # ...but it WAS written to Vault (the sink) and passed to the sudo script.
    write_mock.assert_called_once()
    assert write_mock.call_args.kwargs["secret"] == {"token": _CANARY_NEW_TOKEN}
    script = mock_sudo.await_args.args[2]
    assert "/var/lib/rancher/rke2/bin/rke2 token rotate" in script
    assert 'OLD=$(cat "$TOKENFILE")' in script  # OLD read server-side, never in Python
    assert _CANARY_NEW_TOKEN in script  # new token quoted into the script body only


@pytest.mark.asyncio
async def test_token_rotate_vault_write_failure_is_honest_no_token() -> None:
    connector = Rke2SshConnector()
    with (
        patch.object(connector, "_resolve_secret", new_callable=AsyncMock) as mock_secret,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        patch(
            "meho_backplane.connectors.rke2.ops_write.run_remote_bash_with_sudo",
            new_callable=AsyncMock,
        ) as mock_sudo,
        patch("secrets.token_hex", return_value=_CANARY_NEW_TOKEN),
        _patch_vault_write(raises=True),
    ):
        mock_secret.return_value = {"password": _CANARY_PASSWORD}
        mock_cmd.return_value = _proc(stdout=_preflight_stdout())
        mock_sudo.return_value = _proc(exit_status=0)
        result = await rke2_token_rotate(connector, _TARGET, {}, _OPERATOR)

    assert result["rotated"] is True  # the cluster token DID rotate
    assert result["token_ref"] is None
    assert result["vault_error"] == "RuntimeError"
    assert _CANARY_NEW_TOKEN not in repr(result)


# --- Handler: fingerprint-gate refusals (reject before any rotate) ---------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preflight", "expected_gate"),
    [
        (_preflight_stdout(unit=""), "role"),  # not a server node
        (_preflight_stdout(active="inactive"), "service"),  # rke2-server down
        (_preflight_stdout(version="rke2 version v1.28.2+rke2r2 (x)"), "version"),  # below floor
    ],
)
async def test_token_rotate_gate_refuses_before_rotate(preflight: str, expected_gate: str) -> None:
    connector = Rke2SshConnector()
    with (
        patch.object(connector, "_resolve_secret", new_callable=AsyncMock) as mock_secret,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        patch(
            "meho_backplane.connectors.rke2.ops_write.run_remote_bash_with_sudo",
            new_callable=AsyncMock,
        ) as mock_sudo,
    ):
        mock_secret.return_value = {"password": _CANARY_PASSWORD}
        mock_cmd.return_value = _proc(stdout=preflight)
        result = await rke2_token_rotate(connector, _TARGET, {}, _OPERATOR)
        mock_sudo.assert_not_awaited()  # no mutation on a gate refusal
    assert result["rotated"] is False
    assert result["gate"] == expected_gate


@pytest.mark.asyncio
async def test_token_rotate_without_operator_fails_closed() -> None:
    """No operator => no Vault sink => refuse before touching anything."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await rke2_token_rotate(connector, _TARGET, {}, None)
        mock_cmd.assert_not_awaited()
    assert result["rotated"] is False
    assert result["gate"] == "operator"


@pytest.mark.asyncio
async def test_token_rotate_missing_sudo_credential_refuses() -> None:
    connector = Rke2SshConnector()
    with (
        patch.object(connector, "_resolve_secret", new_callable=AsyncMock) as mock_secret,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        mock_secret.return_value = {"username": "root"}  # no password / sudo_password
        result = await rke2_token_rotate(connector, _TARGET, {}, _OPERATOR)
        mock_cmd.assert_not_awaited()
    assert result["rotated"] is False
    assert result["gate"] == "credentials"


@pytest.mark.asyncio
async def test_token_rotate_nonzero_exit_no_vault_no_output() -> None:
    connector = Rke2SshConnector()
    with (
        patch.object(connector, "_resolve_secret", new_callable=AsyncMock) as mock_secret,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        patch(
            "meho_backplane.connectors.rke2.ops_write.run_remote_bash_with_sudo",
            new_callable=AsyncMock,
        ) as mock_sudo,
        patch("secrets.token_hex", return_value=_CANARY_NEW_TOKEN),
        _patch_vault_write() as write_mock,
    ):
        mock_secret.return_value = {"password": _CANARY_PASSWORD}
        mock_cmd.return_value = _proc(stdout=_preflight_stdout())
        mock_sudo.return_value = _proc(stdout="boom", stderr="rke2: rotate failed", exit_status=1)
        result = await rke2_token_rotate(connector, _TARGET, {}, _OPERATOR)

    assert result["rotated"] is False
    assert result["gate"] == "rotate"
    assert result["exit_status"] == 1
    write_mock.assert_not_called()  # no Vault write on a failed rotate
    # Never surface raw stdout/stderr (could echo a token); only structured fields.
    assert "stderr" not in result
    assert "stdout" not in result
    assert _CANARY_NEW_TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_token_rotate_shim_delegates_to_handler() -> None:
    """The connector bound-method shim runs the same guarded path."""
    connector = Rke2SshConnector()
    result = await connector.token_rotate(_TARGET, {}, None)  # operator=None short-circuits
    assert result["rotated"] is False
    assert result["gate"] == "operator"

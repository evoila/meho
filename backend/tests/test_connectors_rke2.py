# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the RKE2 node connector scaffold + posture tier (#2221).

Coverage matrix (per Task #2221 acceptance criteria):

* :func:`parse_rke2_version` / :func:`parse_os_pretty_name` -- identity
  parsing from ``rke2 --version`` + ``/etc/os-release``.
* :func:`parse_posture_probe_output` / :func:`parse_posture` -- the posture
  envelope: config-file modes + owner/group, the three-state
  present/absent/unknown verdict, and the redacted token entry.
* Posture tri-state (#2698) -- a path whose parent the SSH user cannot
  traverse reports ``present: null`` / ``status: unknown``, never a
  confident ``present: false``; a genuinely missing file still reports
  ``absent``; and a probe that could not run at all raises
  :class:`Rke2PostureProbeError` instead of being served as posture.
* Bound-method shims on :class:`Rke2SshConnector` -- ``about`` (identity)
  and ``posture_show`` (posture) run the correct plain-SSH commands and
  return the expected envelope shape.
* Redaction invariant -- ``posture_show`` issues a single ``stat``-based
  probe (never a ``cat`` of the token path); the token entry carries
  ``redacted: true`` and no secret material bleeds into the result
  envelope or logs.
* :func:`build_service_status_command` / :func:`parse_service_status` -- the
  service-state read (``rke2.node.service.status``, #2833 / #2852): the
  read-only ``systemctl show`` probe over the fixed ``rke2-server`` /
  ``rke2-agent`` pair, and the ``UNIT=`` / ``KEY=VALUE`` parser that reports
  each unit's load / active / sub state, start time, and restart count.
  ``LoadState=not-found`` nulls the live-state fields; a non-zero
  ``NRestarts`` surfaces as the crash-loop signal; the probe raises
  :class:`Rke2ServiceStatusProbeError` when ``systemctl`` is absent.
* ``RKE2_OPS`` registration shape -- 9 ops: four read (``rke2.about`` /
  ``rke2.posture.show``, T1 #2221; ``rke2.node.service.status``, #2852;
  ``rke2.node.config.get``, the redacted config-content read #2854), three
  approval-gated write (``rke2.token.rotate`` T2 #2429,
  ``rke2.node.service.restart`` / ``rke2.node.config.update`` T3 #2430), and
  two safe non-gated snapshot (``rke2.etcd-snapshot.save`` T4 #2431,
  ``rke2.etcd-snapshot.list`` #2853). Read ops are safe / read-only /
  no-approval; ``rke2.about`` / ``rke2.posture.show`` /
  ``rke2.node.service.status`` take no params while ``rke2.node.config.get``
  takes an optional path-bounded ``path``; write ops are dangerous /
  approval-gated; ``.save`` is safe / no-approval but active (neither
  read-only nor write) and takes an optional charset-bounded ``name``;
  ``.list`` is safe / no-approval / read-only and takes no params. Every op
  has ``additionalProperties=False`` on its parameter schema, a non-empty
  SSH-transport ``when_to_use``, and a ``rke2.`` op_id with a handler method
  on the class.
* ``rke2.node.config.get`` handler (#2854) -- reuses the write op's
  ``bound_config_path`` confinement (traversal rejected before any SSH), the
  ``cat`` + ``yaml.safe_load`` read step, and redacts the join tokens + etcd
  S3 credentials (``redact_config_content``) so no secret VALUE reaches the
  result envelope.
* ``rke2.etcd-snapshot.save`` handler -- name charset re-check
  (fail-closed), the shared embedded-etcd-server precondition guard, and a
  bounded-name save parsed from the RKE2 ``Snapshot <name> saved.`` log.
* ``rke2.etcd-snapshot.list`` handler -- the *same* shared precondition
  guard, and a version-drift-resilient parse of the RKE2 ``Name / Location
  / Size / Created`` table into ``{snapshots: [...]}`` rows.
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
    REDACTED_SENTINEL,
    RKE2_TOKEN_PATH,
    SECRET_CONFIG_KEYS,
    SERVICE_STATUS_PROPERTIES,
    SERVICE_UNITS,
    STATUS_ABSENT,
    STATUS_PRESENT,
    STATUS_UNKNOWN,
    Rke2PostureProbeError,
    Rke2ServiceStatusProbeError,
    bound_read_config_path,
    build_posture_probe_command,
    build_service_status_command,
    parse_posture,
    parse_posture_probe_output,
    parse_service_status,
    redact_config_content,
)
from meho_backplane.connectors.rke2.ops_snapshot import (
    SNAPSHOT_DEFAULT_DIR,
    Rke2SnapshotNameError,
    Rke2SnapshotPreconditionError,
    parse_saved_snapshot_name,
    parse_snapshot_list,
)
from meho_backplane.connectors.rke2.ops_write import (
    ConfigPathRejectedError,
    bound_config_path,
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
# Secret-value canaries planted in the rke2.node.config.get fixture (#2854).
# The op reads the config body but must REDACT every secret key, so none of
# these may surface anywhere in the result.
_CONFIG_TOKEN_CANARY = "K10rke2configREADcanaryDONOTLEAK::server:ccc333"  # gitleaks:allow NOSONAR
_CONFIG_AGENT_TOKEN_CANARY = "rke2configAGENTcanaryDONOTLEAKddd444"  # gitleaks:allow NOSONAR
_CONFIG_S3_SECRET_CANARY = "rke2configS3secretDONOTLEAKeee555"  # gitleaks:allow NOSONAR
_CONFIG_DSN_PASS_CANARY = "dsnPassDONOTLEAKfff666"  # gitleaks:allow NOSONAR
# A config.yaml body carrying secret + non-secret keys the operator wants to
# read back (tls-san / node-taint) alongside the redacted ones.
_CONFIG_YAML_FIXTURE = (
    f"token: {_CONFIG_TOKEN_CANARY}\n"
    f"agent-token: {_CONFIG_AGENT_TOKEN_CANARY}\n"
    f"etcd-s3-secret-key: {_CONFIG_S3_SECRET_CANARY}\n"
    "tls-san:\n"
    "  - 10.0.0.5\n"
    "  - rke2.lab.example\n"
    "node-taint:\n"
    "  - dedicated=infra:NoSchedule\n"
    f"datastore-endpoint: postgres://dbuser:{_CONFIG_DSN_PASS_CANARY}@db.lab.example:5432/kine\n"
)

# Nested-secret canaries for the sibling files config.get must REJECT (#2854 B1).
# The flat top-level redaction set only covers config.yaml's schema, so these
# secrets -- the admin kubeconfig's cluster-admin private key and a registry
# password, both nested -- would leak verbatim if the op ever read the file.
# The op must therefore refuse rke2.yaml / registries.yaml before any SSH read.
_KUBECONFIG_KEY_CANARY = "RKE2-ADMIN-KEY-MARKER-DONOTLEAK-8842"  # gitleaks:allow NOSONAR
_REGISTRIES_PASSWORD_CANARY = "registryPassDONOTLEAKggg777"  # gitleaks:allow NOSONAR
# /etc/rancher/rke2/rke2.yaml -- the admin kubeconfig; client-key-data is nested
# under users[].user, not a top-level config.yaml key.
_RKE2_KUBECONFIG_FIXTURE = (
    "apiVersion: v1\n"
    "clusters:\n"
    "  - name: default\n"
    "    cluster:\n"
    "      server: https://127.0.0.1:6443\n"
    "users:\n"
    "  - name: default\n"
    "    user:\n"
    f"      client-key-data: {_KUBECONFIG_KEY_CANARY}\n"
)
# /etc/rancher/rke2/registries.yaml -- the private-registry config; the password
# is nested under configs.<registry>.auth, not a top-level config.yaml key.
_REGISTRIES_YAML_FIXTURE = (
    "configs:\n"
    "  registry.lab.example:\n"
    "    auth:\n"
    "      username: admin\n"
    f"      password: {_REGISTRIES_PASSWORD_CANARY}\n"
)


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


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
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
# build_posture_probe_command / parse_posture_probe_output
# ---------------------------------------------------------------------------


def test_build_posture_probe_command_covers_paths_and_never_cats() -> None:
    cmd = build_posture_probe_command((*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH))
    for path in (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH):
        assert path in cmd
    # Reads modes only -- the token VALUE is never fetched.
    assert "cat" not in cmd
    # Fails closed when the node has no `stat`, rather than reporting
    # every path absent (#2698).
    assert "command -v stat" in cmd
    assert "exit 127" in cmd
    # Traversability is answered by the kernel, not by parsing stat's
    # locale-dependent diagnostic text.
    assert '[ -x "${p%/*}" ]' in cmd


def test_parse_posture_probe_output_parses_and_normalises_mode() -> None:
    stdout = (
        "S|/etc/rancher/rke2/config.yaml|600|root|root\n"
        "S|/etc/rancher/rke2/rke2.yaml|644|root|rke2\n"
    )
    parsed = parse_posture_probe_output(stdout)
    assert parsed["/etc/rancher/rke2/config.yaml"] == {
        "status": STATUS_PRESENT,
        "mode": "0600",
        "owner": "root",
        "group": "root",
    }
    # 3-digit mode left-padded to the canonical 4-digit octal form.
    assert parsed["/etc/rancher/rke2/rke2.yaml"]["mode"] == "0644"


def test_parse_posture_probe_output_distinguishes_absent_from_unreadable() -> None:
    stdout = f"A|/etc/rancher/rke2/rke2.yaml\nU|{RKE2_TOKEN_PATH}\n"
    parsed = parse_posture_probe_output(stdout)
    assert parsed["/etc/rancher/rke2/rke2.yaml"]["status"] == STATUS_ABSENT
    assert parsed[RKE2_TOKEN_PATH]["status"] == STATUS_UNKNOWN
    assert parsed[RKE2_TOKEN_PATH]["mode"] is None


def test_parse_posture_probe_output_skips_malformed_lines() -> None:
    stdout = "garbage banner line\nS|/etc/rancher/rke2/config.yaml|600|root|root\n\nS|truncated\n"
    parsed = parse_posture_probe_output(stdout)
    assert set(parsed) == {"/etc/rancher/rke2/config.yaml"}


# ---------------------------------------------------------------------------
# parse_posture
# ---------------------------------------------------------------------------


def _verdict(status: str, mode: str | None = None) -> dict[str, str | None]:
    """Build a parsed-probe verdict for *status* the way the probe would."""
    if status == STATUS_PRESENT:
        return {"status": status, "mode": mode, "owner": "root", "group": "root"}
    return {"status": status, "mode": None, "owner": None, "group": None}


def test_parse_posture_present_and_redacted_token() -> None:
    probe_map = {
        "/etc/rancher/rke2/config.yaml": _verdict(STATUS_PRESENT, "0600"),
        "/etc/rancher/rke2/rke2.yaml": _verdict(STATUS_PRESENT, "0600"),
        RKE2_TOKEN_PATH: _verdict(STATUS_PRESENT, "0600"),
    }
    posture = parse_posture(probe_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cfg = {c["path"]: c for c in posture["config_files"]}
    assert cfg["/etc/rancher/rke2/config.yaml"]["present"] is True
    assert cfg["/etc/rancher/rke2/config.yaml"]["status"] == STATUS_PRESENT
    assert cfg["/etc/rancher/rke2/config.yaml"]["mode"] == "0600"
    assert cfg["/etc/rancher/rke2/config.yaml"]["detail"] is None
    # Token entry is present, carries its mode, and is explicitly redacted.
    token = posture["token"]
    assert token["path"] == RKE2_TOKEN_PATH
    assert token["present"] is True
    assert token["mode"] == "0600"
    assert token["redacted"] is True
    # No token VALUE field exists anywhere in the envelope.
    assert "value" not in token
    assert "token" not in {k for c in posture["config_files"] for k in c}


def test_parse_posture_absent_paths_report_absent() -> None:
    # Agent node: no server token, no admin kubeconfig -- both genuinely
    # missing, with a traversable parent, so `absent` is the honest answer.
    probe_map = {
        "/etc/rancher/rke2/config.yaml": _verdict(STATUS_PRESENT, "0600"),
        "/etc/rancher/rke2/rke2.yaml": _verdict(STATUS_ABSENT),
        RKE2_TOKEN_PATH: _verdict(STATUS_ABSENT),
    }
    posture = parse_posture(probe_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cfg = {c["path"]: c for c in posture["config_files"]}
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["present"] is False
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["status"] == STATUS_ABSENT
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["mode"] is None
    token = posture["token"]
    assert token["present"] is False
    assert token["status"] == STATUS_ABSENT
    assert token["mode"] is None
    assert token["redacted"] is True


def test_parse_posture_unreadable_parent_is_unknown_not_absent() -> None:
    """#2698: a 0700 root:root server dir must not read as 'no token'."""
    probe_map = {
        "/etc/rancher/rke2/config.yaml": _verdict(STATUS_PRESENT, "0600"),
        "/etc/rancher/rke2/rke2.yaml": _verdict(STATUS_PRESENT, "0600"),
        RKE2_TOKEN_PATH: _verdict(STATUS_UNKNOWN),
    }
    posture = parse_posture(probe_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    token = posture["token"]
    # The load-bearing assertion: NOT False. A rotation pre-check must not
    # be able to read this as "nothing to rotate".
    assert token["present"] is None
    assert token["status"] == STATUS_UNKNOWN
    assert token["mode"] is None
    assert token["redacted"] is True
    # The detail names the directory the operator has to be able to traverse.
    assert "/var/lib/rancher/rke2/server" in str(token["detail"])


def test_parse_posture_path_with_no_verdict_is_unknown() -> None:
    """A path the probe never reported on is undetermined, not absent."""
    posture = parse_posture({}, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    for entry in (*posture["config_files"], posture["token"]):
        assert entry["present"] is None
        assert entry["status"] == STATUS_UNKNOWN
        assert entry["detail"]


def test_parse_posture_entries_share_one_key_set() -> None:
    """One envelope shape per path, whatever the verdict."""
    probe_map = {
        "/etc/rancher/rke2/config.yaml": _verdict(STATUS_PRESENT, "0600"),
        "/etc/rancher/rke2/rke2.yaml": _verdict(STATUS_ABSENT),
        RKE2_TOKEN_PATH: _verdict(STATUS_UNKNOWN),
    }
    posture = parse_posture(probe_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    expected = {"path", "present", "status", "mode", "owner", "group", "detail"}
    for entry in posture["config_files"]:
        assert set(entry) == expected
    assert set(posture["token"]) == expected | {"redacted"}


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
    "S|/etc/rancher/rke2/config.yaml|600|root|root\n"
    "S|/etc/rancher/rke2/rke2.yaml|600|root|root\n"
    "S|/var/lib/rancher/rke2/server/token|600|root|root\n"
)


@pytest.mark.asyncio
async def test_posture_show_returns_redacted_envelope() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_STAT_STDOUT_FULL)
        result = await connector.posture_show(_TARGET, {})
    # Single SSH round-trip; the command `stat`s, never `cat`s.
    mock_cmd.assert_awaited_once()
    cmd = mock_cmd.await_args.args[1]
    assert "stat -c '%n|%a|%U|%G' --" in cmd
    assert "cat" not in cmd
    # Every measured path appears as a stat argument.
    for path in (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH):
        assert path in cmd
    # Envelope shape + redaction.
    assert result["token"]["present"] is True
    assert result["token"]["status"] == STATUS_PRESENT
    assert result["token"]["mode"] == "0600"
    assert result["token"]["redacted"] is True
    cfg = {c["path"]: c for c in result["config_files"]}
    assert cfg["/etc/rancher/rke2/config.yaml"]["mode"] == "0600"


@pytest.mark.asyncio
async def test_posture_show_reports_missing_token_as_absent() -> None:
    connector = Rke2SshConnector()
    # Agent node: config.yaml present, the other two genuinely missing
    # behind a traversable parent.
    stdout = (
        "S|/etc/rancher/rke2/config.yaml|640|root|root\n"
        "A|/etc/rancher/rke2/rke2.yaml\n"
        f"A|{RKE2_TOKEN_PATH}\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.posture_show(_TARGET, {})
    assert result["token"]["present"] is False
    assert result["token"]["status"] == STATUS_ABSENT
    assert result["token"]["redacted"] is True
    cfg = {c["path"]: c for c in result["config_files"]}
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["present"] is False


@pytest.mark.asyncio
async def test_posture_show_unreadable_token_dir_reports_unknown() -> None:
    """#2698 regression: the observed server-A shape must not read 'absent'.

    ``rke2.yaml`` is 0600 root:root and stats fine (the parent is
    traversable); the token sits behind a 0700 root:root server dir, so its
    existence is undetermined -- not false.
    """
    connector = Rke2SshConnector()
    stdout = (
        "S|/etc/rancher/rke2/config.yaml|600|root|root\n"
        "S|/etc/rancher/rke2/rke2.yaml|600|root|root\n"
        f"U|{RKE2_TOKEN_PATH}\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=stdout)
        result = await connector.posture_show(_TARGET, {})
    token = result["token"]
    assert token["present"] is None
    assert token["status"] == STATUS_UNKNOWN
    assert token["mode"] is None
    assert token["redacted"] is True
    # The config sanity signal still works — this is what isolates the
    # cause to parent-directory traversal rather than "stat can't see
    # root-owned files".
    cfg = {c["path"]: c for c in result["config_files"]}
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["present"] is True
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["mode"] == "0600"


@pytest.mark.asyncio
async def test_posture_show_raises_when_probe_cannot_run() -> None:
    """A non-zero probe exit is an infrastructure failure, never posture.

    Same discipline as the snapshot precondition guard: the probe answers
    every real verdict with exit 0, so a non-zero exit must not be served
    as a file-presence answer (#2698).
    """
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr="sh: stat: not found", exit_status=127)
        with pytest.raises(Rke2PostureProbeError, match="no `stat` on the node"):
            await connector.posture_show(_TARGET, {})


@pytest.mark.asyncio
async def test_posture_show_accepts_complete_output_with_no_exit_status() -> None:
    """``exit_status=None`` + a verdict per path is a complete run, not a failure.

    ``asyncssh`` reports ``None`` when the peer closed the channel without
    sending an exit status; some implementations omit it while still
    delivering full output. The marker protocol gives independent evidence
    the probe finished, so this must not fail a posture read that worked.
    """
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_STAT_STDOUT_FULL, exit_status=None)
        result = await connector.posture_show(_TARGET, {})
    assert result["token"]["present"] is True
    assert result["token"]["status"] == STATUS_PRESENT


@pytest.mark.asyncio
async def test_posture_show_raises_on_truncated_output_with_no_exit_status() -> None:
    """No exit status AND missing verdicts: neither the run nor the paths add up."""
    connector = Rke2SshConnector()
    truncated = "S|/etc/rancher/rke2/config.yaml|600|root|root\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=truncated, exit_status=None)
        with pytest.raises(Rke2PostureProbeError, match="no exit status reported"):
            await connector.posture_show(_TARGET, {})


@pytest.mark.asyncio
async def test_posture_show_raises_on_signal_death() -> None:
    """asyncssh reports -1 when the remote process died on a signal."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_STAT_STDOUT_FULL, exit_status=-1)
        with pytest.raises(Rke2PostureProbeError, match=r"exit -1"):
            await connector.posture_show(_TARGET, {})


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
# build_service_status_command / parse_service_status (#2833 / #2852)
# ---------------------------------------------------------------------------


# A server node: rke2-server active, rke2-agent not installed. systemd still
# prints inactive/dead + NRestarts=0 for the not-found unit under `--all`; the
# parser must null those live-state fields off the LoadState=not-found verdict.
_SERVICE_STATUS_SERVER_ACTIVE = (
    "UNIT=rke2-server\n"
    "LoadState=loaded\n"
    "ActiveState=active\n"
    "SubState=running\n"
    "ExecMainStartTimestamp=Fri 2026-08-01 09:12:03 UTC\n"
    "NRestarts=0\n"
    "UNIT=rke2-agent\n"
    "LoadState=not-found\n"
    "ActiveState=inactive\n"
    "SubState=dead\n"
    "ExecMainStartTimestamp=\n"
    "NRestarts=0\n"
)

# A crash-looping server: systemd auto-restarting, NRestarts non-zero.
_SERVICE_STATUS_SERVER_CRASHLOOP = (
    "UNIT=rke2-server\n"
    "LoadState=loaded\n"
    "ActiveState=activating\n"
    "SubState=auto-restart\n"
    "ExecMainStartTimestamp=Sat 2026-08-02 14:03:11 UTC\n"
    "NRestarts=7\n"
    "UNIT=rke2-agent\n"
    "LoadState=not-found\n"
)


def test_build_service_status_command_probes_both_units_read_only() -> None:
    cmd = build_service_status_command(SERVICE_UNITS)
    # Both fixed units are probed; the caller supplies no unit name.
    for unit in ("rke2-server", "rke2-agent"):
        assert unit in cmd
    # Every requested property is in the -p selection.
    for prop in SERVICE_STATUS_PROPERTIES:
        assert prop in cmd
    # Read-only: `systemctl show`, never a mutating systemctl verb.
    assert "systemctl show" in cmd
    for verb in ("restart", "start", "stop", "reload", "kill", "enable", "disable"):
        assert f"systemctl {verb}" not in cmd
    assert "is-active" not in cmd
    # --all so NRestarts=0 / an empty start-time are not suppressed.
    assert "--all" in cmd
    # Fails closed when the node has no systemctl (infra failure != verdict).
    assert "command -v systemctl" in cmd
    assert "exit 127" in cmd


def test_parse_service_status_active_server_and_not_found_agent() -> None:
    units = parse_service_status(_SERVICE_STATUS_SERVER_ACTIVE)
    by_unit = {u["unit"]: u for u in units}
    server = by_unit["rke2-server"]
    assert server["load_state"] == "loaded"
    assert server["active_state"] == "active"
    assert server["sub_state"] == "running"
    assert server["since"] == "Fri 2026-08-01 09:12:03 UTC"
    assert server["restart_count"] == 0
    # The agent unit is not installed on a server node: not-found nulls the
    # live-state fields even though systemd prints inactive/dead defaults.
    agent = by_unit["rke2-agent"]
    assert agent["load_state"] == "not-found"
    assert agent["active_state"] is None
    assert agent["sub_state"] is None
    assert agent["since"] is None
    assert agent["restart_count"] is None


def test_parse_service_status_preserves_probe_order() -> None:
    units = parse_service_status(_SERVICE_STATUS_SERVER_ACTIVE)
    assert [u["unit"] for u in units] == ["rke2-server", "rke2-agent"]


def test_parse_service_status_surfaces_nonzero_restart_count() -> None:
    """AC: a non-zero NRestarts surfaces as the crash-loop signal."""
    units = parse_service_status(_SERVICE_STATUS_SERVER_CRASHLOOP)
    server = next(u for u in units if u["unit"] == "rke2-server")
    assert server["restart_count"] == 7
    assert server["active_state"] == "activating"
    assert server["sub_state"] == "auto-restart"


def test_parse_service_status_ignores_banner_and_blank_lines() -> None:
    noisy = "login banner line\n\n" + _SERVICE_STATUS_SERVER_ACTIVE
    units = parse_service_status(noisy)
    assert {u["unit"] for u in units} == {"rke2-server", "rke2-agent"}


def test_parse_service_status_loaded_but_never_started_has_null_since() -> None:
    """A loaded-but-stopped unit reports restart_count 0 and a null since."""
    stdout = (
        "UNIT=rke2-server\n"
        "LoadState=loaded\n"
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "ExecMainStartTimestamp=\n"
        "NRestarts=0\n"
    )
    entry = parse_service_status(stdout)[0]
    assert entry["load_state"] == "loaded"
    assert entry["active_state"] == "inactive"
    assert entry["since"] is None
    assert entry["restart_count"] == 0


# ---------------------------------------------------------------------------
# service_status shim (service-state read tier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_status_runs_systemctl_show_and_returns_units() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_SERVICE_STATUS_SERVER_ACTIVE)
        result = await connector.service_status(_TARGET, {})
    by_unit = {u["unit"]: u for u in result["units"]}
    assert by_unit["rke2-server"]["active_state"] == "active"
    assert by_unit["rke2-agent"]["load_state"] == "not-found"
    # Exactly one read-only systemctl-show round-trip; never a mutation.
    issued = [call.args[1] for call in mock_cmd.await_args_list]
    assert len(issued) == 1
    assert "systemctl show" in issued[0]
    assert "systemctl restart" not in issued[0]


@pytest.mark.asyncio
async def test_service_status_missing_systemctl_raises_probe_error() -> None:
    """A node with no systemctl (guard exit 127) is an infra failure, not a verdict."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr="", exit_status=127)
        with pytest.raises(Rke2ServiceStatusProbeError, match="no `systemctl` on the node"):
            await connector.service_status(_TARGET, {})


@pytest.mark.asyncio
async def test_service_status_accepts_output_with_no_exit_status() -> None:
    """``exit_status=None`` + parsed unit blocks is a complete run, not a failure.

    ``asyncssh`` reports ``None`` when the peer closed the channel without
    sending an exit status; the per-unit markers give independent evidence the
    probe finished, so this must not fail a status read that worked.
    """
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_SERVICE_STATUS_SERVER_ACTIVE, exit_status=None)
        result = await connector.service_status(_TARGET, {})
    assert {u["unit"] for u in result["units"]} == {"rke2-server", "rke2-agent"}


@pytest.mark.asyncio
async def test_service_status_propagates_ssh_failure() -> None:
    """A transport failure escapes so the dispatcher reports connector_error."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = OSError("connection refused")
        with pytest.raises(OSError, match="connection refused"):
            await connector.service_status(_TARGET, {})


# ---------------------------------------------------------------------------
# rke2.node.config.get -- redacted config-content read (#2854)
# ---------------------------------------------------------------------------


def test_redact_config_content_masks_all_secret_keys() -> None:
    """Every documented secret-bearing key is fully redacted; names sorted."""
    raw: dict[str, Any] = {key: f"secret-value-of-{key}" for key in SECRET_CONFIG_KEYS}
    raw["tls-san"] = ["10.0.0.5", "rke2.lab.example"]
    redacted, names = redact_config_content(raw)
    for key in SECRET_CONFIG_KEYS:
        assert redacted[key] == REDACTED_SENTINEL, f"{key!r} not redacted"
    # Non-secret operator-facing keys pass through untouched.
    assert redacted["tls-san"] == ["10.0.0.5", "rke2.lab.example"]
    assert names == sorted(SECRET_CONFIG_KEYS)
    # The input mapping is not mutated in place.
    assert raw["token"] == "secret-value-of-token"


def test_redact_config_content_masks_only_datastore_userinfo() -> None:
    """datastore-endpoint keeps host/port/db; only the user:pass@ is masked."""
    raw = {"datastore-endpoint": "mysql://kine:s3cr3t@tcp(10.0.0.9:3306)/kine"}
    redacted, names = redact_config_content(raw)
    assert redacted["datastore-endpoint"] == (
        f"mysql://{REDACTED_SENTINEL}@tcp(10.0.0.9:3306)/kine"
    )
    assert names == ["datastore-endpoint"]


def test_redact_config_content_leaves_credential_less_datastore() -> None:
    """An embedded-etcd datastore endpoint (no userinfo) is left untouched."""
    raw = {"datastore-endpoint": "https://etcd.lab.example:2379"}
    redacted, names = redact_config_content(raw)
    assert redacted["datastore-endpoint"] == "https://etcd.lab.example:2379"
    assert names == []


def test_redact_config_content_no_secrets_is_noop() -> None:
    """A config with no secret keys returns unchanged content + empty names.

    ``token-file`` is a filesystem PATH, not a secret value, so it is never
    redacted -- verifying it is exactly what an operator asked to read.
    """
    raw = {
        "tls-san": ["a", "b"],
        "write-kubeconfig-mode": "0644",
        "token-file": "/var/lib/rancher/rke2/server/token",
    }
    redacted, names = redact_config_content(raw)
    assert redacted == raw
    assert names == []


@pytest.mark.asyncio
async def test_config_get_returns_redacted_content() -> None:
    """AC: config.get returns parsed content with every secret redacted, not withheld."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_CONFIG_YAML_FIXTURE)
        result = await connector.config_get(_TARGET, {})
    # One bounded SSH round-trip; the same cat+parse the update op runs.
    mock_cmd.assert_awaited_once()
    cmd = mock_cmd.await_args.args[1]
    assert "cat -- /etc/rancher/rke2/config.yaml" in cmd
    assert result["path"] == "/etc/rancher/rke2/config.yaml"
    content = result["content"]
    # Non-secret operator-facing keys are returned verbatim.
    assert content["tls-san"] == ["10.0.0.5", "rke2.lab.example"]
    assert content["node-taint"] == ["dedicated=infra:NoSchedule"]
    # Secret keys are redacted to the sentinel...
    assert content["token"] == REDACTED_SENTINEL
    assert content["agent-token"] == REDACTED_SENTINEL
    assert content["etcd-s3-secret-key"] == REDACTED_SENTINEL
    # ...datastore-endpoint keeps host/port/db but masks the userinfo.
    assert content["datastore-endpoint"] == (
        f"postgres://{REDACTED_SENTINEL}@db.lab.example:5432/kine"
    )
    # redacted_keys lists exactly the masked names, sorted (write-side discipline).
    assert set(result["redacted_keys"]) == {
        "agent-token",
        "datastore-endpoint",
        "etcd-s3-secret-key",
        "token",
    }
    assert result["redacted_keys"] == sorted(result["redacted_keys"])
    # THE guarantee: no planted secret VALUE surfaces anywhere in the result.
    rendered = repr(result)
    for canary in (
        _CONFIG_TOKEN_CANARY,
        _CONFIG_AGENT_TOKEN_CANARY,
        _CONFIG_S3_SECRET_CANARY,
        _CONFIG_DSN_PASS_CANARY,
    ):
        assert canary not in rendered, f"{canary!r} leaked into the result"


@pytest.mark.asyncio
async def test_config_get_rejects_path_traversal_before_ssh() -> None:
    """AC: a traversal path is rejected via bound_config_path -- no SSH round trip."""
    # The op relies on the write side's confinement; a traversal escapes the
    # /etc/rancher/rke2 root and raises before any transport is touched.
    with pytest.raises(ConfigPathRejectedError):
        bound_config_path("../../etc/passwd")

    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.config_get(_TARGET, {"path": "../../etc/passwd"})
    mock_cmd.assert_not_awaited()
    assert "content" not in result
    assert "path check" in result["error"]


def test_bound_read_config_path_confines_to_server_config() -> None:
    """B1: only config.yaml + config.yaml.d/*.yaml pass; sibling files are rejected."""
    # The server config and a single-level drop-in are accepted.
    assert bound_read_config_path(None) == "/etc/rancher/rke2/config.yaml"
    assert (
        bound_read_config_path("/etc/rancher/rke2/config.yaml") == "/etc/rancher/rke2/config.yaml"
    )
    assert (
        bound_read_config_path("/etc/rancher/rke2/config.yaml.d/10-tls.yaml")
        == "/etc/rancher/rke2/config.yaml.d/10-tls.yaml"
    )
    # Sibling files (nested secrets the flat redaction set cannot mask) and a
    # nested drop-in subdir are rejected outright.
    for rejected in (
        "/etc/rancher/rke2/rke2.yaml",
        "/etc/rancher/rke2/registries.yaml",
        "/etc/rancher/rke2/config.yaml.d/nested/dir.yaml",
        "/etc/rancher/rke2/config.yaml.d/../rke2.yaml",
    ):
        with pytest.raises(ConfigPathRejectedError):
            bound_read_config_path(rejected)


@pytest.mark.asyncio
async def test_config_get_rejects_sibling_files_before_ssh() -> None:
    """B1: rke2.yaml / registries.yaml -- whose nested secrets the flat redaction
    set cannot mask -- are rejected before any SSH read, so a safe/no-approval call
    never returns the cluster-admin private key or the registry password.
    """
    connector = Rke2SshConnector()
    cases = (
        (
            "/etc/rancher/rke2/rke2.yaml",
            _RKE2_KUBECONFIG_FIXTURE,
            _KUBECONFIG_KEY_CANARY,
        ),
        (
            "/etc/rancher/rke2/registries.yaml",
            _REGISTRIES_YAML_FIXTURE,
            _REGISTRIES_PASSWORD_CANARY,
        ),
    )
    for path, fixture, canary in cases:
        with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
            # Even if the file WOULD read back with a nested secret, the op must
            # refuse it before the `cat` ever runs.
            mock_cmd.return_value = _proc(stdout=fixture)
            result = await connector.config_get(_TARGET, {"path": path})
        mock_cmd.assert_not_awaited()
        assert "content" not in result
        assert "path check" in result["error"]
        assert "server config" in result["error"]
        assert canary not in repr(result), f"{canary!r} leaked for {path}"


@pytest.mark.asyncio
async def test_config_get_accepts_config_yaml_d_dropin() -> None:
    """A config.yaml.d/*.yaml drop-in stays in scope (same flat schema, redacted)."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_CONFIG_YAML_FIXTURE)
        result = await connector.config_get(
            _TARGET, {"path": "/etc/rancher/rke2/config.yaml.d/99-custom.yaml"}
        )
    mock_cmd.assert_awaited_once()
    cmd = mock_cmd.await_args.args[1]
    assert "cat -- /etc/rancher/rke2/config.yaml.d/99-custom.yaml" in cmd
    assert result["path"] == "/etc/rancher/rke2/config.yaml.d/99-custom.yaml"
    assert result["content"]["token"] == REDACTED_SENTINEL


@pytest.mark.asyncio
async def test_config_get_non_mapping_yaml_returns_structured_error() -> None:
    """AC: a YAML sequence/scalar (not a mapping) surfaces an error, not a crash."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="- just\n- a\n- list\n")
        result = await connector.config_get(_TARGET, {})
    assert "content" not in result
    assert result["error"] == "config file is not a YAML mapping"
    assert result["path"] == "/etc/rancher/rke2/config.yaml"


@pytest.mark.asyncio
async def test_config_get_invalid_yaml_returns_structured_error() -> None:
    """AC: unparseable YAML surfaces a structured error rather than crashing."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="key: [unterminated\n")
        result = await connector.config_get(_TARGET, {})
    assert "content" not in result
    assert "not valid YAML" in result["error"]


@pytest.mark.asyncio
async def test_config_get_missing_or_empty_file_returns_empty_content() -> None:
    """A missing/empty config.yaml (the ``[ -e ]`` guard) yields content: {}."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="")
        result = await connector.config_get(_TARGET, {})
    assert result["content"] == {}
    assert result["redacted_keys"] == []


@pytest.mark.asyncio
async def test_config_get_read_failure_returns_structured_error() -> None:
    """A non-zero read exit surfaces a structured error, not a partial parse."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr="cat: permission denied", exit_status=1)
        result = await connector.config_get(_TARGET, {})
    assert "content" not in result
    assert result["error"] == "failed to read the config file"


@pytest.mark.asyncio
async def test_config_get_never_leaks_secret_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The config-get envelope + logs carry no credential material."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_CONFIG_YAML_FIXTURE)
        with caplog.at_level("DEBUG"):
            result = await connector.config_get(_TARGET, {})
    rendered = repr(result)
    for canary in (
        _CONFIG_TOKEN_CANARY,
        _CONFIG_AGENT_TOKEN_CANARY,
        _CONFIG_S3_SECRET_CANARY,
        _CONFIG_DSN_PASS_CANARY,
    ):
        assert canary not in rendered
        assert canary not in caplog.text


# ---------------------------------------------------------------------------
# RKE2_OPS registration shape
# ---------------------------------------------------------------------------


#: The read-only tier: ``rke2.about`` + ``rke2.posture.show`` (T1 #2221),
#: ``rke2.node.service.status`` (Initiative #2833 / #2852), and the redacted
#: config-content read ``rke2.node.config.get`` (#2854). Every entry is
#: safe-tier / no-approval.
_READ_OP_IDS: frozenset[str] = frozenset(
    {
        "rke2.about",
        "rke2.posture.show",
        "rke2.node.service.status",
        "rke2.node.config.get",
    }
)

#: The read ops that take no operator parameters at all (fixed paths/units).
#: The sibling ``rke2.node.config.get`` takes an optional path-bounded
#: ``path``, so it is excluded from the no-params invariant.
_PARAMETERLESS_READ_OP_IDS: frozenset[str] = frozenset(
    {"rke2.about", "rke2.posture.show", "rke2.node.service.status"}
)

#: The approval-gated write tier: ``rke2.token.rotate`` (T2 #2429) plus the
#: node-write ops ``rke2.node.service.restart`` / ``rke2.node.config.update``
#: (T3 #2430). Every entry is dangerous-tier / requires-approval.
_WRITE_OP_IDS: frozenset[str] = frozenset(
    {"rke2.token.rotate", "rke2.node.service.restart", "rke2.node.config.update"}
)

#: The safe, non-gated snapshot tier: ``.save`` (T4 #2431) + ``.list``
#: (#2853). Both are safe-tier / no-approval like the read ops.
_SNAPSHOT_OP_IDS: frozenset[str] = frozenset({"rke2.etcd-snapshot.save", "rke2.etcd-snapshot.list"})

#: The *active* snapshot op: ``.save`` copies etcd to disk, so it carries
#: NEITHER the read-only tag NOR the dangerous/write tier -- it belongs to
#: neither sweep set. (``.list``, by contrast, is a genuine read.)
_SNAPSHOT_ACTIVE_OP_IDS: frozenset[str] = frozenset({"rke2.etcd-snapshot.save"})

#: Every op that must carry the ``read-only`` tag: the identity/posture read
#: tier plus the read-only ``.list`` snapshot op (#2853). ``.save`` is
#: deliberately excluded (it is active).
_READ_ONLY_TAGGED_OP_IDS: frozenset[str] = _READ_OP_IDS | frozenset({"rke2.etcd-snapshot.list"})

_EXPECTED_OP_IDS: frozenset[str] = _READ_OP_IDS | _WRITE_OP_IDS | _SNAPSHOT_OP_IDS


def test_rke2_ops_count_matches_expected() -> None:
    # Four read ops (#2221 posture/about + #2852 service.status + #2854
    # config.get) + three approval-gated write ops (#2429 / #2430) + two safe
    # non-gated snapshot ops (.save #2431, .list #2853) = nine.
    assert len(RKE2_OPS) == len(_EXPECTED_OP_IDS)


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


def test_rke2_read_ops_tagged_read_only() -> None:
    """Read-tier ops + ``.list`` carry the read-only tag; ``.save`` does not."""
    by_id = {op.op_id: op for op in RKE2_OPS}
    for op_id in _READ_ONLY_TAGGED_OP_IDS:
        assert "read-only" in by_id[op_id].tags, f"{op_id!r} missing read-only tag"
    # ``.save`` is active (copies etcd to disk), so it is NOT read-only.
    for op_id in _SNAPSHOT_ACTIVE_OP_IDS:
        assert "read-only" not in by_id[op_id].tags, f"{op_id!r} must not be read-only"


def test_rke2_snapshot_op_safe_active_not_gated() -> None:
    """AC: the active snapshot op is safe-tier / no-approval, neither read nor write.

    ``rke2.etcd-snapshot.save`` copies etcd to disk -- it is active on the node
    filesystem yet does not mutate running cluster state, so it is deliberately
    safe-tier and non-gated. It must sit in NEITHER sweep set: not read-only
    (no ``read-only`` tag) and not the dangerous/approval write tier. (``.list``
    is a genuine read and IS read-only -- covered separately.)
    """
    by_id = {op.op_id: op for op in RKE2_OPS}
    for op_id in _SNAPSHOT_ACTIVE_OP_IDS:
        op = by_id[op_id]
        assert op.safety_level == "safe", f"{op_id!r} is not safe-tier"
        assert op.requires_approval is False, f"{op_id!r} requires approval"
        assert "read-only" not in op.tags, f"{op_id!r} must not be read-only-tagged"
        assert op_id not in _READ_OP_IDS
        assert op_id not in _WRITE_OP_IDS


def test_rke2_snapshot_list_is_read_only_safe_no_approval() -> None:
    """AC: ``.list`` is safe-tier / no-approval / read-only (unlike ``.save``).

    Unlike ``.save`` (active), ``rke2.etcd-snapshot.list`` only enumerates
    existing snapshots -- a genuine read -- so it carries the ``read-only`` tag
    and is counted in the snapshot tier, but it is NOT in the dangerous/approval
    write set.
    """
    by_id = {op.op_id: op for op in RKE2_OPS}
    op = by_id["rke2.etcd-snapshot.list"]
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags, "'.list' must be read-only-tagged"
    assert "rke2.etcd-snapshot.list" in _SNAPSHOT_OP_IDS
    assert "rke2.etcd-snapshot.list" not in _WRITE_OP_IDS
    # No operator params: the local snapshot store is fixed.
    assert op.parameter_schema.get("properties") == {}
    assert op.parameter_schema.get("additionalProperties") is False


def test_rke2_write_ops_all_dangerous_approval_gated() -> None:
    """AC: every node-write op is dangerous-tier, approval-gated, write-tagged."""
    for op in RKE2_OPS:
        if op.op_id not in _WRITE_OP_IDS:
            continue
        assert op.safety_level == "dangerous", f"{op.op_id!r} is not dangerous-tier"
        assert op.requires_approval is True, f"{op.op_id!r} must require approval"
        assert "write" in op.tags, f"{op.op_id!r} missing write tag"


def test_rke2_read_ops_parameter_schemas_closed() -> None:
    by_id = {op.op_id: op for op in RKE2_OPS}
    for op in RKE2_OPS:
        if op.op_id not in _READ_OP_IDS:
            continue
        assert op.parameter_schema.get("additionalProperties") is False
    # rke2.about / rke2.posture.show / rke2.node.service.status take no
    # operator parameters -- fixed paths/units.
    for op_id in _PARAMETERLESS_READ_OP_IDS:
        assert by_id[op_id].parameter_schema.get("properties") == {}
    # rke2.node.config.get (#2854) exposes exactly one optional param -- a path
    # bounded to the server config.yaml + its config.yaml.d/*.yaml drop-ins
    # (sibling files like rke2.yaml / registries.yaml are rejected; the
    # handler-side bound_read_config_path check is authoritative).
    get_schema = by_id["rke2.node.config.get"].parameter_schema
    get_props = get_schema.get("properties", {})
    assert set(get_props) == {"path"}
    assert (
        get_props["path"].get("pattern")
        == r"^/etc/rancher/rke2/config\.yaml(\.d/[^/\x00\n\r]+\.yaml)?$"
    )
    assert get_schema.get("required", []) == []  # path is optional
    # The snapshot op exposes exactly one optional, charset-bounded param.
    save_schema = by_id["rke2.etcd-snapshot.save"].parameter_schema
    props = save_schema.get("properties", {})
    assert set(props) == {"name"}
    assert props["name"].get("pattern") == r"^[A-Za-z0-9._-]+$"
    assert save_schema.get("required", []) == []  # name is optional


def test_rke2_write_ops_parameter_schemas_closed() -> None:
    for op in RKE2_OPS:
        if op.op_id not in _WRITE_OP_IDS:
            continue
        # Write ops take bounded params but reject unknown keys.
        assert op.parameter_schema.get("additionalProperties") is False


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
    # Guard runs first (plain, as root -- no sudo argv); then the save by
    # absolute binary path, with no --name flag when none was supplied.
    # No command constructs a sudo argv (repo-wide sudo-guard invariant).
    assert issued[0].startswith("sh -c ")
    assert "datastore-endpoint" in issued[0]
    assert issued[1] == "/var/lib/rancher/rke2/bin/rke2 etcd-snapshot save"
    assert not any("sudo" in cmd for cmd in issued)


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
    assert save_cmd == ("/var/lib/rancher/rke2/bin/rke2 etcd-snapshot save --name pre_up-grade.1")
    assert "sudo" not in save_cmd
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
async def test_etcd_snapshot_save_guard_transport_failure_raises_distinct_error() -> None:
    """M1: a non-zero guard exit is a transport failure, not a node-role verdict.

    ``_run_command`` wraps ``conn.run(check=False)``, so an SSH/shell failure
    returns a non-zero exit with empty stdout. The handler must raise a
    distinct transport error (not mislabel the empty verdict as
    "not an embedded-etcd server"), and MUST NOT run the save command.
    """
    from meho_backplane.connectors.rke2.ops_snapshot import Rke2SnapshotError

    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stderr="sudo: a password is required\n", exit_status=1)
        with pytest.raises(Rke2SnapshotError, match="precondition guard failed to run"):
            await connector.etcd_snapshot_save(_TARGET, {})
    # Guard ran and failed; the save command never did (single await).
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


# ---------------------------------------------------------------------------
# etcd-snapshot.list handler (#2853)
# ---------------------------------------------------------------------------


# A realistic ``rke2 etcd-snapshot list`` transcript: a header row and three
# snapshot rows (two local ``file://``, one ``s3://``). The first row's size is
# non-trivial (50 MiB) per the acceptance criterion; sizes are the documented
# raw-byte integers. Each row is split across adjacent string literals (the
# real vendor lines exceed the 100-col lint limit); they concatenate at compile
# time, and the parser is whitespace-count-insensitive.
_LIST_TABLE_OK = (
    "Name  Location  Size  Created\n"
    "on-demand-srv-0-1754471523  "
    "file:///var/lib/rancher/rke2/server/db/snapshots/on-demand-srv-0-1754471523  "
    "52428800  2026-08-06T09:12:03Z\n"
    "on-demand-srv-0-1754385123  "
    "file:///var/lib/rancher/rke2/server/db/snapshots/on-demand-srv-0-1754385123  "
    "51380224  2026-08-05T09:12:03Z\n"
    "etcd-snapshot-srv-0-1754298723  "
    "s3://rke2-backups/etcd-snapshot-srv-0-1754298723  "
    "50331648  2026-08-04T09:12:03Z\n"
)


def test_parse_snapshot_list_parses_rows() -> None:
    rows = parse_snapshot_list(_LIST_TABLE_OK)
    assert rows == [
        {
            "name": "on-demand-srv-0-1754471523",
            "location": (
                "file:///var/lib/rancher/rke2/server/db/snapshots/on-demand-srv-0-1754471523"
            ),
            "size_bytes": 52428800,
            "created_at": "2026-08-06T09:12:03Z",
        },
        {
            "name": "on-demand-srv-0-1754385123",
            "location": (
                "file:///var/lib/rancher/rke2/server/db/snapshots/on-demand-srv-0-1754385123"
            ),
            "size_bytes": 51380224,
            "created_at": "2026-08-05T09:12:03Z",
        },
        {
            "name": "etcd-snapshot-srv-0-1754298723",
            "location": "s3://rke2-backups/etcd-snapshot-srv-0-1754298723",
            "size_bytes": 50331648,
            "created_at": "2026-08-04T09:12:03Z",
        },
    ]


def test_parse_snapshot_list_skips_header_regardless_of_casing() -> None:
    # An UPPERCASE header (some versions) and a "no snapshots" notice are both
    # skipped: neither ends in an ISO-8601 timestamp.
    out = "NAME  LOCATION  SIZE  CREATED\nNo snapshots found\n"
    assert parse_snapshot_list(out) == []


def test_parse_snapshot_list_empty_output() -> None:
    assert parse_snapshot_list("") == []
    assert parse_snapshot_list("\n  \n") == []


def test_parse_snapshot_list_non_integer_size_is_null() -> None:
    # Version drift: a human-readable ``Size`` column parses to size_bytes=None
    # (fail-closed) rather than a guessed byte conversion; the row survives.
    out = "legacy-snap-1  local  50 MiB  2026-08-01T00:00:00Z\n"
    rows = parse_snapshot_list(out)
    assert rows == [
        {
            "name": "legacy-snap-1",
            "location": "local",
            "size_bytes": None,
            "created_at": "2026-08-01T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_etcd_snapshot_list_success_parses_and_no_sudo() -> None:
    connector = Rke2SshConnector()
    # side_effect order: precondition guard, then the list command.
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stdout=_LIST_TABLE_OK, exit_status=0),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.etcd_snapshot_list(_TARGET, {})
    assert [s["name"] for s in result["snapshots"]] == [
        "on-demand-srv-0-1754471523",
        "on-demand-srv-0-1754385123",
        "etcd-snapshot-srv-0-1754298723",
    ]
    assert result["snapshots"][0]["size_bytes"] == 52428800
    issued = [call.args[1] for call in mock_cmd.await_args_list]
    # Guard runs first (plain, as root -- no sudo argv); then the list by
    # absolute binary path. No command constructs a sudo argv.
    assert issued[0].startswith("sh -c ")
    assert "datastore-endpoint" in issued[0]
    assert issued[1] == "/var/lib/rancher/rke2/bin/rke2 etcd-snapshot list"
    assert not any("sudo" in cmd for cmd in issued)


@pytest.mark.asyncio
async def test_etcd_snapshot_list_reuses_guard_refuses_external_datastore() -> None:
    """AC: ``.list`` raises the SAME precondition error as ``.save`` (shared guard)."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="external-datastore\n")
        with pytest.raises(Rke2SnapshotPreconditionError, match="external datastore"):
            await connector.etcd_snapshot_list(_TARGET, {})
    # Guard ran; the list command never did (single await).
    mock_cmd.assert_awaited_once()


@pytest.mark.asyncio
async def test_etcd_snapshot_list_reuses_guard_refuses_non_server_node() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="no-embedded-etcd\n")
        with pytest.raises(Rke2SnapshotPreconditionError, match="embedded-etcd server"):
            await connector.etcd_snapshot_list(_TARGET, {})
    mock_cmd.assert_awaited_once()


@pytest.mark.asyncio
async def test_etcd_snapshot_list_nonzero_exit_raises() -> None:
    from meho_backplane.connectors.rke2.ops_snapshot import Rke2SnapshotError

    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stderr="FATA[0000] failed to list snapshots\n", exit_status=1),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        with pytest.raises(Rke2SnapshotError, match="list exited 1"):
            await connector.etcd_snapshot_list(_TARGET, {})


@pytest.mark.asyncio
async def test_etcd_snapshot_list_never_leaks_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No SSH/sudo credential material appears in the result or logs."""
    connector = Rke2SshConnector()
    sequence = [
        _proc(stdout="ok\n"),
        _proc(stdout=_LIST_TABLE_OK, exit_status=0),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        with caplog.at_level("DEBUG"):
            result = await connector.etcd_snapshot_list(_TARGET, {})
    rendered = repr(result)
    for canary in (_CANARY_PASSWORD, _CANARY_SSH_KEY, _CANARY_TOKEN_VALUE):
        assert canary not in rendered
        assert canary not in caplog.text


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

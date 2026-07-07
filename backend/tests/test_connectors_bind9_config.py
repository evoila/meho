# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the bind9 config-write op group (G3.4-T4 #590).

Coverage matrix (per Task #590 acceptance criteria):

* :func:`pack_views_tar` -- pure tar-archive builder; absolute member
  names rooted at ``/``, traversal-input rejection, mode-bit pinning.
* :func:`_parse_reload_output` -- the sentinel-delimited parser used
  by ``bind9.config.reload``.
* :func:`bind9_config_apply_file` and :func:`bind9_config_apply_views`
  invoke :func:`atomic_apply` -- the load-bearing T4 constraint that
  the config-write handlers route every staging / validation /
  rollback step through T3's primitive rather than duplicating logic.
  These tests patch ``atomic_apply`` and assert it was called with the
  expected staging payload + validate command.
* :func:`bind9_config_apply_file` rejects traversal-paths pre-stage
  (``ConfigPathRejectedError``); no atomic-apply invocation in that
  branch.
* :func:`bind9_config_backup` shapes the create + list script
  correctly, parses the listing into rows, and emits ``op_class=write``
  with ``state_after=backup_id``.
* :func:`bind9_config_reload` distinguishes success from rndc failure
  via the structured ``ok`` flag (no exception on non-zero
  ``rndc reload`` exit).
* ``CONFIG_OPS`` registration metadata: write ops carry the right
  ``safety_level``, the description and ``llm_instructions`` both
  reference the "global"+"atomic" tokens, and the parameter schemas
  pin ``additionalProperties=False``.

Tests use the same asyncssh-mocked seam as ``test_connectors_bind9_atomic.py``:
:meth:`Bind9Connector._remote_bash_with_sudo` and
:meth:`Bind9Connector._run_command` are patched with
:class:`AsyncMock` whose return value is a stubbed
:class:`asyncssh.SSHCompletedProcess`. Fingerprint is patched to a
fixed Debian-family fingerprint so the ``_resolve_bind_root`` lookup
returns ``/etc/bind`` deterministically.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.bind9 import BIND9_OPS, Bind9Connector
from meho_backplane.connectors.bind9._atomic import AtomicApplyResult
from meho_backplane.connectors.bind9.ops_config import (
    BIND9_CONFIG_APPLY_FILE_PARAMETER_SCHEMA,
    BIND9_CONFIG_APPLY_VIEWS_PARAMETER_SCHEMA,
    BIND9_CONFIG_BACKUP_PARAMETER_SCHEMA,
    BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA,
    ConfigPathRejectedError,
    _parse_reload_output,
    bind9_config_apply_file,
    bind9_config_apply_views,
    bind9_config_backup,
    bind9_config_reload,
    pack_views_tar,
)
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

# ---------------------------------------------------------------------------
# Env fixture -- mirrors test_connectors_bind9_atomic.py
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
# Stub target + completed-process helper
#
# ``secret_ref`` is a Vault KV-v2 path STRING (#2155). The sudo-password
# path resolves it through ``SshConnector._resolve_secret``, which the
# autouse ``_vault_secrets`` fixture routes through the in-memory
# registry below.
# ---------------------------------------------------------------------------

_VAULT_SECRETS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets(_VAULT_SECRETS):
        yield


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    # A Vault KV-v2 path STRING (#2155) — resolved through the stubbed
    # ``_resolve_secret`` seam against the module registry.
    secret_ref: str


def _target_with_secret(name: str, secret: dict[str, Any], *, host: str, port: int | None) -> Any:
    secret_path = f"meho/testing/bind9/{name}"
    _VAULT_SECRETS[secret_path] = secret
    return _StubTarget(name=name, host=host, port=port, secret_ref=secret_path)


_TARGET = _target_with_secret(
    "bind9-test",
    {"username": "root", "password": "test-sudo-pwd"},  # NOSONAR -- unit-test stub
    host="bind9.test.invalid",
    port=22,
)


def _completed_process(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _fingerprint_debian_default() -> FingerprintResult:
    """Stub fingerprint pinning bind config root to ``/etc/bind``."""
    return FingerprintResult(
        vendor="isc",
        product="bind9",
        version="9.18.24",
        build="BIND 9.18.24-Debian",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="ssh: named -v",
        extras={"os": "debian 12", "named_conf_path": "/etc/bind/named.conf"},
    )


# ---------------------------------------------------------------------------
# pack_views_tar (pure)
# ---------------------------------------------------------------------------


class TestPackViewsTar:
    """The pure tar-archive builder used by ``apply_views``."""

    def test_empty_mapping_returns_empty_archive(self) -> None:
        """An empty mapping returns a valid (but member-less) tar.gz."""
        # Empty mapping is rejected at the handler level, but the
        # builder itself should still produce a parseable archive --
        # the contract is "valid tar.gz, possibly with zero members".
        tar_bytes = pack_views_tar({})
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            assert t.getnames() == []

    def test_single_relative_path_packs_under_bind_root(self) -> None:
        """``named.conf.local`` -> ``etc/bind/named.conf.local`` member."""
        tar_bytes = pack_views_tar(
            {"named.conf.local": 'zone "evba.lab" { type master; };\n'},
        )
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            members = t.getnames()
            assert members == ["etc/bind/named.conf.local"]
            extracted = t.extractfile("etc/bind/named.conf.local")
            assert extracted is not None
            assert extracted.read() == b'zone "evba.lab" { type master; };\n'

    def test_absolute_path_under_root_packs_intact(self) -> None:
        """An absolute path under the root is accepted and stripped of leading /."""
        tar_bytes = pack_views_tar(
            {"/etc/bind/views/external.conf": "view-content\n"},
        )
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            assert t.getnames() == ["etc/bind/views/external.conf"]

    def test_traversal_path_rejected_pre_pack(self) -> None:
        """``../../etc/passwd`` -> ConfigPathRejectedError, no archive produced."""
        with pytest.raises(ConfigPathRejectedError):
            pack_views_tar({"../../etc/passwd": "evil"})

    def test_absolute_path_outside_root_rejected(self) -> None:
        """``/etc/passwd`` is outside the bind root -> rejected."""
        with pytest.raises(ConfigPathRejectedError):
            pack_views_tar({"/etc/passwd": "evil"})

    def test_pinned_mode_bits(self) -> None:
        """Every member's mode is pinned to 0o644 (no setuid/setgid smuggling)."""
        tar_bytes = pack_views_tar({"named.conf.local": "x\n"})
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            members = t.getmembers()
            for m in members:
                # 0o644 = -rw-r--r--; no setuid (0o4000), no setgid (0o2000).
                assert m.mode == 0o644

    def test_utf8_content_round_trips(self) -> None:
        """Non-ASCII content (UTF-8) round-trips through encode + extract."""
        content = "; comment with non-ASCII: éè 中文\n"
        tar_bytes = pack_views_tar({"views/comment.txt": content})
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            extracted = t.extractfile("etc/bind/views/comment.txt")
            assert extracted is not None
            assert extracted.read().decode("utf-8") == content

    def test_custom_bind_root_scopes_archive(self) -> None:
        """A non-default bind_root changes the archive member prefix."""
        tar_bytes = pack_views_tar(
            {"named.conf": "x\n"},
            bind_root="/etc/named",  # RHEL-family layout
        )
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
            assert t.getnames() == ["etc/named/named.conf"]


# ---------------------------------------------------------------------------
# _parse_reload_output (pure)
# ---------------------------------------------------------------------------


class TestParseReloadOutput:
    """The sentinel-delimited parser used by ``bind9.config.reload``."""

    def test_success_envelope(self) -> None:
        text = (
            "===STATE_BEFORE_BEGIN===\n"
            "version: 9.18.24\n"
            "zones: 1\n"
            "===STATE_BEFORE_END===\n"
            "===RELOAD_OUT_BEGIN===\n"
            "server reload successful\n"
            "===RELOAD_OUT_END===\n"
            "===STATE_AFTER_BEGIN===\n"
            "version: 9.18.24\n"
            "zones: 1\n"
            "===STATE_AFTER_END===\n"
            "===RELOAD_RC===0\n"
            "===STATUS_BEFORE_RC===0\n"
            "===STATUS_AFTER_RC===0\n"
        )
        result = _parse_reload_output(text)
        assert result["RELOAD_RC"] == "0"
        assert result["RELOAD_OUT"] == "server reload successful"
        assert "version: 9.18.24" in result["STATE_BEFORE"]
        assert "version: 9.18.24" in result["STATE_AFTER"]

    def test_failure_envelope_carries_rndc_exit(self) -> None:
        text = (
            "===STATE_BEFORE_BEGIN===\nv: 9\n===STATE_BEFORE_END===\n"
            "===RELOAD_OUT_BEGIN===\nrndc: connect failed\n===RELOAD_OUT_END===\n"
            "===STATE_AFTER_BEGIN===\nv: 9\n===STATE_AFTER_END===\n"
            "===RELOAD_RC===1\n"
            "===STATUS_BEFORE_RC===0\n"
            "===STATUS_AFTER_RC===0\n"
        )
        result = _parse_reload_output(text)
        assert result["RELOAD_RC"] == "1"
        assert "connect failed" in result["RELOAD_OUT"]


# ---------------------------------------------------------------------------
# bind9_config_apply_file -- routes through atomic_apply
# ---------------------------------------------------------------------------


class TestApplyFile:
    """``bind9.config.apply_file`` calls T3's atomic-apply primitive."""

    async def test_routes_through_atomic_apply_with_named_checkconf_validate(
        self,
    ) -> None:
        """The handler must invoke atomic_apply with named-checkconf as validate.

        This is the load-bearing T4 constraint: no duplicate stage /
        validate / rollback logic in ops_config.py. The test patches
        :func:`atomic_apply` and asserts the call shape.
        """
        connector = Bind9Connector()
        apply_result = AtomicApplyResult(
            state_before="# old\n",
            state_after='zone "x" { };\n',
            audit_slice_path="/etc/bind/named.conf.local",
        )
        atomic_mock = AsyncMock(return_value=apply_result)
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
        ):
            result = await bind9_config_apply_file(
                connector,
                _TARGET,
                {"path": "named.conf.local", "content": 'zone "x" { };\n'},
            )

        # atomic_apply was invoked exactly once.
        assert atomic_mock.await_count == 1
        kwargs = atomic_mock.await_args.kwargs
        assert kwargs["audit_slice_path"] == "/etc/bind/named.conf.local"
        # Validate command must be the whole-config checkconf, NOT
        # named-checkzone -- the latter would refuse a non-zone fragment.
        assert "named-checkconf" in kwargs["validate_command"]
        # Single-file mode: staged_bytes set, staged_tar_bytes absent.
        assert kwargs["staged_bytes"] == b'zone "x" { };\n'
        assert kwargs.get("staged_tar_bytes") is None
        # zone_name must be empty -- config writes have no zone scope.
        assert kwargs["zone_name"] == ""

        # The handler envelope shape.
        assert result["file"] == "/etc/bind/named.conf.local"
        assert result["op_class"] == "write"
        assert result["result_state_before"] == "# old\n"
        assert result["result_state_after"] == 'zone "x" { };\n'

    async def test_traversal_path_rejected_pre_atomic_apply(self) -> None:
        """``../../etc/passwd`` -> ConfigPathRejectedError; atomic_apply not called."""
        connector = Bind9Connector()
        atomic_mock = AsyncMock()
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
            pytest.raises(ConfigPathRejectedError),
        ):
            await bind9_config_apply_file(
                connector,
                _TARGET,
                {"path": "../../etc/passwd", "content": "evil"},
            )
        # The primitive must NOT have been reached.
        assert atomic_mock.await_count == 0

    async def test_missing_sudo_password_raises(self) -> None:
        """target with no password -> ValueError, no remote IO."""
        connector = Bind9Connector()
        bare_target = _target_with_secret(
            "bare", {"username": "root"}, host="bare.invalid", port=22
        )
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            pytest.raises(ValueError, match="sudo_password"),
        ):
            await bind9_config_apply_file(
                connector,
                bare_target,
                {"path": "named.conf.local", "content": "x\n"},
            )


# ---------------------------------------------------------------------------
# bind9_config_apply_views -- routes through atomic_apply (multi-file)
# ---------------------------------------------------------------------------


class TestApplyViews:
    """``bind9.config.apply_views`` calls atomic_apply in multi-file tar mode."""

    async def test_routes_through_atomic_apply_with_tar_payload(self) -> None:
        """The handler must invoke atomic_apply with staged_tar_bytes set."""
        connector = Bind9Connector()
        apply_result = AtomicApplyResult(
            state_before="",
            state_after="new\n",
            audit_slice_path="/etc/bind/named.conf.local",
        )
        atomic_mock = AsyncMock(return_value=apply_result)
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
        ):
            result = await bind9_config_apply_views(
                connector,
                _TARGET,
                {
                    "files": {
                        "named.conf.local": 'zone "x" { type master; file "/etc/bind/db.x"; };\n',
                        "db.x": "$TTL 3600\n@ IN SOA . . 1 1 1 1 1\n",
                    },
                    "primary_path": "named.conf.local",
                },
            )

        assert atomic_mock.await_count == 1
        kwargs = atomic_mock.await_args.kwargs
        # Multi-file mode: staged_tar_bytes set, staged_bytes absent
        # (defaults to None on the primitive's signature).
        assert kwargs.get("staged_bytes") is None
        assert isinstance(kwargs["staged_tar_bytes"], bytes)
        # The tar must contain both members under /etc/bind/.
        with tarfile.open(fileobj=io.BytesIO(kwargs["staged_tar_bytes"]), mode="r:gz") as t:
            names = set(t.getnames())
            assert "etc/bind/named.conf.local" in names
            assert "etc/bind/db.x" in names
        # Validate command must be named-checkconf (not -checkzone).
        assert "named-checkconf" in kwargs["validate_command"]
        # zone_name empty -- multi-file config write.
        assert kwargs["zone_name"] == ""
        # audit_slice_path is the explicit primary_path.
        assert kwargs["audit_slice_path"] == "/etc/bind/named.conf.local"

        # The handler envelope shape.
        assert result["primary_path"] == "/etc/bind/named.conf.local"
        assert set(result["files"]) == {
            "/etc/bind/named.conf.local",
            "/etc/bind/db.x",
        }
        assert result["op_class"] == "write"

    async def test_primary_path_defaults_to_first_sorted_key(self) -> None:
        """``primary_path`` omitted -> first key sorted lexicographically."""
        connector = Bind9Connector()
        apply_result = AtomicApplyResult(
            state_before="",
            state_after="",
            audit_slice_path="/etc/bind/a.conf",
        )
        atomic_mock = AsyncMock(return_value=apply_result)
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
        ):
            result = await bind9_config_apply_views(
                connector,
                _TARGET,
                {"files": {"z.conf": "z\n", "a.conf": "a\n", "m.conf": "m\n"}},
            )
        # Sorted: a.conf, m.conf, z.conf -> primary defaults to a.conf.
        assert result["primary_path"] == "/etc/bind/a.conf"

    async def test_verify_fqdn_renders_dig_predicate(self) -> None:
        """``verify_fqdn`` set -> verify_command uses dig @localhost."""
        connector = Bind9Connector()
        apply_result = AtomicApplyResult(
            state_before="",
            state_after="",
            audit_slice_path="/etc/bind/named.conf.local",
        )
        atomic_mock = AsyncMock(return_value=apply_result)
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
        ):
            await bind9_config_apply_views(
                connector,
                _TARGET,
                {
                    "files": {"named.conf.local": "x\n"},
                    "verify_fqdn": "internal.evba.lab",
                },
            )
        verify_cmd = atomic_mock.await_args.kwargs["verify_command"]
        assert "dig @localhost" in verify_cmd
        assert "internal.evba.lab" in verify_cmd

    async def test_verify_fqdn_omitted_falls_back_to_checkconf(self) -> None:
        """No ``verify_fqdn`` -> verify_command is the static parse check."""
        connector = Bind9Connector()
        apply_result = AtomicApplyResult(
            state_before="",
            state_after="",
            audit_slice_path="/etc/bind/named.conf.local",
        )
        atomic_mock = AsyncMock(return_value=apply_result)
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
        ):
            await bind9_config_apply_views(
                connector,
                _TARGET,
                {"files": {"named.conf.local": "x\n"}},
            )
        verify_cmd = atomic_mock.await_args.kwargs["verify_command"]
        assert "named-checkconf" in verify_cmd

    async def test_empty_files_mapping_rejected(self) -> None:
        """Empty ``files`` -> ValueError; atomic_apply not called."""
        connector = Bind9Connector()
        atomic_mock = AsyncMock()
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
            pytest.raises(ValueError, match="non-empty"),
        ):
            await bind9_config_apply_views(
                connector,
                _TARGET,
                {"files": {}},
            )
        assert atomic_mock.await_count == 0

    async def test_primary_path_not_in_files_rejected(self) -> None:
        """``primary_path`` outside the staged set -> ValueError pre-stage.

        Regression for the double-apply trap (iter-1 B1): if the
        primitive's success-path ``cat`` hits a file the staged tar
        didn't touch, the post-reload audit-slice capture either
        reports an unrelated file or fails outright (missing file ->
        cat exits non-zero -> the primitive raises after the live
        reload already succeeded -> the caller retries -> the change
        is applied twice). Rejecting at the handler boundary keeps
        the audit-replay invariant intact and removes the retry trap.
        """
        connector = Bind9Connector()
        atomic_mock = AsyncMock()
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch(
                "meho_backplane.connectors.bind9.ops_config.atomic_apply",
                atomic_mock,
            ),
            pytest.raises(ValueError, match="must reference one of the staged files"),
        ):
            await bind9_config_apply_views(
                connector,
                _TARGET,
                {
                    "files": {"named.conf.local": "x\n"},
                    # Under bind_root but NOT a key in the files mapping.
                    "primary_path": "named.conf.options",
                },
            )
        # The primitive never ran -- the failure surfaced pre-stage,
        # so there is no half-applied remote state to roll back.
        assert atomic_mock.await_count == 0


# ---------------------------------------------------------------------------
# bind9_config_backup -- creates archive + lists existing backups
# ---------------------------------------------------------------------------


class TestConfigBackup:
    """``bind9.config.backup`` creates a tar.gz, returns ID + listing."""

    async def test_returns_backup_id_and_listing(self) -> None:
        """A successful backup returns the ID, path, rows, total."""
        connector = Bind9Connector()
        listing_json = (
            '[{"id": "bind9-20260518T100000Z", '
            '"path": "/var/backups/meho-bind9/bind9-20260518T100000Z.tar.gz", '
            '"size": 1024, "modified": 1747569600.0}]'
        )
        bash_mock = AsyncMock(return_value=_completed_process(stdout=listing_json, exit_status=0))
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
        ):
            result = await bind9_config_backup(connector, _TARGET, {})

        # Envelope keys.
        assert "backup_id" in result
        assert result["backup_id"].startswith("bind9-")
        assert result["path"].startswith("/var/backups/meho-bind9/")
        assert result["path"].endswith(".tar.gz")
        assert result["op_class"] == "write"
        assert result["state_after"] == result["backup_id"]
        # Rows are parsed from the JSON line.
        assert result["total"] == 1
        assert result["rows"][0]["id"] == "bind9-20260518T100000Z"
        assert result["rows"][0]["size"] == 1024

    async def test_tag_appears_in_backup_filename(self) -> None:
        """A ``tag`` parameter is embedded in the filename + ID."""
        connector = Bind9Connector()
        bash_mock = AsyncMock(return_value=_completed_process(stdout="[]", exit_status=0))
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
        ):
            result = await bind9_config_backup(connector, _TARGET, {"tag": "pre-views-change"})
        assert "pre-views-change" in result["backup_id"]
        assert "pre-views-change" in result["path"]

    async def test_remote_failure_raises_runtime_error(self) -> None:
        """A non-zero exit -> RuntimeError with stderr verbatim."""
        connector = Bind9Connector()
        bash_mock = AsyncMock(
            return_value=_completed_process(
                stdout="",
                stderr="tar: /etc/bind: Cannot open: Permission denied\n",
                exit_status=1,
            )
        )
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
            pytest.raises(RuntimeError, match="Permission denied"),
        ):
            await bind9_config_backup(connector, _TARGET, {})

    async def test_backup_id_distinct_across_same_second_same_tag(self) -> None:
        """Two backups in the same second with the same tag must NOT collide.

        Regression for iter-1 M2: the prior schema embedded only a
        UTC-second timestamp + optional tag, so two concurrent
        backups (or a tight retry loop) could compute the same
        ``backup_id`` and overwrite each other's tar silently.
        ``secrets.token_hex(3)`` appends 24 bits of CSPRNG entropy to
        the filename; the collision risk under any realistic burst
        is negligible and the ID schema (``startswith("bind9-")``)
        stays opaque to existing callers.
        """
        connector = Bind9Connector()
        bash_mock = AsyncMock(return_value=_completed_process(stdout="[]", exit_status=0))
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
            # Freeze the timestamp so the only differentiator left
            # is the random suffix; if the suffix didn't exist the
            # two backup IDs would be byte-identical.
            patch("meho_backplane.connectors.bind9.ops_config.time") as time_mock,
        ):
            time_mock.strftime = lambda fmt, tm: "20260518T120000Z"
            time_mock.gmtime = lambda: None  # value ignored by the lambda above
            first = await bind9_config_backup(connector, _TARGET, {"tag": "pre-prod"})
            second = await bind9_config_backup(connector, _TARGET, {"tag": "pre-prod"})

        assert first["backup_id"] != second["backup_id"], (
            f"same-second same-tag collision: {first['backup_id']!r} == {second['backup_id']!r}"
        )
        assert first["path"] != second["path"]
        # Both still carry the expected prefix and tag.
        for envelope in (first, second):
            assert envelope["backup_id"].startswith("bind9-")
            assert "pre-prod" in envelope["backup_id"]

    async def test_emits_op_class_write_with_state_after_only(self) -> None:
        """``backup`` audit envelope: state_after present, state_before absent."""
        connector = Bind9Connector()
        bash_mock = AsyncMock(return_value=_completed_process(stdout="[]", exit_status=0))
        with (
            patch.object(
                connector, "fingerprint", AsyncMock(return_value=_fingerprint_debian_default())
            ),
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
        ):
            result = await bind9_config_backup(connector, _TARGET, {})
        # state_before is intentionally absent -- nothing in /etc/bind/ mutates.
        assert "state_before" not in result
        assert "result_state_before" not in result
        assert "state_after" in result


# ---------------------------------------------------------------------------
# bind9_config_reload -- structured success/failure envelope
# ---------------------------------------------------------------------------


class TestConfigReload:
    """``bind9.config.reload`` returns a structured envelope, never raises on rndc fail."""

    async def test_success_envelope_has_ok_true(self) -> None:
        connector = Bind9Connector()
        stdout = (
            "===STATE_BEFORE_BEGIN===\nversion: 9.18\n===STATE_BEFORE_END===\n"
            "===RELOAD_OUT_BEGIN===\nserver reload successful\n===RELOAD_OUT_END===\n"
            "===STATE_AFTER_BEGIN===\nversion: 9.18\n===STATE_AFTER_END===\n"
            "===RELOAD_RC===0\n"
            "===STATUS_BEFORE_RC===0\n"
            "===STATUS_AFTER_RC===0\n"
        )
        bash_mock = AsyncMock(return_value=_completed_process(stdout=stdout, exit_status=0))
        with patch.object(connector, "_remote_bash_with_sudo", bash_mock):
            result = await bind9_config_reload(connector, _TARGET, {})
        assert result["ok"] is True
        assert result["rndc_reload_exit"] == 0
        assert result["stderr"] == ""  # no stderr on success
        assert "successful" in result["stdout"]
        assert result["op_class"] == "write"
        assert "version: 9.18" in result["result_state_before"]
        assert "version: 9.18" in result["result_state_after"]

    async def test_rndc_failure_returns_ok_false_no_exception(self) -> None:
        """A non-zero ``rndc reload`` exit is reported, NOT raised."""
        connector = Bind9Connector()
        stdout = (
            "===STATE_BEFORE_BEGIN===\nversion: 9.18\n===STATE_BEFORE_END===\n"
            "===RELOAD_OUT_BEGIN===\nrndc: connect failed\n===RELOAD_OUT_END===\n"
            "===STATE_AFTER_BEGIN===\nversion: 9.18\n===STATE_AFTER_END===\n"
            "===RELOAD_RC===1\n"
            "===STATUS_BEFORE_RC===0\n"
            "===STATUS_AFTER_RC===0\n"
        )
        bash_mock = AsyncMock(return_value=_completed_process(stdout=stdout, exit_status=0))
        with patch.object(connector, "_remote_bash_with_sudo", bash_mock):
            result = await bind9_config_reload(connector, _TARGET, {})
        assert result["ok"] is False
        assert result["rndc_reload_exit"] == 1
        assert "connect failed" in result["stderr"]

    async def test_wrapper_failure_raises(self) -> None:
        """Wrapper-level failure (sudo / ssh down) -> RuntimeError.

        Distinct from a normal rndc-failure: the wrapper exits non-zero
        when the SSH/sudo layer itself broke, which is an exception, not
        a structured envelope.
        """
        connector = Bind9Connector()
        bash_mock = AsyncMock(
            return_value=_completed_process(
                stdout="",
                stderr="sudo: a password is required\n",
                exit_status=1,
            )
        )
        with (
            patch.object(connector, "_remote_bash_with_sudo", bash_mock),
            pytest.raises(RuntimeError, match="password is required"),
        ):
            await bind9_config_reload(connector, _TARGET, {})


# ---------------------------------------------------------------------------
# Registration metadata for the four new T4 ops
# ---------------------------------------------------------------------------


_T4_OP_IDS = [
    "bind9.config.apply_file",
    "bind9.config.apply_views",
    "bind9.config.backup",
    "bind9.config.reload",
]


class TestT4OpsRegistration:
    """The new T4 ops carry the right safety + audit metadata."""

    def test_all_four_t4_ops_registered(self) -> None:
        """Eleven total ops -- the full Initiative #367 §4 surface."""
        op_ids = {op.op_id for op in BIND9_OPS}
        for expected in _T4_OP_IDS:
            assert expected in op_ids
        # All eleven landed.
        assert len(BIND9_OPS) == 11

    @pytest.mark.parametrize(
        "op_id, expected_safety, expected_requires_approval",
        [
            # The two dangerous config-replacement ops require four-eyes on
            # every principal kind (#129) — the non-agent gate keys on
            # requires_approval, so a dangerous op must set it or a human
            # tenant_admin could run it with no approval.
            ("bind9.config.apply_file", "dangerous", True),
            ("bind9.config.apply_views", "dangerous", True),
            # caution ops stay default-allow for humans (auto-execute).
            ("bind9.config.backup", "caution", False),
            ("bind9.config.reload", "caution", False),
        ],
    )
    def test_safety_levels_pin_to_spec(
        self, op_id: str, expected_safety: str, expected_requires_approval: bool
    ) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.safety_level == expected_safety
        assert op.requires_approval is expected_requires_approval

    def test_no_dangerous_op_bypasses_approval(self) -> None:
        """No ``dangerous`` bind9 op ships ``requires_approval=False`` (#129).

        The non-agent policy gate (``_non_agent_verdict``) keys only on
        ``requires_approval``, so a ``dangerous`` op left ``requires_approval=
        False`` lets a human/service principal execute it with no four-eyes
        step. Pin the invariant so a future write op can't reintroduce the gap.
        """
        offenders = [
            o.op_id for o in BIND9_OPS if o.safety_level == "dangerous" and not o.requires_approval
        ]
        assert offenders == [], offenders

    @pytest.mark.parametrize("op_id", ["bind9.config.apply_file", "bind9.config.apply_views"])
    def test_dangerous_op_carries_global_atomic_warning_in_description(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        text = op.description.lower()
        # The exact tokens "global" and "atomic" must appear -- they're
        # the load-bearing agent-visible warning (Initiative #367 WI7).
        assert "global" in text
        assert "atomic" in text

    @pytest.mark.parametrize("op_id", ["bind9.config.apply_file", "bind9.config.apply_views"])
    def test_dangerous_op_llm_instructions_carry_global_atomic_warning(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.llm_instructions is not None
        text = op.llm_instructions.get("when_to_use", "").lower()
        assert "global" in text
        assert "atomic" in text

    @pytest.mark.parametrize("op_id", _T4_OP_IDS)
    def test_parameter_schema_disallows_additional_properties(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.parameter_schema.get("additionalProperties") is False

    @pytest.mark.parametrize("op_id", _T4_OP_IDS)
    def test_handler_attr_resolves_on_connector_class(self, op_id: str) -> None:
        """Every op declares a handler_attr that resolves on the connector class."""
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        handler = getattr(Bind9Connector, op.handler_attr, None)
        assert handler is not None, (
            f"op {op_id!r} declares handler_attr={op.handler_attr!r} "
            f"but Bind9Connector has no such attribute"
        )

    def test_backup_tag_pattern_restricts_charset(self) -> None:
        """The ``tag`` schema's regex rejects shell-meta + path separators."""
        # The pattern lives in the parameter schema; the agent's input
        # is filtered at the dispatcher's validate gate, but we pin
        # the shape here too.
        tag_schema = BIND9_CONFIG_BACKUP_PARAMETER_SCHEMA["properties"]["tag"]
        import re

        # Allowed examples must match.
        for ok in ("pre-prod", "v1.2.3", "snapshot_2026", "abc"):
            assert re.match(tag_schema["pattern"], ok), f"{ok!r} should match"
        # Hostile examples must NOT match.
        for bad in ("../escape", "foo/bar", "foo;rm -rf /", "foo$(whoami)", "foo bar"):
            assert not re.match(tag_schema["pattern"], bad), (
                f"{bad!r} should NOT match the tag pattern"
            )

    def test_apply_views_requires_files_in_schema(self) -> None:
        """``files`` is a required property."""
        assert "files" in BIND9_CONFIG_APPLY_VIEWS_PARAMETER_SCHEMA["required"]

    def test_apply_file_requires_path_and_content(self) -> None:
        """Both ``path`` and ``content`` are required."""
        assert set(BIND9_CONFIG_APPLY_FILE_PARAMETER_SCHEMA["required"]) == {
            "path",
            "content",
        }

    def test_reload_schema_takes_no_params(self) -> None:
        """``reload`` declares no parameters -- the op takes none."""
        assert BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA["properties"] == {}
        assert BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA.get("required", []) == []

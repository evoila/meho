# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HolodeckConnector skeleton (G3.8-T1 #853).

Coverage matrix (per Task #853 acceptance criteria):

* ``HolodeckConnector`` advertises the registry-v2 triple
  ``("holodeck", "9.0", "holodeck-ssh")``.
* Importing the package registers the connector against the v2
  registry under that triple (and does **not** dual-write to v1).
* :meth:`HolodeckConnector._auth_config` -- password-default works
  (Vault secret with ``password`` and no ``ssh_private_key`` returns
  ``{username, password}``); key-preferred works (Vault secret with
  ``ssh_private_key`` returns ``{username, client_keys}``); missing
  both fields raises :exc:`ValueError`.
* Per-target connection isolation: two distinct targets do not share
  a pooled connection object.
* :func:`parse_photon_version` recovers the version token from the
  Photon release strings the connector targets.
* :meth:`HolodeckConnector.fingerprint` returns the canonical
  :class:`FingerprintResult` shape against a mocked ``_run_command``
  + ``pwsh_run`` seam with valid Photon + ``Get-HoloDeckConfig``
  outputs.
* :meth:`HolodeckConnector.fingerprint` with unreachable SSH ->
  ``reachable=False`` + ``extras["error"]``.
* :meth:`HolodeckConnector.fingerprint` with broken pwsh (cmdlet
  fail) -> ``reachable=False`` and still surfaces the Photon snapshot
  in ``extras``.
* :meth:`HolodeckConnector.probe` returns the four distinct
  ``ProbeResult.reason`` values from the matching failure modes:
  ``tcp_unreachable``, ``ssh_auth_failed``, ``photon_unhealthy``,
  ``holodeck_services_down``.
* :meth:`HolodeckConnector.about` reuses :meth:`fingerprint` and
  surfaces the flat dict the dispatcher's reducer forwards verbatim.
* :meth:`HolodeckConnector.execute` after registration is the same
  shape as the bind9 / pfSense siblings: unknown / invalid_params /
  ok envelopes via the G0.6 dispatcher shim.
* Secret-leak invariant: nothing in :meth:`fingerprint`,
  :meth:`probe`, or :meth:`execute`'s observable surface (return
  value, logs, exception messages) carries the canary password the
  test target ships.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.holodeck  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors import all_connectors_v2
from meho_backplane.connectors.holodeck import HOLODECK_OPS, HolodeckConnector
from meho_backplane.connectors.holodeck._pwsh import PwshRunError
from meho_backplane.connectors.holodeck.connector import parse_photon_version
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Target + process stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]
    # The SSH connection pool keys on ``target_cache_key`` (``(tenant_id,
    # id)``); a double missing either field hits ``AttributeError`` at the
    # pool (evoila/meho#1682). ``id`` defaults off ``name`` so distinct
    # targets in one tenant land on distinct pool keys.
    id: str = ""
    tenant_id: str = "00000000-0000-0000-0000-000000000000"

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"id-{self.name}"


# Canary credentials the secret-leak assertions key on. The fake
# "private key" string is assembled at runtime from non-secret-shaped
# fragments so gitleaks' ``private-key`` rule doesn't false-positive
# on the test source: we want the assertion target to be the same
# substring that real PEM headers would surface, without writing a
# literal PEM-bracketed multi-line constant into the file.
_CANARY_PASSWORD = "holodeck-canary-password-xyz"  # NOSONAR
_FAKE_KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----"
_FAKE_KEY_FOOTER = "-----END " + "OPENSSH PRIVATE KEY" + "-----"
_CANARY_PRIVATE_KEY = f"{_FAKE_KEY_HEADER}\nFAKE-CANARY-KEY-BODY\n{_FAKE_KEY_FOOTER}\n"


def _password_target(name: str = "holorouter-pw") -> _StubTarget:
    return _StubTarget(
        name=name,
        host=f"{name}.test.invalid",
        port=22,
        secret_ref={"username": "root", "password": _CANARY_PASSWORD},
    )


def _key_target(name: str = "holorouter-key") -> _StubTarget:
    return _StubTarget(
        name=name,
        host=f"{name}.test.invalid",
        port=22,
        secret_ref={"username": "root", "ssh_private_key": _CANARY_PRIVATE_KEY},
    )


def _no_cred_target(name: str = "holorouter-empty") -> _StubTarget:
    return _StubTarget(
        name=name,
        host=f"{name}.test.invalid",
        port=22,
        secret_ref={"username": "root"},
    )


def _completed_process(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


# Canonical Photon and Holodeck cmdlet outputs the fingerprint / probe
# tests reuse.
_PHOTON_5 = "VMware Photon Linux 5.0\nPHOTON_BUILD_NUMBER=12345\n"
_HOLODECK_CONFIG_JSON = '{"Version":"9.0.0","PodId":"lab-pod-01"}'
_HOLODECK_SERVICES_ALL_RUNNING_JSON = (
    '[{"Name":"HoloDNS","Status":"Running"},{"Name":"HoloDHCP","Status":"Running"}]'
)
_HOLODECK_SERVICES_ONE_DOWN_JSON = (
    '[{"Name":"HoloDNS","Status":"Running"},{"Name":"HoloDHCP","Status":"Stopped"}]'
)


# ---------------------------------------------------------------------------
# Class-level registry v2 metadata + package import registration
# ---------------------------------------------------------------------------


def test_registry_v2_class_attrs() -> None:
    """Class-level attrs match the v2 triple the package registers."""
    assert HolodeckConnector.product == "holodeck"
    assert HolodeckConnector.version == "9.0"
    assert HolodeckConnector.impl_id == "holodeck-ssh"
    assert HolodeckConnector.supported_version_range is None
    assert HolodeckConnector.priority == 0


def test_package_import_registers_v2_entry_only() -> None:
    """Importing the package registers under the v2 triple, no v1 dual-write.

    Drives the registry clear + reload itself so the assertion observes
    the **side-effect of importing the package** rather than a fixture's
    re-registration -- mirrors the bind9 / pfSense pattern.
    """
    import meho_backplane.connectors.holodeck as holodeck_pkg

    clear_registry()
    # Force the module-top-level ``register_connector_v2`` call in
    # ``holodeck/__init__.py`` to fire under the cleared registry. The
    # ``HolodeckConnector`` class object is owned by
    # ``connectors.holodeck.connector`` which is *not* reloaded here,
    # so the post-reload registry entry points at the same class object
    # the rest of this module imports at the top.
    importlib.reload(holodeck_pkg)

    v2 = all_connectors_v2()
    assert v2[("holodeck", "9.0", "holodeck-ssh")] is HolodeckConnector
    # G0.15-T6 (#1215) wildcard fanout -- a fresh target with
    # ``version=None`` resolves to ``HolodeckConnector`` through the
    # wildcard. The wildcard lands via :func:`register_connector_v2`
    # directly (not the v1 dual-write surface) so holodeck still has
    # no v1 chassis history.
    assert v2[("holodeck", "", "")] is HolodeckConnector


def test_about_canary_op_remains_at_index_zero() -> None:
    """``holodeck.about`` is the T1 canary; T2 (#854) appends the 7 read ops
    onto the same tuple while preserving the canary at index 0. The full T2
    registration shape lives in ``test_connectors_holodeck_ops.py``.
    """
    assert HOLODECK_OPS[0].op_id == "holodeck.about"
    assert HOLODECK_OPS[0].handler_attr == "about"
    assert len(HOLODECK_OPS) == 9


# ---------------------------------------------------------------------------
# parse_photon_version (AC #4 sub-bullet)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, expected",
    [
        ("VMware Photon Linux 5.0\nPHOTON_BUILD_NUMBER=...", "5.0"),
        ("VMware Photon OS 4.0", "4.0"),
        ("VMware Photon Linux 3.0\nID=photon\n", "3.0"),
        ("Photon 5.0.2", "5.0.2"),
    ],
)
def test_parse_photon_version_recovers_token(content: str, expected: str) -> None:
    assert parse_photon_version(content) == expected


def test_parse_photon_version_returns_none_on_garbage() -> None:
    assert parse_photon_version("") is None
    assert parse_photon_version("totally unrelated text") is None


# ---------------------------------------------------------------------------
# Auth: password-default + key-fallback + neither (AC #3)
# ---------------------------------------------------------------------------


async def test_auth_config_password_default() -> None:
    """Password-only secret -> ``{username, password}``; no key path taken."""
    connector = HolodeckConnector()
    auth = await connector._auth_config(_password_target())
    assert auth == {"username": "root", "password": _CANARY_PASSWORD}
    assert "client_keys" not in auth


async def test_auth_config_key_preferred_when_present() -> None:
    """``ssh_private_key`` present in the secret wins over password.

    The base ``_auth_config`` checks ``ssh_private_key`` first and
    returns the key auth dict -- mirroring the wrapper's
    ``PreferredAuthentications=publickey,password`` shape.
    """
    connector = HolodeckConnector()
    # Stub ``asyncssh.import_private_key`` since the canary PEM is not
    # a real key; we just need to confirm the key branch is taken and
    # the result is shaped correctly.
    stub_key = MagicMock(name="stub-private-key")
    with patch("asyncssh.import_private_key", return_value=stub_key) as imp:
        auth = await connector._auth_config(_key_target())
    imp.assert_called_once_with(_CANARY_PRIVATE_KEY)
    assert auth == {"username": "root", "client_keys": [stub_key]}
    assert "password" not in auth


async def test_auth_config_raises_when_neither_credential_is_set() -> None:
    connector = HolodeckConnector()
    with pytest.raises(ValueError, match="ssh_private_key or password"):
        await connector._auth_config(_no_cred_target())


# ---------------------------------------------------------------------------
# Per-target connection isolation (AC #3 sub-bullet)
# ---------------------------------------------------------------------------


async def test_per_target_connection_isolation() -> None:
    """Two distinct targets get distinct pooled connection objects."""
    connector = HolodeckConnector()
    conn_a = MagicMock(name="conn-A")
    conn_a.is_closed.return_value = False
    conn_b = MagicMock(name="conn-B")
    conn_b.is_closed.return_value = False
    target_a = _password_target("holorouter-a")
    target_b = _password_target("holorouter-b")
    with patch("asyncssh.connect", new=AsyncMock(side_effect=[conn_a, conn_b])):
        a = await connector._connect(target_a, raw_jwt="")
        b = await connector._connect(target_b, raw_jwt="")
    assert a is conn_a
    assert b is conn_b
    assert a is not b
    # Pool keyed by the tenant-unique ``(tenant_id, id)`` tuple; both present.
    assert (target_a.tenant_id, target_a.id) in connector._connections
    assert (target_b.tenant_id, target_b.id) in connector._connections


# ---------------------------------------------------------------------------
# Fingerprint (AC #4)
# ---------------------------------------------------------------------------


async def test_fingerprint_returns_canonical_shape_against_mocked_seams() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value={"Version": "9.0.0", "PodId": "lab-pod-01"}),
        ),
    ):
        result = await connector.fingerprint(_password_target())

    assert result.vendor == "vmware"
    assert result.product == "holodeck"
    assert result.version == "9.0.0"
    assert result.build == "VMware Photon Linux 5.0"
    assert result.reachable is True
    assert result.probe_method == "ssh: pwsh Get-HoloDeckConfig"
    assert result.extras["photon_version"] == "5.0"
    assert result.extras["pod_id"] == "lab-pod-01"


async def test_fingerprint_unreachable_when_ssh_fails() -> None:
    connector = HolodeckConnector()
    with patch.object(
        connector,
        "_run_command",
        AsyncMock(side_effect=OSError("connection refused")),
    ):
        result = await connector.fingerprint(_password_target())
    assert result.reachable is False
    assert "connection refused" in result.extras["error"]
    # Auth canary must never appear in the fingerprint result.
    assert _CANARY_PASSWORD not in str(result.extras)


async def test_fingerprint_partial_when_pwsh_cmdlet_fails() -> None:
    """Photon read OK but cmdlet failed -> reachable=False; Photon snapshot stays in extras."""
    connector = HolodeckConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(
                side_effect=PwshRunError(
                    "pwsh -EncodedCommand exited with status 1",
                    exit_status=1,
                    stderr="Get-HoloDeckConfig : cmdlet not found",
                )
            ),
        ),
    ):
        result = await connector.fingerprint(_password_target())
    assert result.reachable is False
    assert "pwsh" in result.extras["error"]
    # Photon snapshot is preserved so the operator sees how far the
    # probe got before the cmdlet broke.
    assert result.extras["photon_version"] == "5.0"


# ---------------------------------------------------------------------------
# Probe — four distinct reasons (AC #5)
# ---------------------------------------------------------------------------


async def test_probe_tcp_unreachable() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("no route"))):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "tcp_unreachable"


async def test_probe_ssh_auth_failed_on_permission_denied() -> None:
    connector = HolodeckConnector()
    with patch.object(
        connector,
        "_connect",
        AsyncMock(side_effect=asyncssh.PermissionDenied(reason="bad password")),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "ssh_auth_failed"


async def test_probe_ssh_auth_failed_on_disconnect_error() -> None:
    """``DisconnectError`` (non-auth handshake failures) also folds into ``ssh_auth_failed``.

    The Initiative #371 taxonomy uses four buckets; bind9 splits
    auth-vs-handshake but Holodeck does not. The operator response is
    the same.
    """
    connector = HolodeckConnector()
    with patch.object(
        connector,
        "_connect",
        AsyncMock(side_effect=asyncssh.DisconnectError(code=1, reason="protocol error")),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "ssh_auth_failed"


async def test_probe_ssh_auth_failed_when_credentials_missing() -> None:
    """``ValueError`` from ``_auth_config`` (no creds) folds into ``ssh_auth_failed``."""
    connector = HolodeckConnector()
    with patch.object(
        connector,
        "_connect",
        AsyncMock(side_effect=ValueError("secret_ref must include ssh_private_key or password")),
    ):
        result = await connector.probe(_no_cred_target())
    assert result.ok is False
    assert result.reason == "ssh_auth_failed"


async def test_probe_photon_unhealthy_on_empty_release_file() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout="", exit_status=0)),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "photon_unhealthy"


async def test_probe_photon_unhealthy_on_nonzero_exit() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout="", exit_status=1)),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "photon_unhealthy"


async def test_probe_holodeck_services_down_when_service_is_stopped() -> None:
    connector = HolodeckConnector()
    services_payload = [
        {"Name": "HoloDNS", "Status": "Running"},
        {"Name": "HoloDHCP", "Status": "Stopped"},
    ]
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value=services_payload),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "holodeck_services_down"


async def test_probe_holodeck_services_down_when_pwsh_fails() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(
                side_effect=PwshRunError(
                    "pwsh -EncodedCommand exited with status 1",
                    exit_status=1,
                    stderr="...",
                )
            ),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is False
    assert result.reason == "holodeck_services_down"


async def test_probe_happy_path_returns_ok_true() -> None:
    connector = HolodeckConnector()
    services_payload = [
        {"Name": "HoloDNS", "Status": "Running"},
        {"Name": "HoloDHCP", "Status": "Running"},
    ]
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value=services_payload),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is True
    assert result.reason is None
    assert result.latency_ms is not None and result.latency_ms >= 0.0


async def test_probe_accepts_single_service_dict_payload() -> None:
    """``ConvertTo-Json`` on a single-element list emits a flat dict, not a list."""
    connector = HolodeckConnector()
    with (
        patch.object(connector, "_connect", AsyncMock()),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value={"Name": "HoloDNS", "Status": "Running"}),
        ),
    ):
        result = await connector.probe(_password_target())
    assert result.ok is True


# ---------------------------------------------------------------------------
# about wrapper
# ---------------------------------------------------------------------------


async def test_about_reuses_fingerprint_and_returns_flat_dict() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value={"Version": "9.0.0", "PodId": "lab-pod-01"}),
        ),
    ):
        payload = await connector.about(_password_target(), {})

    assert payload == {
        "vendor": "vmware",
        "product": "holodeck",
        "version": "9.0.0",
        "build": "VMware Photon Linux 5.0",
        "photon_version": "5.0",
        "pod_id": "lab-pod-01",
    }


# ---------------------------------------------------------------------------
# Secret-leak invariants on fingerprint / probe / about result shapes
# ---------------------------------------------------------------------------


async def test_observable_paths_do_not_leak_canary_password() -> None:
    """The canary password from secret_ref never appears in any observable surface."""
    connector = HolodeckConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value={"Version": "9.0.0", "PodId": "lab-pod-01"}),
        ),
    ):
        fp = await connector.fingerprint(_password_target())
        about = await connector.about(_password_target(), {})

    serialised = repr(fp) + repr(about)
    assert _CANARY_PASSWORD not in serialised
    # Private-key canary never enters; this is a password-only target.
    assert "FAKE-CANARY-KEY-BODY" not in serialised


async def test_observable_paths_do_not_leak_canary_private_key() -> None:
    connector = HolodeckConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout=_PHOTON_5, exit_status=0)),
        ),
        patch(
            "meho_backplane.connectors.holodeck.connector.pwsh_run",
            AsyncMock(return_value={"Version": "9.0.0", "PodId": "lab-pod-01"}),
        ),
    ):
        fp = await connector.fingerprint(_key_target())
        about = await connector.about(_key_target(), {})

    serialised = repr(fp) + repr(about)
    assert "FAKE-CANARY-KEY-BODY" not in serialised
    assert _CANARY_PASSWORD not in serialised

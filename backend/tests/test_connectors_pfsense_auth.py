# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the PfSenseConnector skeleton (G3.7-T1 #844).

Coverage matrix (per Task #844 acceptance criteria):

* ``PfSenseConnector`` advertises the registry-v2 triple
  ``("pfsense", "2.7", "pfsense-ssh")``.
* Importing the package registers the connector against the v2
  registry under that triple (and does **not** dual-write to v1).
* :meth:`PfSenseConnector._auth_config` with ``ssh_private_key``
  present → returns ``{username, client_keys}`` dict (key auth).
* :meth:`PfSenseConnector._auth_config` with password-only secret
  (no ``ssh_private_key``) → raises :exc:`ValueError` with a message
  naming the WebGUI break-glass credential. Password auth is never
  attempted.
* :meth:`PfSenseConnector._auth_config` with missing credentials
  → raises :exc:`ValueError`.
* Per-target connection isolation: two distinct targets do not share
  a connection object.
* :func:`parse_pfsense_version` parses the ``/etc/version`` multi-
  line content into version, build, and kernel fields.
* :meth:`PfSenseConnector.fingerprint` returns the canonical
  :class:`FingerprintResult` shape against a mocked ``_run_command``
  seam with valid ``/etc/version`` content.
* :meth:`PfSenseConnector.fingerprint` with unreachable target
  (``_run_command`` raises) → ``reachable=False`` + ``extras["error"]``.
* :meth:`PfSenseConnector.probe` returns the four distinct
  ``ProbeResult.reason`` values from the matching failure modes:
  ``tcp_unreachable``, ``ssh_handshake_failed``, ``auth_failed``,
  ``no_shell_access``.
* :meth:`PfSenseConnector.probe` returns ``ok=True`` when SSH
  connects and ``/etc/version`` returns non-empty stdout.
* :meth:`PfSenseConnector.probe` returns ``no_shell_access`` when
  ``/etc/version`` stdout is empty (console-menu trap detection).
* :meth:`PfSenseConnector.probe` maps :exc:`ValueError` from
  ``_connect`` (missing ssh_private_key) to ``auth_failed``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors import all_connectors_v2
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.connector import parse_pfsense_version
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

# ---------------------------------------------------------------------------
# Environment + registry fixtures
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
    # A Vault KV-v2 path STRING (#2155) — resolved through the stubbed
    # ``_resolve_secret`` seam against the module registry below.
    secret_ref: str
    # The SSH connection pool keys on ``target_cache_key`` (``(tenant_id,
    # id)``); a double missing either field hits ``AttributeError`` at the
    # pool (evoila/meho#1682). ``id`` defaults off ``name`` so distinct
    # targets in one tenant land on distinct pool keys.
    id: str = ""
    tenant_id: str = "00000000-0000-0000-0000-000000000000"

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"id-{self.name}"


#: Path → secret-data registry the autouse ``_vault_secrets`` fixture
#: routes ``SshConnector._resolve_secret`` through. Populated at module
#: definition (for the shared targets below) and by ``_target_with_secret``
#: inside tests; never cleared per-test so module-level targets stay
#: resolvable.
_VAULT_SECRETS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets(_VAULT_SECRETS):
        yield


def _target_with_secret(
    name: str, secret: dict[str, Any], *, host: str | None = None, port: int | None = 22
) -> _StubTarget:
    secret_path = f"meho/testing/pfsense/{name}"
    _VAULT_SECRETS[secret_path] = secret
    return _StubTarget(
        name=name,
        host=host if host is not None else "pfsense.test.invalid",
        port=port,
        secret_ref=secret_path,
    )


_PASSWORD_ONLY_TARGET = _target_with_secret(
    "pfsense-password-only",
    {
        "username": "admin",
        "password": "webgui-breakglass",  # NOSONAR -- test only
    },
)

_NO_CREDS_TARGET = _target_with_secret("pfsense-no-creds", {"username": "admin"})


def _completed_process(stdout: str = "", exit_status: int = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub.

    asyncssh's :class:`SSHCompletedProcess` is a dataclass; the
    handler code only touches ``.stdout`` and ``.exit_status`` so a
    :class:`MagicMock` with those attrs is enough for unit-test
    coverage without bringing in the real asyncssh server fixture.
    """
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


# ---------------------------------------------------------------------------
# Class-level registry v2 metadata + package import registration
# ---------------------------------------------------------------------------


def test_registry_v2_class_attrs() -> None:
    """Class-level attrs match the v2 triple the package registers."""
    assert PfSenseConnector.product == "pfsense"
    assert PfSenseConnector.version == "2.7"
    assert PfSenseConnector.impl_id == "pfsense-ssh"
    assert PfSenseConnector.supported_version_range is None
    assert PfSenseConnector.priority == 0


def test_package_import_registers_v2_entry_only() -> None:
    """Importing the package registers under the v2 triple, no v1 dual-write.

    Drives the registry clear + reload itself so the assertion observes
    the **side-effect of importing the package** rather than a fixture's
    re-registration. Mirrors the pattern in
    :func:`test_connectors_bind9.test_package_import_registers_v2_entry_only`.
    """
    import meho_backplane.connectors.pfsense as pfsense_pkg

    clear_registry()
    importlib.reload(pfsense_pkg)

    v2 = all_connectors_v2()
    assert v2[("pfsense", "2.7", "pfsense-ssh")] is PfSenseConnector
    # G0.15-T6 (#1215) wildcard fanout -- the sibling ``("pfsense", "",
    # "")`` registration keeps a fresh target with ``version=None``
    # resolvable to ``PfSenseConnector``. The wildcard lands via
    # :func:`register_connector_v2` directly, not the v1 dual-write
    # surface, so pfsense still has no v1 chassis history.
    assert v2[("pfsense", "", "")] is PfSenseConnector


def test_pfsense_connector_registered_under_v2_triple() -> None:
    """PfSenseConnector package registers under (pfsense, 2.7, pfsense-ssh).

    Re-registers manually (autouse _clean_registry cleared the table).
    Asserts the triple resolves to the correct class. Mirrors the
    ``test_connectors_registry_v2.test_sddc_manager_connector_registered_under_v2_triple``
    pattern so the assertion lives both in this module and is visible
    to the registry_v2 test suite when the new triple is added there.
    """
    clear_registry()
    register_connector_v2(
        product=PfSenseConnector.product,
        version=PfSenseConnector.version,
        impl_id=PfSenseConnector.impl_id,
        cls=PfSenseConnector,
    )
    snapshot = all_connectors_v2()
    key = ("pfsense", "2.7", "pfsense-ssh")
    assert key in snapshot
    assert snapshot[key] is PfSenseConnector


# ---------------------------------------------------------------------------
# _auth_config -- key auth path + password-rejection + missing-creds error
# ---------------------------------------------------------------------------


async def test_auth_config_key_auth_returns_client_keys() -> None:
    """``ssh_private_key`` present → ``{username, client_keys=[key]}``."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    pem = private_key.export_private_key().decode()

    target = _target_with_secret("pfsense-key", {"username": "admin", "ssh_private_key": pem})
    connector = PfSenseConnector()
    auth = await connector._auth_config(target)
    assert auth["username"] == "admin"
    assert "client_keys" in auth
    assert len(auth["client_keys"]) == 1
    assert "password" not in auth


async def test_auth_config_password_only_raises_with_webgui_message() -> None:
    """Password-only secret → ``ValueError`` naming the WebGUI break-glass.

    The message must explicitly mention the WebGUI credential so
    the operator knows the ``password`` field is the break-glass
    credential and not a valid SSH auth method for this connector.
    """
    connector = PfSenseConnector()
    with pytest.raises(ValueError, match="WebGUI") as exc_info:
        await connector._auth_config(_PASSWORD_ONLY_TARGET)

    # Confirm it also mentions the key auth requirement.
    assert "ssh_private_key" in str(exc_info.value)
    # Confirm no password auth is attempted -- the exception fires before
    # any connection attempt, so there is no mock interaction to assert.


async def test_auth_config_missing_credentials_raises_value_error() -> None:
    """No ``ssh_private_key`` and no ``password`` → ``ValueError``."""
    connector = PfSenseConnector()
    with pytest.raises(ValueError, match="ssh_private_key"):
        await connector._auth_config(_NO_CREDS_TARGET)


async def test_auth_config_defaults_username_to_admin() -> None:
    """When ``username`` is absent, defaults to ``"admin"``."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    pem = private_key.export_private_key().decode()

    target = _target_with_secret(
        "pfsense-no-user", {"ssh_private_key": pem}, port=None
    )  # no "username" in the secret
    connector = PfSenseConnector()
    auth = await connector._auth_config(target)
    assert auth["username"] == "admin"


# ---------------------------------------------------------------------------
# parse_pfsense_version -- canonical version parsing
# ---------------------------------------------------------------------------


def test_parse_pfsense_version_full_three_line_output() -> None:
    """Full ``/etc/version`` content parses all three fields."""
    content = (
        "2.7.2-RELEASE (amd64)\n"
        "built on Fri Jan 12 18:00:00 UTC 2024\n"
        "FreeBSD 14.1-RELEASE-p5 #1 releng/14.1+n267679-c6d9a4dc7d2(HEAD)"
    )
    result = parse_pfsense_version(content)
    assert result["version"] == "2.7.2-RELEASE"
    assert result["build"] == "2.7.2-RELEASE (amd64)"
    assert result["kernel"] == "FreeBSD 14.1-RELEASE-p5"


def test_parse_pfsense_version_returns_none_on_empty_content() -> None:
    """Empty content → all fields are ``None``."""
    result = parse_pfsense_version("")
    assert result["version"] is None
    assert result["build"] is None
    assert result["kernel"] is None


def test_parse_pfsense_version_whitespace_only_returns_none() -> None:
    """Whitespace-only content → all fields are ``None``."""
    result = parse_pfsense_version("   \n  \n")
    assert result["version"] is None


def test_parse_pfsense_version_no_kernel_line() -> None:
    """Single-line content without FreeBSD line → kernel is ``None``."""
    result = parse_pfsense_version("2.7.2-RELEASE (amd64)\n")
    assert result["version"] == "2.7.2-RELEASE"
    assert result["kernel"] is None


def test_parse_pfsense_version_older_release_format() -> None:
    """pfSense 2.6.x style line (two-digit minor) parses correctly."""
    content = "2.6.0-RELEASE (amd64)\nbuilt on Wed Jan 26 09:21:49 UTC 2022\nFreeBSD 12.3-STABLE"
    result = parse_pfsense_version(content)
    assert result["version"] == "2.6.0-RELEASE"
    assert result["kernel"] == "FreeBSD 12.3-STABLE"


# ---------------------------------------------------------------------------
# fingerprint -- canonical shape (AC #3)
# ---------------------------------------------------------------------------

_PFSENSE_VERSION_CONTENT = (
    "2.7.2-RELEASE (amd64)\n"
    "built on Fri Jan 12 18:00:00 UTC 2024\n"
    "FreeBSD 14.1-RELEASE-p5 #1 releng/14.1"
)


async def test_fingerprint_parses_canonical_etc_version() -> None:
    """``/etc/version`` with version+build+kernel → canonical FingerprintResult."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    pem = private_key.export_private_key().decode()
    target = _target_with_secret("pfsense-fp", {"username": "admin", "ssh_private_key": pem})

    connector = PfSenseConnector()
    run_mock = AsyncMock(
        return_value=_completed_process(stdout=_PFSENSE_VERSION_CONTENT, exit_status=0)
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.fingerprint(target)

    assert result.vendor == "netgate"
    assert result.product == "pfsense"
    assert result.version == "2.7.2-RELEASE"
    assert result.build == "2.7.2-RELEASE (amd64)"
    assert result.reachable is True
    assert result.probe_method == "ssh: cat /etc/version"
    assert result.extras.get("kernel") == "FreeBSD 14.1-RELEASE-p5"


async def test_fingerprint_unreachable_on_os_error() -> None:
    """``OSError`` from ``_run_command`` → ``reachable=False`` + ``extras["error"]``."""
    target = _target_with_secret(
        "pfsense-unreachable",
        {"username": "admin", "ssh_private_key": "fake"},
        host="dead.test.invalid",
    )
    connector = PfSenseConnector()
    with patch.object(
        connector,
        "_run_command",
        AsyncMock(side_effect=OSError("Connection refused")),
    ):
        result = await connector.fingerprint(target)

    assert result.reachable is False
    assert result.vendor == "netgate"
    assert result.product == "pfsense"
    assert "error" in result.extras
    assert "Connection refused" in result.extras["error"]


async def test_fingerprint_unreachable_on_ssh_error() -> None:
    """``asyncssh.Error`` from ``_run_command`` → ``reachable=False``."""
    target = _target_with_secret(
        "pfsense-ssh-error",
        {"username": "admin", "ssh_private_key": "fake"},
        host="broken.test.invalid",
    )
    connector = PfSenseConnector()
    perm_denied = asyncssh.PermissionDenied(reason="publickey auth failed")
    with patch.object(
        connector,
        "_run_command",
        AsyncMock(side_effect=perm_denied),
    ):
        result = await connector.fingerprint(target)

    assert result.reachable is False
    assert "error" in result.extras


# ---------------------------------------------------------------------------
# probe -- four failure modes + success + console-menu trap (AC #4)
# ---------------------------------------------------------------------------


async def test_probe_tcp_unreachable_when_connect_raises_oserror() -> None:
    """``OSError`` from ``_connect`` → reason=``tcp_unreachable``."""
    connector = PfSenseConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("Connection refused"))):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "tcp_unreachable"


async def test_probe_ssh_handshake_failed_when_disconnect_error_raised() -> None:
    """``asyncssh.DisconnectError`` → reason=``ssh_handshake_failed``."""
    connector = PfSenseConnector()
    boom = asyncssh.DisconnectError(asyncssh.DISC_PROTOCOL_ERROR, "fake")
    with patch.object(connector, "_connect", AsyncMock(side_effect=boom)):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "ssh_handshake_failed"


async def test_probe_auth_failed_when_permission_denied_raised() -> None:
    """``asyncssh.PermissionDenied`` → reason=``auth_failed``."""
    connector = PfSenseConnector()
    with patch.object(
        connector,
        "_connect",
        AsyncMock(side_effect=asyncssh.PermissionDenied(reason="publickey")),
    ):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "auth_failed"


async def test_probe_auth_failed_when_missing_key_raises_value_error() -> None:
    """``ValueError`` from ``_connect`` (missing ``ssh_private_key``) → ``auth_failed``.

    The ``_auth_config`` override raises :exc:`ValueError` before any
    TCP connection is opened when ``ssh_private_key`` is absent. The
    probe must map this to ``auth_failed`` rather than letting it
    propagate as an unhandled exception, because the operator-facing
    semantics are "auth configuration is wrong" not "unexpected error".
    """
    connector = PfSenseConnector()
    with patch.object(
        connector,
        "_connect",
        AsyncMock(side_effect=ValueError("pfSense connector requires ssh_private_key")),
    ):
        result = await connector.probe(_PASSWORD_ONLY_TARGET)
    assert result.ok is False
    assert result.reason == "auth_failed"


async def test_probe_no_shell_access_when_version_stdout_empty() -> None:
    """Empty ``/etc/version`` stdout → reason=``no_shell_access``.

    This is the console-menu trap: the admin SSH session landed in
    the interactive pfSense console menu and the command was
    swallowed. An empty stdout (even with exit_status=0) must map
    to ``no_shell_access`` rather than a generic failure.
    """
    connector = PfSenseConnector()
    conn_mock = MagicMock()
    proc_mock = _completed_process(stdout="", exit_status=0)  # empty!
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=conn_mock)),
        patch.object(connector, "_run_command", AsyncMock(return_value=proc_mock)),
    ):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "no_shell_access"


async def test_probe_no_shell_access_when_version_exits_nonzero() -> None:
    """Non-zero exit from ``/etc/version`` → reason=``no_shell_access``.

    A non-zero exit_status (file not found, permission denied) also
    signals a broken shell environment.
    """
    connector = PfSenseConnector()
    conn_mock = MagicMock()
    proc_mock = _completed_process(stdout="", exit_status=1)
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=conn_mock)),
        patch.object(connector, "_run_command", AsyncMock(return_value=proc_mock)),
    ):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "no_shell_access"


async def test_probe_ok_when_ssh_connects_and_version_returns_content() -> None:
    """SSH connects + non-empty ``/etc/version`` → ``ok=True``."""
    connector = PfSenseConnector()
    conn_mock = MagicMock()
    proc_mock = _completed_process(stdout=_PFSENSE_VERSION_CONTENT, exit_status=0)
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=conn_mock)),
        patch.object(connector, "_run_command", AsyncMock(return_value=proc_mock)),
    ):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is True
    assert result.reason is None
    assert result.latency_ms is not None
    assert result.latency_ms >= 0.0


@pytest.mark.parametrize(
    "boom",
    [
        OSError("connection reset by peer"),
        asyncssh.ConnectionLost("channel closed mid-command"),
        TimeoutError("cat /etc/version timed out"),
    ],
)
async def test_probe_command_failed_when_run_command_raises_after_connect(
    boom: Exception,
) -> None:
    """``_run_command`` raising after a successful ``_connect`` → ``command_failed``.

    AC (#986): a mid-probe failure (connection drop, ``asyncssh.Error``,
    or timeout after the handshake) must map to a non-ok
    :class:`ProbeResult` with reason ``command_failed`` — no exception
    escapes ``probe``. ``TimeoutError`` is an ``OSError`` subclass so the
    ``(OSError, asyncssh.Error)`` catch tuple covers the timeout case.
    """
    connector = PfSenseConnector()
    conn_mock = MagicMock()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=conn_mock)),
        patch.object(connector, "_run_command", AsyncMock(side_effect=boom)),
    ):
        result = await connector.probe(_NO_CREDS_TARGET)
    assert result.ok is False
    assert result.reason == "command_failed"


# ---------------------------------------------------------------------------
# about -- surfaces unreachability rather than masking it as status="ok" (#986)
# ---------------------------------------------------------------------------


async def test_about_raises_connector_unreachable_on_unreachable_target() -> None:
    """``about`` against an unreachable target raises, never returns empty fields.

    AC (#986): ``fingerprint`` returns ``reachable=False`` on a connection
    failure; ``about`` must surface that as a
    :exc:`ConnectorUnreachableError` rather than returning a dict of
    empty/None identity fields the dispatcher would report as
    ``status="ok"``.
    """
    from meho_backplane.connectors.adapters.ssh import ConnectorUnreachableError

    connector = PfSenseConnector()
    with (
        patch.object(
            connector,
            "_run_command",
            AsyncMock(side_effect=OSError("Connection refused")),
        ),
        pytest.raises(ConnectorUnreachableError, match="Connection refused"),
    ):
        await connector.about(_NO_CREDS_TARGET, {})


async def test_execute_about_unreachable_returns_connector_error_not_ok() -> None:
    """End-to-end: ``execute("pfsense.about")`` on an unreachable target → non-ok.

    The dispatcher shim catches the ``ConnectorUnreachableError`` raised
    by ``about`` and maps it to the ``connector_error`` envelope. The op
    is reported as ``status="error"`` — not a successful op carrying empty
    identity fields (#986).
    """
    from meho_backplane.connectors.schemas import OperationResult
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await PfSenseConnector.register_operations()

    connector = PfSenseConnector()
    with patch.object(
        connector,
        "_run_command",
        AsyncMock(side_effect=OSError("Connection refused")),
    ):
        result = await connector.execute(_NO_CREDS_TARGET, "pfsense.about", {})

    assert isinstance(result, OperationResult)
    assert result.status == "error"
    assert result.status != "ok"
    assert result.error is not None and result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "ConnectorUnreachableError"


# ---------------------------------------------------------------------------
# Per-target isolation
# ---------------------------------------------------------------------------


async def test_per_target_connection_isolation() -> None:
    """Distinct targets get distinct connections -- pool keyed by ``(tenant_id, id)``."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    pem = private_key.export_private_key().decode()

    target_a = _target_with_secret(
        "pfsense-a", {"username": "admin", "ssh_private_key": pem}, host="pfsense-a.test.invalid"
    )
    target_b = _target_with_secret(
        "pfsense-b", {"username": "admin", "ssh_private_key": pem}, host="pfsense-b.test.invalid"
    )

    conn_a = MagicMock(name="conn_a")
    conn_a.is_closed.return_value = False
    conn_b = MagicMock(name="conn_b")
    conn_b.is_closed.return_value = False

    call_count = 0
    connections = [conn_a, conn_b]

    async def _fake_connect_call(host: str, **_kwargs: Any) -> Any:
        nonlocal call_count
        conn = connections[call_count]
        call_count += 1
        return conn

    connector = PfSenseConnector()
    with (
        patch("asyncssh.connect", side_effect=_fake_connect_call),
        # _auth_config raises without a real key; patch it to return
        # key-auth kwargs so _connect proceeds to asyncssh.connect.
        patch.object(
            connector,
            "_auth_config",
            AsyncMock(return_value={"username": "admin", "client_keys": []}),
        ),
    ):
        got_a = await connector._connect(target_a)
        got_b = await connector._connect(target_b)

    assert got_a is conn_a
    assert got_b is conn_b
    assert got_a is not got_b


# ---------------------------------------------------------------------------
# PFSENSE_OPS sanity check
# ---------------------------------------------------------------------------


def test_pfsense_ops_contains_about_canary() -> None:
    """``PFSENSE_OPS`` must contain at least the ``pfsense.about`` canary op."""
    op_ids = {op.op_id for op in PFSENSE_OPS}
    assert "pfsense.about" in op_ids


def test_pfsense_ops_about_handler_exists_on_class() -> None:
    """``pfsense.about`` ``handler_attr`` resolves on ``PfSenseConnector``."""
    for op in PFSENSE_OPS:
        assert hasattr(PfSenseConnector, op.handler_attr), (
            f"op {op.op_id!r} declares handler_attr={op.handler_attr!r} "
            f"but PfSenseConnector has no such attribute"
        )

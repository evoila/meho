# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Bind9Connector skeleton (G3.4-T1 #587).

Coverage matrix (per Task #587 acceptance criteria):

* ``Bind9Connector`` advertises the registry-v2 triple
  ``("bind9", "9.x", "bind9-ssh")``.
* Importing the package registers the connector against the v2
  registry under that triple (and does **not** dual-write to v1).
* :meth:`Bind9Connector._remote_bash_with_sudo` is the only sudo
  shell-construction path in the connector layer; the constructed
  remote argv is the fixed ``"sudo -S -p '' bash -s"`` string and
  the sudo password is streamed via ``input=``, never appearing in
  the argv, the bound log fields, or any other observable.
* :func:`parse_named_version` recovers the ``<X.Y.Z>`` triple from
  the ``named -v`` banner shapes ISC ships on Debian + RHEL.
* :meth:`Bind9Connector.fingerprint` returns the canonical
  :class:`FingerprintResult` shape against a mocked
  ``_run_command`` seam.
* :meth:`Bind9Connector.probe` returns the five distinct
  ``ProbeResult.reason`` values from the matching failure modes.
* :meth:`Bind9Connector.register_operations` upserts one descriptor
  row per entry in :data:`BIND9_OPS` and is idempotent.
* :meth:`Bind9Connector.execute` after registration:
    * Unknown op_id -> ``unknown_op`` envelope.
    * Known op_id with valid params -> handler invoked, result wrapped.
    * Known op_id with invalid params -> ``invalid_params`` envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors import all_connectors_v2
from meho_backplane.connectors.bind9 import BIND9_OPS, Bind9Connector
from meho_backplane.connectors.bind9.connector import (
    _SUDO_BASH_REMOTE_CMD,
    parse_named_version,
    parse_os_release,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.settings import get_settings

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


@pytest.fixture
def _bind9_registered() -> Iterator[None]:
    """Opt-in fixture: ensure ``Bind9Connector`` is in the v2 registry.

    **Not autouse.** A previous version made this autouse so registry-
    shaped assertions saw the production state regardless of test
    ordering -- but that defeated
    :func:`test_package_import_registers_v2_entry_only`: the autouse
    body called :func:`clear_registry` and then
    :func:`register_connector_v2` itself, so the test would pass even
    if ``connectors/bind9/__init__.py`` never registered the connector.
    The registration test now drives its own clear + reload to observe
    the import side-effect (see that test for the canonical pattern).

    The remaining tests in this module exercise :class:`Bind9Connector`
    directly (``Bind9Connector()`` plus mocked seams) -- they read the
    descriptor row via SQL, not the in-memory registry -- so neither
    autouse re-registration nor this opt-in fixture is required. The
    fixture is retained for any future test that *does* need the v2
    registry populated and is not itself testing the import side-effect.
    """
    clear_registry()
    register_connector_v2(
        product="bind9",
        version="9.x",
        impl_id="bind9-ssh",
        cls=Bind9Connector,
    )
    yield


# ---------------------------------------------------------------------------
# Target + SSHCompletedProcess stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]


_TARGET = _StubTarget(
    name="vcf-router-bind9",
    host="bind9.test.invalid",
    port=22,
    secret_ref={"username": "root", "password": "irrelevant-for-mocked-tests"},  # NOSONAR
)


def _completed_process(stdout: str = "", exit_status: int = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub.

    asyncssh's :class:`SSHCompletedProcess` is a dataclass; the
    handler code only touches ``.stdout`` and ``.exit_status`` so a
    :class:`MagicMock` with those attrs is enough for unit-test
    coverage without bringing in the real asyncssh server fixture
    the adapter suite uses.
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
    assert Bind9Connector.product == "bind9"
    assert Bind9Connector.version == "9.x"
    assert Bind9Connector.impl_id == "bind9-ssh"
    assert Bind9Connector.supported_version_range is None
    assert Bind9Connector.priority == 0


def test_package_import_registers_v2_entry_only() -> None:
    """Importing the package registers under the v2 triple, no v1 dual-write.

    Drives the registry clear + reload itself so the assertion observes
    the **side-effect of importing the package** rather than a fixture's
    re-registration. A previous incarnation of this test ran behind an
    autouse fixture that pre-populated the registry; the test would
    pass even if ``connectors/bind9/__init__.py`` no longer called
    ``register_connector_v2``. The reload pattern matches
    :func:`tests.test_connectors_vmware_rest_composites_register.test_importing_vmware_rest_subpackage_queues_composite_registrar`
    (#509) for the same reason.
    """
    import importlib

    import meho_backplane.connectors.bind9 as bind9_pkg

    clear_registry()
    # Force the module-top-level ``register_connector_v2`` call in
    # ``bind9/__init__.py`` to fire under the cleared registry. The
    # ``Bind9Connector`` class object is owned by
    # ``connectors.bind9.connector`` which is *not* reloaded here, so
    # the post-reload registry entry points at the same class object
    # the rest of this module imports at the top.
    importlib.reload(bind9_pkg)

    v2 = all_connectors_v2()
    assert v2[("bind9", "9.x", "bind9-ssh")] is Bind9Connector
    # Bind9 has no v1 chassis history (see ``__init__.py`` docstring);
    # the v1 ``register_connector`` write is intentionally absent.
    # ``register_connector`` would also dual-write a ``(product, "",
    # "")`` v2 entry; that key must not be present.
    assert ("bind9", "", "") not in v2


# ---------------------------------------------------------------------------
# parse_named_version + parse_os_release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "banner, expected",
    [
        # AC #4: the canonical Debian 12 banner pinned in the issue
        # body must parse to 9.18.24.
        (
            "BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>",
            "9.18.24",
        ),
        # Older Debian 11 banner.
        ("BIND 9.16.50-Debian (Extended Support Version) <id:>", "9.16.50"),
        # RHEL-shaped banner (the Initiative covers 9.x across distros).
        ("BIND 9.18.28-rh (Extended Support Version)", "9.18.28"),
        # Stable Release 9.20.
        ("BIND 9.20.4-1+deb13u1-Debian (Stable Release)", "9.20.4"),
        # Tab-and-extra-whitespace shape -- the regex's ``\s+`` covers it.
        ("BIND\t9.18.0\t(release)", "9.18.0"),
    ],
)
def test_parse_named_version_recovers_triple(banner: str, expected: str) -> None:
    assert parse_named_version(banner) == expected


def test_parse_named_version_returns_none_on_garbage() -> None:
    assert parse_named_version("") is None
    assert parse_named_version("unrelated text") is None
    # Two-component version with no patch -- the regex demands all three.
    assert parse_named_version("BIND 9.18") is None


def test_parse_os_release_identifies_debian_12() -> None:
    content = (
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
        "NAME=Debian GNU/Linux\n"
        'VERSION_ID="12"\n'
        "ID=debian\n"
    )
    assert parse_os_release(content) == "debian 12"


def test_parse_os_release_falls_back_to_id_when_version_missing() -> None:
    content = "ID=arch\nNAME=Arch Linux\n"
    assert parse_os_release(content) == "arch"


def test_parse_os_release_returns_none_on_no_id() -> None:
    assert parse_os_release("") is None
    assert parse_os_release("NAME=Whatever\nFOO=bar\n") is None


# ---------------------------------------------------------------------------
# _remote_bash_with_sudo -- safety primitive (AC #2 + #3)
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_ssh_conn() -> MagicMock:
    """Build a :class:`MagicMock` standing in for an asyncssh SSH connection.

    Tests that exercise :meth:`_remote_bash_with_sudo` use this stub
    to bypass the real ``asyncssh.connect`` plumbing. ``conn.run`` is
    the load-bearing method -- the test assertions read its call args
    to prove the password never lands in argv and only in stdin.
    """
    conn = MagicMock()
    conn.is_closed.return_value = False
    conn.run = AsyncMock(return_value=_completed_process(stdout="", exit_status=0))
    return conn


async def test_remote_bash_with_sudo_uses_fixed_argv_and_streams_password_via_stdin(
    _mock_ssh_conn: MagicMock,
) -> None:
    """The constructed argv is the constant string; the password is on stdin."""
    connector = Bind9Connector()
    with patch.object(connector, "_connect", AsyncMock(return_value=_mock_ssh_conn)):
        result = await connector._remote_bash_with_sudo(
            _TARGET,
            "echo body-of-script",
            raw_jwt="jwt",
            sudo_password="super-secret-password",  # NOSONAR
        )
    assert result.exit_status == 0

    # ``conn.run`` was called with the fixed argv string -- no
    # caller-supplied substring lives in the remote command.
    args, kwargs = _mock_ssh_conn.run.call_args
    assert args[0] == _SUDO_BASH_REMOTE_CMD
    assert args[0] == "sudo -S -p '' bash -s"

    # The password must not appear anywhere in the constructed argv.
    assert "super-secret-password" not in args[0]
    # The script body must not appear in the argv either.
    assert "echo body-of-script" not in args[0]

    # The stdin payload streams the password as line 1 and the script
    # as line 2. The fixed-line-position contract is what makes the
    # caller unable to mis-order the payload.
    stdin_payload = kwargs["input"]
    lines = stdin_payload.split("\n")
    assert lines[0] == "super-secret-password"  # password is line 1
    assert lines[1] == "echo body-of-script"  # script body is line 2

    # ``check=False`` -- the helper never raises ProcessError on
    # non-zero exit; callers decide.
    assert kwargs["check"] is False


@pytest.mark.parametrize(
    "injection_shape, char_name",
    [
        ("password\nrm -rf /\n", "newline"),
        ("password\rsmuggled-line", "carriage-return"),
        ("password\x00trailing-nul", "nul"),
    ],
)
async def test_remote_bash_with_sudo_rejects_multiline_password(
    injection_shape: str,
    char_name: str,
) -> None:
    """Control chars in ``sudo_password`` must raise before any wire IO.

    The helper's safety contract is "password is exactly stdin line 1,
    bash reads line 2 onwards as the script". A password containing
    ``\\n`` / ``\\r`` / ``\\x00`` breaks that invariant by terminating
    sudo's read early and feeding the trailing bytes to ``bash -s``
    as commands. The validation runs before :meth:`_connect` -- the
    test asserts a ``ValueError`` is raised without an SSH connection
    being attempted.
    """
    connector = Bind9Connector()
    connect_mock = AsyncMock()
    with (
        patch.object(connector, "_connect", connect_mock),
        pytest.raises(ValueError, match="single line"),
    ):
        await connector._remote_bash_with_sudo(
            _TARGET,
            "echo body",
            raw_jwt="jwt",
            sudo_password=injection_shape,
        )
    # Defense-in-depth: validation must run before any wire IO, so the
    # SSH connection mock was never invoked. ``char_name`` distinguishes
    # the parametrise case in test output on failure.
    assert not connect_mock.called, (
        f"{char_name} injection: _connect was called before sudo_password validation"
    )


async def test_remote_bash_with_sudo_does_not_log_password(
    _mock_ssh_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The structured log event records cmd_len/exit_code, not the password."""
    connector = Bind9Connector()
    secret = "leak-detector-canary-1234"  # NOSONAR
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=_mock_ssh_conn)),
        caplog.at_level(logging.INFO),
    ):
        await connector._remote_bash_with_sudo(
            _TARGET,
            "rndc reload",
            raw_jwt="jwt",
            sudo_password=secret,
        )

    # No log record (formatted text or structured event KV) carries
    # the canary. The structured-logger structlog routes through the
    # stdlib logging machinery, so caplog's records are the union of
    # both surfaces. Cover both the rendered ``message`` and the per-
    # record attribute dict so a future log-shape refactor cannot
    # accidentally route the password through a different field.
    for record in caplog.records:
        rendered = record.getMessage()
        assert secret not in rendered
        for key, value in record.__dict__.items():
            assert secret not in str(value), f"sudo password leaked into log record attr {key!r}"


async def test_remote_bash_with_sudo_does_not_log_script_body(
    _mock_ssh_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The script body is not logged either -- only its length goes into the event."""
    connector = Bind9Connector()
    script = "rndc-reload-canary; cat /etc/bind/named.conf; echo done"
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=_mock_ssh_conn)),
        caplog.at_level(logging.INFO),
    ):
        await connector._remote_bash_with_sudo(
            _TARGET,
            script,
            raw_jwt="jwt",
            sudo_password="pwd",  # NOSONAR
        )

    for record in caplog.records:
        rendered = record.getMessage()
        assert script not in rendered


def test_remote_bash_with_sudo_signature_makes_misordering_unrepresentable() -> None:
    """The API shape forces ``sudo_password`` keyword-only.

    Acceptance gate for AC #2: the safety primitive's signature must
    not allow a caller to pass ``sudo_password`` positionally in a
    position that could be transposed with ``script``. Inspecting the
    parameter kinds proves the contract.
    """
    import inspect

    sig = inspect.signature(Bind9Connector._remote_bash_with_sudo)
    params = sig.parameters
    # ``script`` is positional-or-keyword and second after ``target``.
    assert params["script"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    # ``sudo_password`` is keyword-only -- the leading ``*,`` enforces
    # this. A caller cannot accidentally swap script and password.
    assert params["sudo_password"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["raw_jwt"].kind == inspect.Parameter.KEYWORD_ONLY


def test_sudo_is_only_referenced_via_the_safe_primitive() -> None:
    """AC #2: no other sudo-shell-construction path lives in the connector layer.

    Greps the connector tree for the literal substring ``sudo`` and
    asserts every occurrence is either the safe primitive itself, a
    docstring or comment, or a reference to it -- never an
    ``"sudo ..."`` shell-fragment string constructed elsewhere. A
    failure here means a new sudo path slipped in alongside the
    safe primitive; the operator must fold it back through
    :meth:`_remote_bash_with_sudo`.
    """
    from pathlib import Path

    connectors_root = (
        Path(__file__).resolve().parent.parent / "src" / "meho_backplane" / "connectors"
    )
    # The only file that should construct any ``sudo ...`` shell
    # fragment is bind9/connector.py -- and even there, only the
    # ``_SUDO_BASH_REMOTE_CMD`` constant carries the literal command.
    allowed_files = {
        connectors_root / "bind9" / "connector.py",
        connectors_root / "bind9" / "__init__.py",
        connectors_root / "bind9" / "ops.py",
    }
    offenders: list[str] = []
    for py_file in connectors_root.rglob("*.py"):
        if py_file in allowed_files:
            continue
        # Skip the test seam itself; this assertion is run from
        # ``backend/tests/`` which lives outside ``connectors/``, so
        # the rglob already excludes it. The check is on production
        # source only.
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            # Strip comments; the literal token ``sudo`` in prose docs
            # / module headers is non-load-bearing.
            code = line.split("#", 1)[0]
            if "sudo" in code.lower():
                offenders.append(f"{py_file.relative_to(connectors_root)}:{lineno}: {line!r}")
    assert not offenders, (
        "Found sudo references outside the bind9 safe-primitive surface:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# fingerprint -- canonical shape + version parsing (AC #4)
# ---------------------------------------------------------------------------


async def test_fingerprint_parses_canonical_debian_banner_and_os_release() -> None:
    """``BIND 9.18.24-1+deb12u2-Debian`` -> ``version="9.18.24"``."""
    banner = "BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>"
    os_release = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'

    connector = Bind9Connector()
    # Patch _run_command so its first call returns the banner, second
    # returns the os-release content. side_effect lets us return per-
    # call values without re-entering the mock setup.
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=banner, exit_status=0),
            _completed_process(stdout=os_release, exit_status=0),
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.fingerprint(_TARGET)

    assert result.vendor == "isc"
    assert result.product == "bind9"
    assert result.version == "9.18.24"
    assert result.build is not None
    assert "9.18.24" in result.build
    assert result.reachable is True
    assert result.probe_method == "ssh: named -v"
    assert result.extras.get("os") == "debian 12"
    assert result.extras.get("named_conf_path") == "/etc/bind/named.conf"


async def test_fingerprint_falls_back_to_debian_version_when_os_release_missing() -> None:
    """``/etc/os-release`` missing -> read ``/etc/debian_version``."""
    banner = "BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>"
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=banner, exit_status=0),
            # os-release read fails (file missing -> exit_status != 0)
            _completed_process(stdout="", exit_status=1),
            # debian_version succeeds
            _completed_process(stdout="12.5\n", exit_status=0),
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.fingerprint(_TARGET)

    assert result.extras.get("os") == "debian 12.5"


async def test_fingerprint_returns_none_version_on_unparseable_banner() -> None:
    """No matchable banner -> version is ``None``, build carries the raw text."""
    banner = "(unrecognised output from named -v)"
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=banner, exit_status=0),
            _completed_process(stdout="ID=debian\nVERSION_ID=12\n", exit_status=0),
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.fingerprint(_TARGET)

    assert result.version is None
    assert result.build == banner


# ---------------------------------------------------------------------------
# probe -- five distinct failure-reason shapes (AC #5)
# ---------------------------------------------------------------------------


async def test_probe_tcp_unreachable_when_connect_raises_oserror() -> None:
    connector = Bind9Connector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("Connection refused"))):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "tcp_unreachable"


async def test_probe_ssh_handshake_failed_when_disconnect_error_raised() -> None:
    connector = Bind9Connector()
    boom = asyncssh.DisconnectError(asyncssh.DISC_PROTOCOL_ERROR, "fake")
    with patch.object(connector, "_connect", AsyncMock(side_effect=boom)):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "ssh_handshake_failed"


async def test_probe_auth_failed_when_permission_denied_raised() -> None:
    connector = Bind9Connector()
    boom = asyncssh.PermissionDenied("fake auth failure")
    with patch.object(connector, "_connect", AsyncMock(side_effect=boom)):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "auth_failed"


async def test_probe_named_not_running_when_pgrep_nonzero() -> None:
    connector = Bind9Connector()
    # _connect succeeds (returns a stub); pgrep reports exit 1.
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(exit_status=1)),
        ),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "named_not_running"


async def test_probe_named_config_invalid_when_checkconf_nonzero() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            # pgrep -- named present, exit 0
            _completed_process(exit_status=0),
            # named-checkconf -p -- exit non-zero (config broken)
            _completed_process(exit_status=1),
        ]
    )
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "named_config_invalid"


async def test_probe_ok_when_named_running_and_config_parses() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(exit_status=0),  # pgrep
            _completed_process(exit_status=0),  # named-checkconf -p
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
# register_operations -- upsert + idempotency
# ---------------------------------------------------------------------------


async def test_register_operations_upserts_one_row_per_op() -> None:
    """Each row in :data:`BIND9_OPS` lands in ``endpoint_descriptor``."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations import typed_register as tr_module

    # Patch the embedding-encode helper so the test does not touch
    # fastembed (the K8s dispatcher-shim test seam reused here).
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await Bind9Connector.register_operations()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "bind9",
                EndpointDescriptor.version == "9.x",
                EndpointDescriptor.impl_id == "bind9-ssh",
            )
        )
        rows = result.scalars().all()

    assert len(rows) == len(BIND9_OPS)
    op_ids = {row.op_id for row in rows}
    assert op_ids == {op.op_id for op in BIND9_OPS}

    about_row = next(r for r in rows if r.op_id == "bind9.about")
    assert about_row.source_kind == "typed"
    assert about_row.tenant_id is None
    assert about_row.handler_ref == (
        "meho_backplane.connectors.bind9.connector.Bind9Connector.about"
    )
    assert about_row.safety_level == "safe"
    assert about_row.requires_approval is False


async def test_register_operations_is_idempotent_on_re_call() -> None:
    """Second invocation skips re-embedding when op text is unchanged."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations import typed_register as tr_module

    encode_mock = AsyncMock(return_value=[0.1] * 384)
    with patch.object(tr_module, "encode_endpoint_text", encode_mock):
        await Bind9Connector.register_operations()
        first_count = encode_mock.call_count
        await Bind9Connector.register_operations()
        second_count = encode_mock.call_count

    # First call computes one embedding per op; second hits the
    # body-hash skip-re-embed branch and computes zero new embeddings.
    assert first_count == len(BIND9_OPS)
    assert second_count == first_count

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "bind9",
                EndpointDescriptor.version == "9.x",
                EndpointDescriptor.impl_id == "bind9-ssh",
            )
        )
        rows = result.scalars().all()
    assert len(rows) == len(BIND9_OPS)


# ---------------------------------------------------------------------------
# execute() shim -- unknown / valid / invalid (AC #6)
# ---------------------------------------------------------------------------


async def test_execute_unknown_op_returns_dispatcher_unknown_op_envelope() -> None:
    """Op_id with no descriptor row hits :func:`result_unknown_op`."""
    connector = Bind9Connector()
    result = await connector.execute(_TARGET, "bind9.totally.unregistered", {})
    assert isinstance(result, OperationResult)
    assert result.status == "error"
    assert result.op_id == "bind9.totally.unregistered"
    assert result.error is not None and result.error.startswith("unknown_op:")
    assert result.extras.get("error_code") == "unknown_op"
    assert isinstance(result.extras.get("known_op_count"), int)


async def test_execute_about_dispatches_through_descriptor_and_returns_ok() -> None:
    """``bind9.about`` registered -> ``execute`` resolves handler + returns ``ok``."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await Bind9Connector.register_operations()

    connector = Bind9Connector()
    banner = "BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>"
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=banner, exit_status=0),
            _completed_process(stdout="ID=debian\nVERSION_ID=12\n", exit_status=0),
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.execute(_TARGET, "bind9.about", {})

    assert isinstance(result, OperationResult)
    assert result.status == "ok"
    assert result.op_id == "bind9.about"
    assert result.error is None
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["vendor"] == "isc"
    assert payload["product"] == "bind9"
    assert payload["version"] == "9.18.24"
    assert payload["os"] == "debian 12"


async def test_execute_invalid_params_returns_invalid_params_envelope() -> None:
    """Params failing the descriptor's JSON Schema -> ``invalid_params``."""
    from meho_backplane.operations import typed_register as tr_module

    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await Bind9Connector.register_operations()

    connector = Bind9Connector()
    # ``bind9.about`` declares ``additionalProperties: False``; an
    # extra key fails the JSON Schema validator before the handler
    # ever runs.
    result = await connector.execute(_TARGET, "bind9.about", {"unexpected": "key"})
    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"

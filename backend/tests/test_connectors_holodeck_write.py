# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Holodeck approval-gated remediation write ops (G3.18-T2 #2154).

Coverage matrix (per Task #2154 acceptance criteria):

* ``WRITE_OPS`` registration shape -- three ops, all
  ``safety_level='dangerous'`` + ``requires_approval=True``, all carry the
  ``write`` tag and the SSH-only transport note.
* Input bounds (pure, no event loop):
  - ``holodeck.k8s.pods.gc`` -- rejects a named pod / label selector /
    non-terminal phase; accepts only Failed / Succeeded; composes one
    ``kubectl delete pods --field-selector status.phase=<phase>`` per phase.
  - ``holodeck.backups.prune`` -- rejects any path resolving outside
    ``/var/backups/**`` (traversal + absolute escape); honours an explicit
    ``keep_newest`` int.
  - ``holodeck.images.import`` -- rejects any tar path not matching
    ``/root/containerd-images/*.tar``.
* Handler happy-path + rejected-input (mocked ``_run_command``): a bound
  violation fails closed with an error envelope and NO SSH traffic.
* Dispatch park -> approve -> execute -> audit (recorded fake-shell SSH
  harness): an un-approved USER dispatch parks (``awaiting_approval``,
  handler never runs); the ``_approved=True`` resume path runs the handler
  and writes a ``DISPATCH`` audit row.
* shlex-quoting canary: a namespace/path token never reaches the composed
  command unquoted.
"""

from __future__ import annotations

import asyncio
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import jsonschema
import pytest
from sqlalchemy import select

import meho_backplane.connectors.holodeck  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.holodeck import HolodeckConnector
from meho_backplane.connectors.holodeck.ops_write import (
    WRITE_OPS,
    HolodeckWriteSafetyError,
    bound_backup_path,
    bound_image_tar,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

# ---------------------------------------------------------------------------
# Environment fixture
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
# Stub target for the unit-level handler tests
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]


_TARGET = _StubTarget(
    name="holorouter-write-test",
    host="holorouter.test.invalid",
    port=22,
    secret_ref={"username": "root", "password": "write-canary-pw"},  # NOSONAR canary
)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


# ---------------------------------------------------------------------------
# Registration contract (acceptance criterion 1)
# ---------------------------------------------------------------------------

EXPECTED_WRITE_OP_IDS: frozenset[str] = frozenset(
    {"holodeck.k8s.pods.gc", "holodeck.backups.prune", "holodeck.images.import"}
)


def test_write_ops_registration_set() -> None:
    assert {op.op_id for op in WRITE_OPS} == EXPECTED_WRITE_OP_IDS
    assert len(WRITE_OPS) == 3


def test_write_ops_are_dangerous_and_require_approval() -> None:
    for op in WRITE_OPS:
        assert op.safety_level == "dangerous", f"{op.op_id} must be dangerous"
        assert op.requires_approval is True, f"{op.op_id} must require approval"
        assert "write" in op.tags, f"{op.op_id} should carry the write tag"


def test_write_ops_carry_transport_note_and_instructions() -> None:
    for op in WRITE_OPS:
        instr = op.llm_instructions or {}
        assert "Holodeck has no REST API" in instr.get("when_to_use", "")
        assert instr.get("output_shape"), f"{op.op_id} missing output_shape"


# ---------------------------------------------------------------------------
# Bounds -- backups.prune path safety (acceptance criterion 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/var/backups/daily/db.sql.gz",
        "daily/db.sql.gz",
        "/var/backups/a/b/c.tar",
    ],
)
def test_bound_backup_path_accepts_inside_tree(path: str) -> None:
    resolved = bound_backup_path(path)
    assert resolved.startswith("/var/backups/")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/shadow",  # absolute escape
        "../etc/passwd",  # relative traversal
        "/var/backups/../etc/passwd",  # traversal via the prefix
        "daily/../../etc",  # nested traversal
        "/var/backups",  # the root itself, not a child
        "/var/backups/",  # normalises to the root
        "",  # empty
        "   ",  # whitespace-only
    ],
)
def test_bound_backup_path_rejects_escape(path: str) -> None:
    with pytest.raises(HolodeckWriteSafetyError):
        bound_backup_path(path)


def test_bound_backup_path_rejects_non_string() -> None:
    with pytest.raises(HolodeckWriteSafetyError):
        bound_backup_path(None)


# ---------------------------------------------------------------------------
# Bounds -- images.import tar path safety (acceptance criterion 4)
# ---------------------------------------------------------------------------


def test_bound_image_tar_accepts_matching_path() -> None:
    assert bound_image_tar("/root/containerd-images/pause.tar") == (
        "/root/containerd-images/pause.tar"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/x.tar",  # wrong directory
        "/root/containerd-images/sub/x.tar",  # nested
        "/root/containerd-images/../../etc/x.tar",  # traversal
        "/root/containerd-images/x.txt",  # wrong suffix
        "/root/containerd-images/",  # no basename
        "relative.tar",  # not absolute / wrong dir
        "",
    ],
)
def test_bound_image_tar_rejects_out_of_bounds(path: str) -> None:
    with pytest.raises(HolodeckWriteSafetyError):
        bound_image_tar(path)


# ---------------------------------------------------------------------------
# Schema-layer bounds -- pods.gc (acceptance criterion 2)
# ---------------------------------------------------------------------------


def _op(op_id: str) -> Any:
    return next(op for op in WRITE_OPS if op.op_id == op_id)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"phases": ["Failed"]},
        {"phases": ["Failed", "Succeeded"]},
        {"phases": ["Succeeded"], "namespace": "holodeck"},
    ],
)
def test_pods_gc_schema_accepts_valid(params: dict[str, Any]) -> None:
    jsonschema.validate(params, _op("holodeck.k8s.pods.gc").parameter_schema)


@pytest.mark.parametrize(
    "params",
    [
        {"phases": ["Running"]},  # live phase
        {"phases": ["Pending"]},
        {"phases": []},  # empty
        {"namespace": "Bad_NS"},  # not RFC-1123
        {"name": "my-pod"},  # naming a pod -> additionalProperties=False
        {"selector": "app=x"},  # label selector -> additionalProperties=False
    ],
)
def test_pods_gc_schema_rejects_invalid(params: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(params, _op("holodeck.k8s.pods.gc").parameter_schema)


# ---------------------------------------------------------------------------
# Handler-layer -- pods.gc (authoritative gate, mocked SSH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pods_gc_default_deletes_both_terminal_phases() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout='pod "x" deleted\n')
        result = await connector.k8s_pods_gc(_TARGET, {})
    assert result["deleted"] is True
    assert result["phases"] == ["Failed", "Succeeded"]
    cmds = [call.args[1] for call in mock_cmd.await_args_list]
    # shlex.quote leaves 'status.phase=Failed' unquoted (no shell-special
    # chars), which is correct and injection-safe -- the token is a fixed
    # allowlisted selector, not operator input.
    assert cmds == [
        "kubectl delete pods --field-selector status.phase=Failed",
        "kubectl delete pods --field-selector status.phase=Succeeded",
    ]


@pytest.mark.asyncio
async def test_pods_gc_namespace_is_quoted() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc()
        await connector.k8s_pods_gc(_TARGET, {"phases": ["Failed"], "namespace": "holodeck"})
    cmd = mock_cmd.await_args.args[1]
    assert cmd == "kubectl delete pods -n holodeck --field-selector status.phase=Failed"


@pytest.mark.asyncio
async def test_pods_gc_handler_rejects_non_terminal_phase() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.k8s_pods_gc(_TARGET, {"phases": ["Running"]})
        mock_cmd.assert_not_awaited()
    assert result["deleted"] is False
    assert "safety check" in result["error"]


# ---------------------------------------------------------------------------
# Handler-layer -- backups.prune (mocked SSH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backups_prune_happy_path_composes_bounded_command() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="")
        result = await connector.backups_prune(_TARGET, {"keep_newest": 3})
    assert result["pruned"] is True
    assert result["keep_newest"] == 3
    assert result["directory"] == "/var/backups"
    cmd = mock_cmd.await_args.args[1]
    assert cmd.startswith("find /var/backups -maxdepth 1 -type f")
    assert "tail -n +4" in cmd  # keep_newest + 1


@pytest.mark.asyncio
async def test_backups_prune_rejects_out_of_tree_path() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.backups_prune(_TARGET, {"keep_newest": 2, "path": "../etc"})
        mock_cmd.assert_not_awaited()
    assert result["pruned"] is False
    assert "safety check" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "3", 2.5, True, None])
async def test_backups_prune_rejects_bad_keep_newest(bad: Any) -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.backups_prune(_TARGET, {"keep_newest": bad})
        mock_cmd.assert_not_awaited()
    assert result["pruned"] is False


# ---------------------------------------------------------------------------
# Handler-layer -- images.import (mocked SSH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_images_import_happy_path() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="unpacking ... done\n")
        result = await connector.images_import(
            _TARGET, {"tar_path": "/root/containerd-images/pause.tar"}
        )
    assert result["imported"] is True
    cmd = mock_cmd.await_args.args[1]
    assert cmd == "ctr -n k8s.io images import /root/containerd-images/pause.tar"


@pytest.mark.asyncio
async def test_images_import_rejects_bad_tar_path() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.images_import(_TARGET, {"tar_path": "/tmp/evil.tar"})
        mock_cmd.assert_not_awaited()
    assert result["imported"] is False
    assert "safety check" in result["error"]


# ===========================================================================
# Dispatch park -> approve -> execute -> audit (recorded fake-shell SSH)
# ===========================================================================

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()

_CONNECTOR_ID = "holodeck-ssh-9.0"
_TARGET_NAME = "holodeck-write-e2e"
_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000021ba")
_OPERATOR = Operator(
    sub="holodeck-write-e2e-test",
    name="Holodeck Write E2E Operator",
    email=None,
    raw_jwt="<holodeck-write-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

#: The fake-shell returns a canned line for every write command shape the
#: e2e exercises. Keyed by the full command string (all write ops use plain
#: SSH, no pwsh wrapping).
_PLAIN_SSH_FIXTURES: dict[str, str] = {
    "kubectl delete pods --field-selector status.phase=Failed": 'pod "a" deleted\n',
    "kubectl delete pods --field-selector status.phase=Succeeded": 'pod "b" deleted\n',
    "ctr -n k8s.io images import /root/containerd-images/pause.tar": "unpacking done\n",
}


class _FakeShellSSHServer(asyncssh.SSHServer):
    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:  # type: ignore[override]
        return bool(key == _CLIENT_PUB)


async def _fake_shell_process_factory(process: Any) -> None:
    cmd: str = process.command or ""
    response = _PLAIN_SSH_FIXTURES.get(cmd)
    if response is not None:
        process.stdout.write(response)
        process.exit(0)
        return
    # backups.prune composes a find|sort|... pipeline; accept any such shape.
    if cmd.startswith("find /var/backups -maxdepth 1 -type f"):
        process.stdout.write("")
        process.exit(0)
        return
    process.stderr.write(f"fake-shell: unknown command: {cmd!r}\n")
    process.exit(127)


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
async def fake_shell_server() -> AsyncIterator[types.SimpleNamespace]:
    srv = await asyncssh.create_server(
        _FakeShellSSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[_SERVER_KEY],
        process_factory=_fake_shell_process_factory,
    )
    port: int = srv.sockets[0].getsockname()[1]
    yield types.SimpleNamespace(host="127.0.0.1", port=port)
    srv.close()
    await srv.wait_closed()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


async def _seed_target(host: str, port: int) -> TargetORM:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="holodeck",
            host=host,
            port=port,
            fqdn=None,
            secret_ref="kv/dev/holodeck/write-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "9.0.0"},
            notes="seeded by test_connectors_holodeck_write",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> HolodeckConnector:
    class _SeededHolodeckConnector(HolodeckConnector):  # type: ignore[misc]
        async def _auth_config(self, target: Any) -> dict[str, Any]:
            return {"username": "root", "client_keys": [_CLIENT_KEY], "known_hosts": None}

    instance = _SeededHolodeckConnector()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[HolodeckConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture
async def holodeck_write_e2e(
    fake_shell_server: types.SimpleNamespace,
) -> AsyncIterator[HolodeckConnector]:
    set_default_reducer(PassThroughReducer())
    await HolodeckConnector.register_operations()
    await _seed_target(fake_shell_server.host, fake_shell_server.port)
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    """Dispatch a write op. ``approved=False`` hits the policy gate (parks);
    ``approved=True`` is the approvals-API resume path that runs the handler."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


@pytest.mark.asyncio
async def test_write_op_parks_for_user_not_hard_deny(
    holodeck_write_e2e: HolodeckConnector,
) -> None:
    """An un-approved USER dispatch parks in the approve-queue (not denied, not run)."""
    result = await _dispatch("holodeck.k8s.pods.gc", {"phases": ["Failed"]}, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert result.get("extras", {}).get("approval_request_id")


@pytest.mark.asyncio
async def test_pods_gc_approved_executes_and_writes_audit_row(
    holodeck_write_e2e: HolodeckConnector,
) -> None:
    """The _approved=True resume path runs the handler and writes a DISPATCH audit row."""
    op_id = "holodeck.k8s.pods.gc"
    sessionmaker = get_sessionmaker()

    async def _count() -> int:
        async with sessionmaker() as session:
            rows = await session.execute(
                select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
            )
            return len(list(rows.scalars().all()))

    baseline = await _count()
    result = await _dispatch(op_id, {"phases": ["Failed", "Succeeded"]}, approved=True)
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["deleted"] is True
    assert await _count() == baseline + 1

    async with sessionmaker() as session:
        rows = await session.execute(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
        row = rows.scalar_one()
    assert row.target_id is not None
    assert row.payload.get("op_id") == op_id
    assert row.payload.get("params_hash")


@pytest.mark.asyncio
async def test_images_import_approved_executes(
    holodeck_write_e2e: HolodeckConnector,
) -> None:
    result = await _dispatch(
        "holodeck.images.import",
        {"tar_path": "/root/containerd-images/pause.tar"},
        approved=True,
    )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["imported"] is True


@pytest.mark.asyncio
async def test_backups_prune_approved_executes(
    holodeck_write_e2e: HolodeckConnector,
) -> None:
    result = await _dispatch("holodeck.backups.prune", {"keep_newest": 3}, approved=True)
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["pruned"] is True

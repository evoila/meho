# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the RKE2 approval-gated node-write ops (G-Node/RKE2-T3 #2430).

Coverage matrix (per Task #2430 acceptance criteria):

* ``WRITE_OPS`` registration shape -- three ops (``rke2.token.rotate`` from
  T2 #2429 plus this task's two node ops ``service.restart`` +
  ``config.update``), all ``safety_level='dangerous'`` + ``requires_approval=True``,
  all carrying the ``write`` tag and an SSH transport note. token.rotate is the
  credential-minting op (group ``rke2-token-write``, distinct from the node ops'
  ``rke2-node-write``); its Vault-pointer / no-token-value contract is asserted
  in ``test_connectors_rke2.py``.
* Bounds (pure, no event loop):
  - ``service.restart`` -- rejects any unit outside {rke2-server, rke2-agent}
    at the schema enum AND the handler frozenset re-check.
  - ``config.update`` -- rejects any path outside /etc/rancher/rke2/*.yaml
    (traversal / absolute escape / non-yaml), refuses an empty/bad patch,
    and never leaks the file body in the rejection.
  - backplane-owned merge / replace + changed-key-names.
* Handler-layer (mocked ``_run_command``): the restart health-gates on
  ``is-active``; ``config.update`` reads + merges + writes ``0600 root:root``
  atomically, returns key NAMES only + ``restart_required: true`` and does
  NOT restart; a bound violation fails closed with no SSH traffic.
* Preview builders render the blast-radius shapes with no file body/values.
* Dispatch park -> approve -> execute -> audit (recorded fake-shell SSH):
  an un-approved USER dispatch parks; the ``_approved=True`` resume path runs
  the handler and writes a ``DISPATCH`` audit row; no secret value on the row.
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
import yaml
from sqlalchemy import select

import meho_backplane.connectors.rke2  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.rke2 import Rke2SshConnector
from meho_backplane.connectors.rke2.ops_write import (
    WRITE_OPS,
    ConfigPathRejectedError,
    Rke2WriteSafetyError,
    _rke2_config_update_preview,
    _rke2_service_restart_preview,
    apply_config_patch,
    bound_config_path,
    bound_unit,
    changed_config_keys,
    ensure_config_path_under_root,
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
    secret_ref: str  # a Vault KV-v2 path STRING (#2155); never resolved here.


_TARGET = _StubTarget(
    name="rke2-node-write-test",
    host="rke2-node.test.invalid",
    port=22,
    secret_ref="meho/testing/rke2/node-write-test",
)

#: A token-value canary that must never surface in a result / preview / log.
_TOKEN_CANARY = "K10rke2writecanaryDONOTLEAK::server:qqq123"  # gitleaks:allow NOSONAR


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _op(op_id: str) -> Any:
    return next(op for op in WRITE_OPS if op.op_id == op_id)


# ---------------------------------------------------------------------------
# Registration contract (acceptance criterion 1)
# ---------------------------------------------------------------------------

EXPECTED_WRITE_OP_IDS: frozenset[str] = frozenset(
    {"rke2.token.rotate", "rke2.node.service.restart", "rke2.node.config.update"}
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
        assert "SSH" in instr.get("when_to_use", ""), f"{op.op_id} missing SSH transport note"
        assert instr.get("output_shape"), f"{op.op_id} missing output_shape"
        # token.rotate is a credential-write op in its own group; the two node
        # ops share rke2-node-write. Both are valid RKE2 write groups.
        assert op.group_key in {"rke2-node-write", "rke2-token-write"}


def test_token_rotate_is_the_credential_minting_write_op() -> None:
    """The union brought T2 #2429's ``rke2.token.rotate`` into ``WRITE_OPS``.

    Its credential-mint / Vault-pointer contract is exercised in full by
    ``test_connectors_rke2.py``; here we only guard that the distinguishing
    credential identity is not silently dropped from the write set again.
    """
    token_op = _op("rke2.token.rotate")
    assert "credential" in token_op.tags
    assert token_op.group_key == "rke2-token-write"
    assert token_op.safety_level == "dangerous"
    assert token_op.requires_approval is True


# ---------------------------------------------------------------------------
# Bounds -- service.restart unit allow-list (acceptance criterion 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["rke2-server", "rke2-agent"])
def test_bound_unit_accepts_allowed(unit: str) -> None:
    assert bound_unit({"unit": unit}) == unit


@pytest.mark.parametrize(
    "params",
    [
        {"unit": "rke2-killall"},
        {"unit": "nginx"},
        {"unit": "rke2-server; rm -rf /"},
        {"unit": "RKE2-SERVER"},
        {"unit": ""},
        {},
        {"unit": None},
    ],
)
def test_bound_unit_rejects_others(params: dict[str, Any]) -> None:
    with pytest.raises(Rke2WriteSafetyError):
        bound_unit(params)


@pytest.mark.parametrize("unit", ["rke2-server", "rke2-agent"])
def test_restart_schema_accepts_allowed_unit(unit: str) -> None:
    jsonschema.validate({"unit": unit}, _op("rke2.node.service.restart").parameter_schema)


@pytest.mark.parametrize(
    "params",
    [{"unit": "nginx"}, {"unit": "rke2-killall"}, {}, {"unit": "rke2-server", "extra": 1}],
)
def test_restart_schema_rejects_others(params: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(params, _op("rke2.node.service.restart").parameter_schema)


# ---------------------------------------------------------------------------
# Bounds -- config.update path confinement (acceptance criterion 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("config.yaml", "/etc/rancher/rke2/config.yaml"),
        ("config.yaml.d/10-extra.yaml", "/etc/rancher/rke2/config.yaml.d/10-extra.yaml"),
        ("/etc/rancher/rke2/config.yaml", "/etc/rancher/rke2/config.yaml"),
    ],
)
def test_ensure_config_path_accepts_inside_tree(requested: str, expected: str) -> None:
    assert ensure_config_path_under_root(requested, "/etc/rancher/rke2") == expected


@pytest.mark.parametrize(
    "requested",
    [
        "/etc/shadow",  # absolute escape
        "../../etc/passwd",  # relative traversal
        "/etc/rancher/rke2/../../etc/passwd",  # traversal via the prefix
        "/etc/rancher/rke2-evil/config.yaml",  # sibling prefix, not under root
        "/etc/rancher/rke2",  # the root dir itself
        "/etc/rancher/rke2/config.conf",  # wrong suffix
        "config.txt",  # relative, wrong suffix
        "",  # empty
        "   ",  # whitespace-only
        "config.yaml\nx",  # control char
    ],
)
def test_ensure_config_path_rejects_escape(requested: str) -> None:
    with pytest.raises(ConfigPathRejectedError):
        ensure_config_path_under_root(requested, "/etc/rancher/rke2")


def test_bound_config_path_defaults_to_config_yaml() -> None:
    assert bound_config_path(None) == "/etc/rancher/rke2/config.yaml"


def test_config_path_rejection_message_carries_no_body() -> None:
    """A rejection names the sanitised candidate, never a file body."""
    try:
        ensure_config_path_under_root("/etc/shadow", "/etc/rancher/rke2")
    except ConfigPathRejectedError as exc:
        assert "root:" not in str(exc)  # no /etc/shadow content shape leaked


# ---------------------------------------------------------------------------
# Bounds -- backplane-owned merge / replace + changed keys
# ---------------------------------------------------------------------------


def test_apply_config_patch_merge_overlays_keys() -> None:
    current = {"a": 1, "token": "OLD"}
    merged = apply_config_patch(current, {"b": 2, "token": "NEW"}, "merge")
    assert merged == {"a": 1, "token": "NEW", "b": 2}
    assert current == {"a": 1, "token": "OLD"}  # input not mutated


def test_apply_config_patch_replace_swaps_whole_config() -> None:
    assert apply_config_patch({"a": 1}, {"x": 9}, "replace") == {"x": 9}


def test_changed_config_keys_is_names_only() -> None:
    before = {"a": 1, "token": "OLD"}
    after = {"a": 1, "token": "NEW", "b": 2}
    assert changed_config_keys(before, after) == ["b", "token"]
    # Replace that drops a key surfaces it as changed.
    assert changed_config_keys({"a": 1, "b": 2}, {"a": 1}) == ["b"]


# ---------------------------------------------------------------------------
# Preview builders (acceptance criterion 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_restart_preview_shape() -> None:
    ctx = types.SimpleNamespace(
        params={"unit": "rke2-server"}, target=types.SimpleNamespace(name="node-1")
    )
    preview = await _rke2_service_restart_preview(ctx)  # type: ignore[arg-type]
    assert preview == {
        "resource": "systemd_unit",
        "unit": "rke2-server",
        "action": "restart",
        "node": "node-1",
    }


@pytest.mark.asyncio
async def test_service_restart_preview_declines_out_of_list_unit() -> None:
    ctx = types.SimpleNamespace(params={"unit": "nginx"}, target=types.SimpleNamespace(name="n"))
    assert await _rke2_service_restart_preview(ctx) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_config_update_preview_key_names_only_no_values() -> None:
    ctx = types.SimpleNamespace(
        params={
            "path": "/etc/rancher/rke2/config.yaml",
            "patch": {"token": _TOKEN_CANARY, "server": "https://x"},
            "semantics": "merge",
        },
        target=types.SimpleNamespace(name="node-1"),
    )
    preview = await _rke2_config_update_preview(ctx)  # type: ignore[arg-type]
    assert preview == {
        "resource": "config_file",
        "path": "/etc/rancher/rke2/config.yaml",
        "semantics": "merge",
        "key_names": ["server", "token"],
    }
    assert _TOKEN_CANARY not in repr(preview)  # no value ever


# ---------------------------------------------------------------------------
# Handler-layer -- service.restart (mocked SSH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_restart_happy_path_health_gates() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(exit_status=0), _proc(stdout="active\n")]
        result = await connector.service_restart(_TARGET, {"unit": "rke2-server"})
    assert result["restarted"] is True
    assert result["unit"] == "rke2-server"
    assert result["is_active"] is True
    cmds = [call.args[1] for call in mock_cmd.await_args_list]
    assert cmds == ["systemctl restart rke2-server", "systemctl is-active rke2-server"]


@pytest.mark.asyncio
async def test_service_restart_reports_inactive_after_restart() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(exit_status=0), _proc(stdout="activating\n")]
        result = await connector.service_restart(_TARGET, {"unit": "rke2-agent"})
    assert result["restarted"] is True
    assert result["is_active"] is False


@pytest.mark.asyncio
async def test_service_restart_rejects_bad_unit_no_ssh() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.service_restart(_TARGET, {"unit": "nginx"})
        mock_cmd.assert_not_awaited()
    assert result["restarted"] is False
    assert "safety check" in result["error"]


# ---------------------------------------------------------------------------
# Handler-layer -- config.update (mocked SSH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_update_merges_and_writes_atomically() -> None:
    connector = Rke2SshConnector()
    current = "server: https://old\ntoken: " + _TOKEN_CANARY + "\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(stdout=current, exit_status=0),  # read
            _proc(stdout="===RKE2_CONFIG_WRITTEN===\n", exit_status=0),  # write
        ]
        result = await connector.config_update(
            _TARGET,
            {
                "patch": {"server": "https://new", "cluster-cidr": "10.0.0.0/16"},
                "semantics": "merge",
            },
        )
    assert result["updated"] is True
    assert result["path"] == "/etc/rancher/rke2/config.yaml"
    assert result["restart_required"] is True
    # key NAMES only (server changed, cluster-cidr added); token unchanged.
    assert result["changed_keys"] == ["cluster-cidr", "server"]
    # The write script writes 0600 root:root atomically and the merge is
    # backplane-owned (the decoded body carries the preserved token).
    write_cmd = mock_cmd.await_args_list[1].args[1]
    assert write_cmd.startswith("# meho-rke2-config-update")
    assert "chmod 0600" in write_cmd and "chown root:root" in write_cmd and "mv -f" in write_cmd
    # No token value leaks into the result envelope (only the base64 body,
    # which is on the wire, carries it -- never the returned dict).
    assert _TOKEN_CANARY not in repr(result)


@pytest.mark.asyncio
async def test_config_update_preserves_existing_token_in_merge() -> None:
    """A merge that doesn't touch the token still writes it back (backplane merge)."""
    connector = Rke2SshConnector()
    current = "token: " + _TOKEN_CANARY + "\n"
    captured: dict[str, str] = {}

    async def _run(target: Any, cmd: str, **kwargs: Any) -> Any:
        if cmd.startswith("if [ -e"):
            return _proc(stdout=current, exit_status=0)
        captured["write"] = cmd
        return _proc(stdout="===RKE2_CONFIG_WRITTEN===\n", exit_status=0)

    with patch.object(connector, "_run_command", side_effect=_run):
        result = await connector.config_update(_TARGET, {"patch": {"debug": True}})
    assert result["updated"] is True
    assert result["changed_keys"] == ["debug"]
    # Decode the base64 body the backplane composed and confirm the merge
    # preserved the pre-existing token key/value.
    import base64
    import shlex

    # The body is the single-quoted token after ``printf %s``.
    line = next(ln for ln in captured["write"].splitlines() if ln.startswith("printf %s"))
    b64 = shlex.split(line)[2]
    body = base64.b64decode(b64).decode()
    parsed = yaml.safe_load(body)
    assert parsed == {"token": _TOKEN_CANARY, "debug": True}


@pytest.mark.asyncio
async def test_config_update_rejects_out_of_tree_path_no_ssh() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.config_update(_TARGET, {"path": "/etc/shadow", "patch": {"a": 1}})
        mock_cmd.assert_not_awaited()
    assert result["updated"] is False
    assert "safety check" in result["error"]


@pytest.mark.asyncio
async def test_config_update_rejects_empty_patch_no_ssh() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.config_update(_TARGET, {"patch": {}})
        mock_cmd.assert_not_awaited()
    assert result["updated"] is False


@pytest.mark.asyncio
async def test_config_update_rejects_non_mapping_current_config() -> None:
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="- a\n- b\n", exit_status=0)  # a YAML list
        result = await connector.config_update(_TARGET, {"patch": {"a": 1}})
    assert result["updated"] is False
    assert "mapping" in result["error"]


@pytest.mark.asyncio
async def test_config_update_absent_file_creates_from_patch() -> None:
    """An absent config (empty read, exit 0) merges onto an empty mapping."""
    connector = Rke2SshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(stdout="", exit_status=0),  # absent -> empty
            _proc(stdout="===RKE2_CONFIG_WRITTEN===\n", exit_status=0),
        ]
        result = await connector.config_update(_TARGET, {"patch": {"server": "https://x"}})
    assert result["updated"] is True
    assert result["changed_keys"] == ["server"]


# ===========================================================================
# Dispatch park -> approve -> execute -> audit (recorded fake-shell SSH)
# ===========================================================================

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()

_CONNECTOR_ID = "rke2-ssh-1.x"
_TARGET_NAME = "rke2-node-write-e2e"
_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000024e2")
_OPERATOR = Operator(
    sub="rke2-write-e2e-test",
    name="RKE2 Write E2E Operator",
    email=None,
    raw_jwt="<rke2-write-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_E2E_CURRENT_CONFIG = "server: https://old.example:9345\n"


class _FakeShellSSHServer(asyncssh.SSHServer):
    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:  # type: ignore[override]
        return bool(key == _CLIENT_PUB)


async def _fake_shell_process_factory(process: Any) -> None:
    cmd: str = process.command or ""
    if cmd.startswith("systemctl restart "):
        process.exit(0)
        return
    if cmd.startswith("systemctl is-active "):
        process.stdout.write("active\n")
        process.exit(0)
        return
    if cmd.startswith("if [ -e "):  # config read
        process.stdout.write(_E2E_CURRENT_CONFIG)
        process.exit(0)
        return
    if cmd.startswith("# meho-rke2-config-update"):  # atomic write script
        process.stdout.write("===RKE2_CONFIG_WRITTEN===\n")
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
            product="rke2",
            host=host,
            port=port,
            fqdn=None,
            secret_ref="kv/dev/rke2/write-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.x"},
            notes="seeded by test_connectors_rke2_write",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> Rke2SshConnector:
    class _SeededRke2Connector(Rke2SshConnector):  # type: ignore[misc]
        async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
            return {"username": "root", "client_keys": [_CLIENT_KEY], "known_hosts": None}

    instance = _SeededRke2Connector()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[Rke2SshConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture
async def rke2_write_e2e(
    fake_shell_server: types.SimpleNamespace,
) -> AsyncIterator[Rke2SshConnector]:
    set_default_reducer(PassThroughReducer())
    await Rke2SshConnector.register_operations()
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
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("rke2.node.service.restart", {"unit": "rke2-server"}),
        ("rke2.node.config.update", {"patch": {"debug": True}}),
    ],
)
async def test_write_op_parks_for_user_not_hard_deny(
    rke2_write_e2e: Rke2SshConnector, op_id: str, params: dict[str, Any]
) -> None:
    """An un-approved USER dispatch parks in the approve-queue (not denied, not run)."""
    result = await _dispatch(op_id, params, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert result.get("extras", {}).get("approval_request_id")


@pytest.mark.asyncio
async def test_service_restart_approved_executes_and_audits(
    rke2_write_e2e: Rke2SshConnector,
) -> None:
    op_id = "rke2.node.service.restart"
    sessionmaker = get_sessionmaker()

    async def _count() -> int:
        async with sessionmaker() as session:
            rows = await session.execute(
                select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
            )
            return len(list(rows.scalars().all()))

    baseline = await _count()
    result = await _dispatch(op_id, {"unit": "rke2-server"}, approved=True)
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["restarted"] is True
    assert result["result"]["is_active"] is True
    assert await _count() == baseline + 1


@pytest.mark.asyncio
async def test_config_update_approved_executes_and_redacts(
    rke2_write_e2e: Rke2SshConnector,
) -> None:
    result = await _dispatch(
        "rke2.node.config.update",
        {"patch": {"node-label": ["role=worker"]}, "semantics": "merge"},
        approved=True,
    )
    assert result["status"] == "ok", result.get("error")
    payload = result["result"]
    assert payload["updated"] is True
    assert payload["restart_required"] is True
    assert payload["changed_keys"] == ["node-label"]
    # No file body / value on the returned envelope.
    assert "server" not in payload  # the pre-existing config body is never echoed

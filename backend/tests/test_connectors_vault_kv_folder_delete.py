# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.folder.delete`` and ``vault.kv.list`` with ``recursive=true``.

The folder delete soft-deletes many KV v2 secrets under one folder with one
approval. These tests prove:

* **The recursive list** returns every secret below a folder, also in
  sub-folders, and refuses a folder bigger than the limit.
* **The handler** soft-deletes the live versions of every listed key, skips
  keys that are already deleted or gone, reports a failed key without
  stopping, and leaves secrets that are not listed alone. A version whose
  deletion Vault has only scheduled for later is still live and is deleted.
* **The guards**: the mount root, an empty path, bad keys and a list longer
  than the limit are refused before the call waits for approval.
* **The approval card** lists every key path and the count, never a value.
* **The permission check** runs before the call waits for approval, covers
  every listed key and every sub-folder the delete will list (also one no
  listed key is in) in one Vault call, and a call Vault would refuse on any
  of them is not parked.
* **The full flow**: preview, wait for approval, a second person approves,
  only the listed keys are deleted, a key added after the approval is left
  alone.

The Vault HTTP layer is the shared in-process fake (``tests/_vault_fakes.py``)
with a small folder tree on top. All values are obvious fakes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault import ops_kv_folder as folder_module
from meho_backplane.connectors.vault.kv_tree import (
    VaultFolderTooLargeError,
    list_kv_folders,
    list_kv_tree,
)
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PERMISSION_PREFLIGHTS,
    _PREVIEW_BUILDERS,
    PreviewContext,
)
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

from ._vault_fakes import _FakeClient, install_fake_vault

_OP_ID = "vault.kv.folder.delete"
_CONNECTOR_ID = "vault-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003939")
#: In the default tenant-scope layout (``secret/tenants/{tenant_id}/``).
_FOLDER = f"tenants/{_TENANT_ID}/lab-old"
#: An obvious fake value. It must never show up in a result, preview or row.
_FAKE_SECRET_VALUE = "fake-value-do-not-echo"
#: A deletion that happened long ago, and one Vault has only scheduled.
_PAST_DELETION = "2000-01-01T00:00:00.123456789Z"
_FUTURE_DELETION = "2999-01-01T00:00:00.123456789Z"


# ---------------------------------------------------------------------------
# A small KV v2 tree on top of the shared fake
# ---------------------------------------------------------------------------


class _Tree:
    """Secrets by full path (``<mount>/<path>``) with their version metadata."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, dict[str, Any]]] = {}
        self.list_calls: list[str] = []
        self.list_exc_for: dict[str, Exception] = {}
        self.delete_calls: list[dict[str, Any]] = []
        self.delete_exc_for: dict[str, Exception] = {}

    def add(
        self,
        full_path: str,
        *,
        versions: int = 2,
        deleted: tuple[int, ...] = (),
        scheduled: tuple[int, ...] = (),
    ) -> None:
        """Add a secret. *deleted* versions are gone; *scheduled* ones are deleted later."""
        self.secrets[full_path] = {
            str(n): {
                "created_time": "2026-01-01T00:00:00Z",
                "deletion_time": _PAST_DELETION
                if n in deleted
                else _FUTURE_DELETION
                if n in scheduled
                else "",
                "destroyed": False,
            }
            for n in range(1, versions + 1)
        }

    def list_secrets(self, path: str, mount_point: str = "secret", **_: Any) -> dict[str, Any]:
        self.list_calls.append(f"{mount_point}/{path}")
        if f"{mount_point}/{path}" in self.list_exc_for:
            raise self.list_exc_for[f"{mount_point}/{path}"]
        prefix = f"{mount_point}/{path}".rstrip("/") + "/"
        names: set[str] = set()
        for full in self.secrets:
            if full.startswith(prefix):
                rest = full[len(prefix) :]
                head, sep, _tail = rest.partition("/")
                names.add(head + sep)
        if not names:
            raise hvac.exceptions.InvalidPath("no keys")
        return {"data": {"keys": sorted(names)}}

    def read_secret_metadata(
        self, path: str, mount_point: str = "secret", **_: Any
    ) -> dict[str, Any]:
        full = f"{mount_point}/{path}"
        if full not in self.secrets:
            raise hvac.exceptions.InvalidPath("no secret")
        return {"data": {"versions": {k: dict(v) for k, v in self.secrets[full].items()}}}

    def delete_secret_versions(
        self, path: str, versions: list[int], mount_point: str = "secret", **_: Any
    ) -> None:
        full = f"{mount_point}/{path}"
        self.delete_calls.append({"path": path, "versions": list(versions)})
        if full in self.delete_exc_for:
            raise self.delete_exc_for[full]
        for number in versions:
            self.secrets[full][str(number)]["deletion_time"] = "2026-10-28T00:00:00Z"


def _install_tree(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeClient, _Tree]:
    fake = install_fake_vault(monkeypatch)
    tree = _Tree()
    kv = fake.secrets.kv.v2
    monkeypatch.setattr(kv, "list_secrets", tree.list_secrets)
    monkeypatch.setattr(kv, "read_secret_metadata", tree.read_secret_metadata)
    monkeypatch.setattr(kv, "delete_secret_versions", tree.delete_secret_versions)
    kv.secret = {"password": _FAKE_SECRET_VALUE}
    return fake, tree


def _lab_tree(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeClient, _Tree]:
    """Three secrets: one at the top and two in sub-folders."""
    fake, tree = _install_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/admin")
    tree.add(f"secret/{_FOLDER}/run1/token")
    tree.add(f"secret/{_FOLDER}/run2/deep/token", versions=1)
    # A neighbour folder whose name starts the same. It must never be touched.
    tree.add(f"secret/{_FOLDER}-keep/admin")
    return fake, tree


_ALL_KEYS = ["admin", "run1/token", "run2/deep/token"]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    monkeypatch.delenv("VAULT_KV_FOLDER_MAX_KEYS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _register() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_vault_typed_operations(embedding_service=service)


@pytest.fixture
async def _registered_ops() -> None:
    await _register()


def _operator(
    sub: str = "op-requester", *, principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _run(
    params: dict[str, Any], *, op_id: str = _OP_ID, approved: bool = True
) -> OperationResult:
    """Run the op as the approval-resume path does (the approval already happened)."""
    return await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _live(tree: _Tree, full_path: str) -> list[str]:
    return [n for n, meta in tree.secrets[full_path].items() if not meta["deletion_time"]]


# ===========================================================================
# vault.kv.list with recursive=true
# ===========================================================================


async def test_recursive_list_returns_every_secret_under_the_folder(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "recursive": True}, op_id="vault.kv.list")

    assert result.status == "ok", result.error
    assert result.result == {"keys": _ALL_KEYS}


async def test_plain_list_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER}, op_id="vault.kv.list")

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["admin", "run1/", "run2/"]}


async def test_folder_walk_returns_every_sub_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _tree = _lab_tree(monkeypatch)

    folders = await list_kv_folders(fake, mount="secret", path=_FOLDER, max_keys=10)

    assert folders == _SUB_FOLDERS


async def test_folder_walk_keeps_a_sub_folder_it_may_not_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused sub-folder is returned and not entered; the secret walk raises instead."""
    fake, tree = _lab_tree(monkeypatch)
    tree.list_exc_for[f"secret/{_FOLDER}/run2"] = hvac.exceptions.Forbidden("denied")

    folders = await list_kv_folders(fake, mount="secret", path=_FOLDER, max_keys=10)

    assert folders == ["run1", "run2"]
    with pytest.raises(hvac.exceptions.Forbidden):
        await list_kv_tree(fake, mount="secret", path=_FOLDER, max_keys=10)


async def test_folder_walk_refuses_a_folder_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _tree = _lab_tree(monkeypatch)

    with pytest.raises(VaultFolderTooLargeError):
        await list_kv_folders(fake, mount="secret", path=_FOLDER, max_keys=2)


async def test_recursive_list_refuses_a_folder_over_the_limit(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _lab_tree(monkeypatch)
    monkeypatch.setenv("VAULT_KV_FOLDER_MAX_KEYS", "2")
    get_settings.cache_clear()

    result = await _run({"path": _FOLDER, "recursive": True}, op_id="vault.kv.list")

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultFolderTooLargeError"


# ===========================================================================
# Handler
# ===========================================================================


async def test_soft_deletes_every_listed_key(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS})

    assert result.status == "ok", result.error
    assert result.result == {
        "mount": "secret",
        "path": _FOLDER,
        "listed": 3,
        "deleted": 3,
        "already_deleted": 0,
        "not_found": 0,
        "failed": 0,
        "not_on_list": 0,
        "results": [
            {"key": "admin", "status": "deleted", "versions": [1, 2]},
            {"key": "run1/token", "status": "deleted", "versions": [1, 2]},
            {"key": "run2/deep/token", "status": "deleted", "versions": [1]},
        ],
    }
    for key in _ALL_KEYS:
        assert _live(tree, f"secret/{_FOLDER}/{key}") == []
    # The neighbour folder with the same name start is untouched.
    assert _live(tree, f"secret/{_FOLDER}-keep/admin") == ["1", "2"]


async def test_only_live_versions_are_deleted(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _install_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/admin", versions=3, deleted=(1,))

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "ok", result.error
    assert tree.delete_calls == [{"path": f"{_FOLDER}/admin", "versions": [2, 3]}]


async def test_version_with_a_scheduled_deletion_is_still_live(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """Vault also puts a time in ``deletion_time`` for ``delete_version_after``."""
    _fake, tree = _install_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/admin", versions=3, deleted=(1,), scheduled=(2,))

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["results"] == [{"key": "admin", "status": "deleted", "versions": [2, 3]}]
    assert tree.delete_calls == [{"path": f"{_FOLDER}/admin", "versions": [2, 3]}]


async def test_only_scheduled_versions_left_are_deleted_not_skipped(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _install_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/admin", versions=2, scheduled=(1, 2))

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["already_deleted"] == 0
    assert result.result["deleted"] == 1
    assert tree.delete_calls == [{"path": f"{_FOLDER}/admin", "versions": [1, 2]}]


async def test_past_deletions_count_as_already_deleted(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _install_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/admin", versions=2, deleted=(1, 2))

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["results"] == [{"key": "admin", "status": "already_deleted"}]
    assert tree.delete_calls == []


@pytest.mark.parametrize(
    ("meta", "deleted"),
    [
        ({"deletion_time": "", "destroyed": False}, False),
        ({"deletion_time": _PAST_DELETION, "destroyed": False}, True),
        ({"deletion_time": _FUTURE_DELETION, "destroyed": False}, False),
        ({"deletion_time": "2026-10-29T11:59:59Z", "destroyed": False}, True),
        ({"deletion_time": "2026-10-29T12:00:00Z", "destroyed": False}, True),
        ({"deletion_time": "2026-10-29T12:00:01Z", "destroyed": False}, False),
        ({"deletion_time": "2026-10-29T12:00:01", "destroyed": False}, False),
        ({"deletion_time": "", "destroyed": True}, True),
        ({"deletion_time": "not-a-time", "destroyed": False}, True),
        ({}, False),
    ],
    ids=[
        "live",
        "past",
        "future",
        "one-second-ago",
        "right-now",
        "in-one-second",
        "naive-is-utc",
        "destroyed",
        "unparseable-is-deleted",
        "no-fields",
    ],
)
def test_deleted_at_compares_with_the_current_time(meta: dict[str, Any], deleted: bool) -> None:
    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
    assert folder_module._deleted_at(meta, now) is deleted


async def test_safe_to_run_twice(monkeypatch: pytest.MonkeyPatch, _registered_ops: None) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    await _run({"path": _FOLDER, "keys": _ALL_KEYS})
    second = await _run({"path": _FOLDER, "keys": _ALL_KEYS})

    assert second.status == "ok", second.error
    assert second.result is not None
    assert second.result["already_deleted"] == 3
    assert second.result["deleted"] == 0
    assert {row["status"] for row in second.result["results"]} == {"already_deleted"}
    # The second run sent no delete.
    assert len(tree.delete_calls) == 3


async def test_unlisted_secret_is_left_alone_and_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["deleted"] == 1
    assert result.result["not_on_list"] == 2
    assert result.result["results"][1:] == [
        {"key": "run1/token", "status": "not_on_list"},
        {"key": "run2/deep/token", "status": "not_on_list"},
    ]
    assert _live(tree, f"secret/{_FOLDER}/run1/token") == ["1", "2"]
    assert tree.delete_calls == [{"path": f"{_FOLDER}/admin", "versions": [1, 2]}]


async def test_listed_key_that_is_gone_is_reported_not_found(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "keys": ["admin", "gone"]})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert {"key": "gone", "status": "not_found"} in result.result["results"]
    assert result.result["not_found"] == 1
    assert all(call["path"] != f"{_FOLDER}/gone" for call in tree.delete_calls)


async def test_one_failed_key_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)
    tree.delete_exc_for[f"secret/{_FOLDER}/admin"] = hvac.exceptions.Forbidden("denied")

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["results"][0] == {
        "key": "admin",
        "status": "failed",
        "error": "Forbidden",
    }
    assert result.result["failed"] == 1
    assert result.result["deleted"] == 2


async def test_folder_over_the_limit_when_it_runs_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)
    monkeypatch.setenv("VAULT_KV_FOLDER_MAX_KEYS", "2")
    get_settings.cache_clear()

    result = await _run({"path": _FOLDER, "keys": ["admin"]})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultFolderTooLargeError"
    assert tree.delete_calls == []


async def test_result_never_carries_secret_values(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, _tree = _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS})

    assert _FAKE_SECRET_VALUE not in json.dumps(result.result)
    # Values are never read: only names and version metadata.
    assert fake.secrets.kv.v2.read_calls == []


async def test_out_of_tenant_folder_is_denied_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, tree = _lab_tree(monkeypatch)
    other = UUID("00000000-0000-0000-0000-00000000beef")

    result = await _run({"path": f"tenants/{other}/lab-old", "keys": ["admin"]})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultTenantScopeError"
    assert fake.auth.jwt.login_calls == []
    assert tree.delete_calls == []


# ===========================================================================
# Guards
# ===========================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"keys": ["a"]},
        {"path": "", "keys": ["a"]},
        {"path": "/", "keys": ["a"]},
        {"path": " / ", "keys": ["a"]},
        {"path": _FOLDER},
        {"path": _FOLDER, "keys": []},
        {"path": _FOLDER, "keys": ["a", "a"]},
        {"path": _FOLDER, "keys": ["/a"]},
        {"path": _FOLDER, "keys": ["a/"]},
        {"path": _FOLDER, "keys": ["a//b"]},
        {"path": _FOLDER, "keys": ["a"], "versions": [1]},
    ],
    ids=[
        "missing-path",
        "empty-path",
        "mount-root",
        "blank-root",
        "missing-keys",
        "empty-keys",
        "duplicate-keys",
        "leading-slash",
        "trailing-slash",
        "double-slash",
        "extra-param",
    ],
)
async def test_invalid_params_are_refused(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, Any], _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    result = await _run(params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
    assert tree.delete_calls == []


@pytest.mark.parametrize("key", ["../other/admin", "run1/./token", "run1/.."])
async def test_dot_segments_are_refused_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch, key: str, _registered_ops: None
) -> None:
    fake, tree = _lab_tree(monkeypatch)

    result = await _run({"path": _FOLDER, "keys": [key]})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultFolderKeyError"
    assert fake.auth.jwt.login_calls == []
    assert tree.delete_calls == []


@pytest.mark.parametrize(
    "path",
    [f"{_FOLDER}/../other", f"./{_FOLDER}", f"tenants//{_TENANT_ID}/lab-old"],
    ids=["dot-dot", "dot", "empty-part"],
)
async def test_folder_with_bad_parts_is_refused_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch, path: str, _registered_ops: None
) -> None:
    fake, tree = _lab_tree(monkeypatch)

    result = await _run({"path": path, "keys": ["admin"]})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultFolderKeyError"
    assert fake.auth.jwt.login_calls == []
    assert tree.delete_calls == []


async def test_too_many_keys_are_refused_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_KV_FOLDER_MAX_KEYS", "2")
    get_settings.cache_clear()
    await _register()
    _fake, tree = _lab_tree(monkeypatch)

    # Not approved yet: the call is refused instead of waiting for approval.
    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
    assert await _pending_count() == 0
    assert tree.delete_calls == []


def test_schema_limit_follows_the_argument() -> None:
    schema = folder_module.vault_kv_folder_delete_parameter_schema(7)
    assert schema["properties"]["keys"]["maxItems"] == 7


# ===========================================================================
# Registration
# ===========================================================================


async def test_registered_row_is_dangerous_and_needs_approval(_registered_ops: None) -> None:
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert row.source_kind == "typed"
    assert row.parameter_schema["properties"]["keys"]["maxItems"] == 500


def test_classifies_as_plain_write() -> None:
    assert classify_op(_OP_ID) == "write"


def test_preview_and_preflight_are_registered() -> None:
    assert _PREVIEW_BUILDERS[_OP_ID] is folder_module._folder_delete_preview
    assert _PERMISSION_PREFLIGHTS[_OP_ID] is folder_module._folder_delete_preflight


# ===========================================================================
# Approval card
# ===========================================================================


async def test_preview_lists_every_key_path_and_the_count() -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"path": f"{_FOLDER}/", "keys": ["run1/token", "admin"]},
        connector_id=_CONNECTOR_ID,
    )

    preview = await folder_module._folder_delete_preview(ctx)

    assert preview is not None
    assert preview["key_count"] == 2
    assert preview["key_paths"] == [f"{_FOLDER}/admin", f"{_FOLDER}/run1/token"]
    assert preview["mount"] == "secret"
    assert preview["semantics"] == "soft_delete"


# ===========================================================================
# Permission check before approval
# ===========================================================================


_SUB_FOLDERS = ["run1", "run2", "run2/deep"]


def _caps(granted_update: list[str]) -> dict[str, list[str]]:
    """Every right the lab tree needs; the ``admin`` delete path gets *granted_update*."""
    caps: dict[str, list[str]] = {f"secret/metadata/{_FOLDER}": ["list"]}
    for sub in _SUB_FOLDERS:
        caps[f"secret/metadata/{_FOLDER}/{sub}"] = ["list"]
    for key in _ALL_KEYS:
        caps[f"secret/metadata/{_FOLDER}/{key}"] = ["read"]
        caps[f"secret/delete/{_FOLDER}/{key}"] = ["update"]
    caps[f"secret/delete/{_FOLDER}/admin"] = granted_update
    return caps


def test_checks_cover_every_key_and_every_sub_folder() -> None:
    checks = folder_module._folder_delete_checks("secret", _FOLDER, _ALL_KEYS, ["run1", "run2"])

    assert checks == [
        (f"secret/metadata/{_FOLDER}", frozenset({"list"})),
        *[(f"secret/metadata/{_FOLDER}/{sub}", frozenset({"list"})) for sub in _SUB_FOLDERS],
        (f"secret/metadata/{_FOLDER}/admin", frozenset({"read"})),
        (f"secret/delete/{_FOLDER}/admin", frozenset({"update"})),
        (f"secret/metadata/{_FOLDER}/run1/token", frozenset({"read"})),
        (f"secret/delete/{_FOLDER}/run1/token", frozenset({"update"})),
        (f"secret/metadata/{_FOLDER}/run2/deep/token", frozenset({"read"})),
        (f"secret/delete/{_FOLDER}/run2/deep/token", frozenset({"update"})),
    ]


async def test_call_vault_would_refuse_is_not_parked(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, tree = _lab_tree(monkeypatch)
    fake.sys.capabilities_by_path = _caps(["read"])

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == f"secret/delete/{_FOLDER}/admin"
    assert await _pending_count() == 0
    assert tree.delete_calls == []


async def test_missing_list_right_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, _tree = _lab_tree(monkeypatch)
    fake.sys.capabilities_by_path = {**_caps(["update"]), f"secret/metadata/{_FOLDER}": []}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["path"] == f"secret/metadata/{_FOLDER}"
    assert result.extras["required"] == ["list"]


async def test_missing_right_on_a_later_key_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """The first key is allowed; a key further down the list is not."""
    fake, tree = _lab_tree(monkeypatch)
    denied = f"secret/delete/{_FOLDER}/run2/deep/token"
    fake.sys.capabilities_by_path = {**_caps(["update"]), denied: ["read"]}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == denied
    assert result.extras["required"] == ["update"]
    assert await _pending_count() == 0
    assert tree.delete_calls == []


async def test_missing_read_right_on_a_later_key_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, _tree = _lab_tree(monkeypatch)
    denied = f"secret/metadata/{_FOLDER}/run1/token"
    fake.sys.capabilities_by_path = {**_caps(["update"]), denied: ["deny"]}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["path"] == denied
    assert result.extras["required"] == ["read"]


async def test_missing_list_right_on_a_sub_folder_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """The top folder is allowed; a sub-folder the walk visits is not."""
    fake, tree = _lab_tree(monkeypatch)
    denied = f"secret/metadata/{_FOLDER}/run2/deep"
    fake.sys.capabilities_by_path = {**_caps(["update"]), denied: []}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["path"] == denied
    assert result.extras["required"] == ["list"]
    assert await _pending_count() == 0
    assert tree.delete_calls == []


async def test_unlisted_sub_folder_without_list_right_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """A sub-folder no listed key is in still gets listed by the delete, so it is checked."""
    fake, tree = _lab_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/stray/token")
    denied = f"secret/metadata/{_FOLDER}/stray"
    fake.sys.capabilities_by_path = {**_caps(["update"]), denied: []}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == denied
    assert result.extras["required"] == ["list"]
    assert await _pending_count() == 0
    assert tree.delete_calls == []


async def test_unlisted_sub_folder_vault_refuses_to_list_is_reported(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """Vault refuses the list of an unlisted sub-folder during the walk: the check names it."""
    fake, tree = _lab_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/stray/token")
    denied = f"secret/metadata/{_FOLDER}/stray"
    tree.list_exc_for[f"secret/{_FOLDER}/stray"] = hvac.exceptions.Forbidden("denied")
    fake.sys.capabilities_by_path = {**_caps(["update"]), denied: []}

    result = await _run({"path": _FOLDER, "keys": _ALL_KEYS}, approved=False)

    assert result.status == "denied"
    assert result.extras["path"] == denied
    assert result.extras["required"] == ["list"]
    assert await _pending_count() == 0
    assert tree.delete_calls == []


async def test_preflight_walks_the_folder_and_probes_every_path_in_one_vault_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check lists the folder tree once and probes every folder and key in one call."""
    fake, tree = _lab_tree(monkeypatch)
    tree.add(f"secret/{_FOLDER}/stray/token")
    stray = f"secret/metadata/{_FOLDER}/stray"
    fake.sys.capabilities_by_path = {**_caps(["update"]), stray: ["list"]}
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"path": _FOLDER, "keys": _ALL_KEYS},
        connector_id=_CONNECTOR_ID,
    )

    result = await folder_module._folder_delete_preflight(ctx)

    assert result is not None
    assert result["will_be_denied"] is False
    assert result["checked_paths"] == 11
    assert tree.list_calls == [
        f"secret/{_FOLDER}",
        f"secret/{_FOLDER}/stray",
        f"secret/{_FOLDER}/run2",
        f"secret/{_FOLDER}/run2/deep",
        f"secret/{_FOLDER}/run1",
    ]
    assert len(fake.sys.get_capabilities_calls) == 1
    assert set(fake.sys.get_capabilities_calls[0]["paths"]) == {*_caps(["update"]), stray}


async def test_preflight_probes_every_path_in_one_vault_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _tree = _lab_tree(monkeypatch)
    fake.sys.capabilities_by_path = _caps(["update"])
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"path": _FOLDER, "keys": list(reversed(_ALL_KEYS))},
        connector_id=_CONNECTOR_ID,
    )

    result = await folder_module._folder_delete_preflight(ctx)

    assert result is not None
    assert result["will_be_denied"] is False
    assert result["checked_paths"] == 10
    assert len(fake.sys.get_capabilities_calls) == 1
    assert set(fake.sys.get_capabilities_calls[0]["paths"]) == set(_caps(["update"]))


async def test_agent_principal_cannot_run_it(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    _fake, tree = _lab_tree(monkeypatch)

    result = await dispatch(
        operator=_operator("agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params={"path": _FOLDER, "keys": _ALL_KEYS},
    )

    assert result.status == "denied", result
    assert tree.delete_calls == []


# ===========================================================================
# Full flow
# ===========================================================================


async def test_full_flow_one_approval_and_a_late_key_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake, tree = _lab_tree(monkeypatch)
    fake.sys.capabilities_by_path = _caps(["update"])
    requester = _operator("op-requester")

    # 1) The caller lists the folder, then previews the delete.
    listing = await call_operation(
        requester,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": "vault.kv.list",
            "params": {"path": _FOLDER, "recursive": True},
        },
    )
    keys = listing["result"]["keys"]
    assert keys == _ALL_KEYS
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "params": {"path": _FOLDER, "keys": keys},
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    assert preview["plan_shown"] is True
    assert preview["proposed_effect"]["preview"]["key_count"] == 3

    # 2) The call waits for one approval; nothing is deleted yet.
    call = await call_operation(requester, args)
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert tree.delete_calls == []

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["preview"]["key_paths"] == [f"{_FOLDER}/{key}" for key in _ALL_KEYS]
    assert effect["preview"]["key_count"] == 3
    assert effect["permission_preflight"]["will_be_denied"] is False
    assert _FAKE_SECRET_VALUE not in json.dumps(effect)

    # 3) A secret is added to the folder after the request was made.
    tree.add(f"secret/{_FOLDER}/run3/late")

    # 4) A second person approves; the resume runs once.
    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)

    assert resume.status == "ok", resume.error
    assert resume.result["deleted"] == 3
    assert resume.result["not_on_list"] == 1
    assert {"key": "run3/late", "status": "not_on_list"} in resume.result["results"]
    # The late secret was not touched.
    assert _live(tree, f"secret/{_FOLDER}/run3/late") == ["1", "2"]
    assert len(tree.delete_calls) == 3

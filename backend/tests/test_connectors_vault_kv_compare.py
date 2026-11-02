# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``vault.kv.compare``: copies of one secret match, value never shown.

Vault is the in-process fake from ``_vault_fakes``. Each test installs a
small per-path store on it, so different paths can hold different values,
be missing, or answer 403.

The leak test uses values that match no known credential pattern. So if a
value reached the answer, the audit row, the broadcast payload or the
flight-recorder span, no redaction rule would hide it and the test would
see it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.connectors.vault import kv_compare as kv_compare_module
from meho_backplane.connectors.vault.kv_compare import KV_COMPARE_OP_SPEC, vault_kv_compare
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.redaction.flight_recorder import redact_span
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_vault

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000c0de")
_BASE = f"tenants/{_TENANT_ID}"

# Plain words and digits: no credential pattern matches these, so nothing
# but the op's own design keeps them out of every output.
_VALUE = "amber-falcon-river-0427"
_OTHER_VALUE = "amber-falcon-river-0428"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _operator() -> Operator:
    return Operator(
        sub="op-kv-compare-test",
        name=None,
        email=None,
        raw_jwt="kv-compare.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _install_store(
    monkeypatch: pytest.MonkeyPatch,
    store: dict[str, tuple[int, dict[str, Any] | None]],
    *,
    forbidden: frozenset[str] = frozenset(),
) -> Any:
    """Fake Vault whose reads answer from *store*, keyed ``<mount>/<path>``.

    A ``None`` data dict models a deleted latest version. A key in
    *forbidden* answers 403; a key not in *store* answers 404.
    """
    fake = install_fake_vault(monkeypatch)
    reads: list[str] = []

    def _read(path: str, mount_point: str = "secret", **_kwargs: Any) -> dict[str, Any]:
        full = f"{mount_point}/{path}"
        reads.append(full)
        if full in forbidden:
            raise hvac.exceptions.Forbidden("permission denied")
        if full not in store:
            raise hvac.exceptions.InvalidPath(f"no secret at {full}")
        version, data = store[full]
        return {"data": {"data": data, "metadata": {"version": version}}}

    fake.secrets.kv.v2.read_secret_version = _read
    fake.reads = reads
    return fake


async def _compare(paths: list[dict[str, Any]]) -> dict[str, Any]:
    return await vault_kv_compare(_operator(), None, {"paths": paths})


# ---------------------------------------------------------------------------
# Registration and schema
# ---------------------------------------------------------------------------


def test_spec_is_a_safe_read_classified_credential_read() -> None:
    assert KV_COMPARE_OP_SPEC["op_id"] == "vault.kv.compare"
    assert KV_COMPARE_OP_SPEC["safety_level"] == "safe"
    assert KV_COMPARE_OP_SPEC["requires_approval"] is False
    assert classify_op("vault.kv.compare") == "credential_read"


def test_paths_must_hold_2_to_10_copies() -> None:
    validator = Draft202012Validator(KV_COMPARE_OP_SPEC["parameter_schema"])
    one = {"path": "a", "key": "k"}
    assert not validator.is_valid({"paths": [one]})
    assert validator.is_valid({"paths": [one] * 2})
    assert validator.is_valid({"paths": [one] * 10})
    assert not validator.is_valid({"paths": [one] * 11})
    assert not validator.is_valid({"paths": [{"path": "a"}, one]}), "key is required"


async def test_registers_next_to_the_kv_ops(stub_embedding_service: AsyncMock) -> None:
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.compare")
            )
        ).scalar_one()
    assert row.safety_level == "safe"
    assert row.requires_approval is False


# ---------------------------------------------------------------------------
# Behaviour: same, different, missing, forbidden
# ---------------------------------------------------------------------------


async def test_copies_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/apps/db": (3, {"password": _VALUE, "user": "app"}),
            f"secret/{_BASE}/apps/db-replica": (9, {"pw": _VALUE}),
        },
    )
    result = await _compare(
        [
            {"path": f"{_BASE}/apps/db", "key": "password"},
            {"path": f"{_BASE}/apps/db-replica", "key": "pw"},
        ]
    )
    assert result["same"] is True
    first, second = result["copies"]
    assert first == {
        "mount": "secret",
        "path": f"{_BASE}/apps/db",
        "key": "password",
        "found": True,
        "matches_first": True,
        "version": 3,
        "reason": None,
    }
    assert second["found"] is True
    assert second["matches_first"] is True
    assert second["version"] == 9
    # Read with the caller's own Vault login.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "kv-compare.jwt"


async def test_copies_different(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/a": (1, {"k": _VALUE}),
            f"secret/{_BASE}/b": (1, {"k": _VALUE}),
            f"secret/{_BASE}/c": (2, {"k": _OTHER_VALUE}),
        },
    )
    result = await _compare(
        [
            {"path": f"{_BASE}/a", "key": "k"},
            {"path": f"{_BASE}/b", "key": "k"},
            {"path": f"{_BASE}/c", "key": "k"},
        ]
    )
    assert result["same"] is False
    assert [c["matches_first"] for c in result["copies"]] == [True, True, False]
    assert all(c["found"] for c in result["copies"])


async def test_non_string_values_compare_by_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/a": (1, {"k": {"x": 1, "y": [1, 2]}}),
            f"secret/{_BASE}/b": (1, {"k": {"y": [1, 2], "x": 1}}),
        },
    )
    result = await _compare(
        [{"path": f"{_BASE}/a", "key": "k"}, {"path": f"{_BASE}/b", "key": "k"}]
    )
    assert result["same"] is True


@pytest.mark.parametrize(
    ("text", "other"),
    [
        ("123", 123),
        ("1.5", 1.5),
        ("true", True),
        ("null", None),
        ("[1]", [1]),
        ('{"a":1}', {"a": 1}),
    ],
)
async def test_a_string_never_matches_another_json_type(
    monkeypatch: pytest.MonkeyPatch, text: str, other: Any
) -> None:
    """``"123"`` and ``123`` are different stored values, in either order."""
    _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/text": (1, {"k": text}),
            f"secret/{_BASE}/other": (1, {"k": other}),
            f"secret/{_BASE}/other-again": (2, {"k": other}),
        },
    )
    text_first = await _compare(
        [{"path": f"{_BASE}/text", "key": "k"}, {"path": f"{_BASE}/other", "key": "k"}]
    )
    assert text_first["same"] is False
    assert [c["found"] for c in text_first["copies"]] == [True, True]
    assert [c["matches_first"] for c in text_first["copies"]] == [True, False]

    other_first = await _compare(
        [
            {"path": f"{_BASE}/other", "key": "k"},
            {"path": f"{_BASE}/text", "key": "k"},
            {"path": f"{_BASE}/other-again", "key": "k"},
        ]
    )
    assert other_first["same"] is False
    assert [c["matches_first"] for c in other_first["copies"]] == [True, False, True]


async def test_number_and_boolean_are_different_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_store(
        monkeypatch,
        {f"secret/{_BASE}/a": (1, {"k": 1}), f"secret/{_BASE}/b": (1, {"k": True})},
    )
    result = await _compare(
        [{"path": f"{_BASE}/a", "key": "k"}, {"path": f"{_BASE}/b", "key": "k"}]
    )
    assert result["same"] is False


async def test_one_copy_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/a": (1, {"k": _VALUE}),
            f"secret/{_BASE}/no-key": (4, {"other": _VALUE}),
            f"secret/{_BASE}/deleted": (5, None),
        },
    )
    result = await _compare(
        [
            {"path": f"{_BASE}/a", "key": "k"},
            {"path": f"{_BASE}/gone", "key": "k"},
            {"path": f"{_BASE}/no-key", "key": "k"},
            {"path": f"{_BASE}/deleted", "key": "k"},
        ]
    )
    assert result["same"] is False
    _, gone, no_key, deleted = result["copies"]
    assert (gone["found"], gone["matches_first"], gone["reason"]) == (False, False, "not_found")
    assert gone["version"] is None
    assert (no_key["found"], no_key["reason"], no_key["version"]) == (False, "key_not_found", 4)
    assert (deleted["found"], deleted["reason"], deleted["version"]) == (False, "not_found", 5)


async def test_missing_first_copy_matches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_store(monkeypatch, {f"secret/{_BASE}/b": (1, {"k": _VALUE})})
    result = await _compare(
        [{"path": f"{_BASE}/a", "key": "k"}, {"path": f"{_BASE}/b", "key": "k"}]
    )
    assert result["same"] is False
    assert [c["matches_first"] for c in result["copies"]] == [False, False]
    assert result["copies"][1]["found"] is True


async def test_one_copy_forbidden_does_not_fail_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/a": (1, {"k": _VALUE}),
            f"secret/{_BASE}/locked": (1, {"k": _VALUE}),
            f"secret/{_BASE}/c": (1, {"k": _VALUE}),
        },
        forbidden=frozenset({f"secret/{_BASE}/locked"}),
    )
    result = await _compare(
        [
            {"path": f"{_BASE}/a", "key": "k"},
            {"path": f"{_BASE}/locked", "key": "k"},
            # Outside the caller's tenant: the tenant guard denies it before
            # any Vault round-trip.
            {"path": "tenants/someone-else/a", "key": "k"},
            {"path": f"{_BASE}/c", "key": "k"},
        ]
    )
    assert result["same"] is False
    _, locked, other_tenant, last = result["copies"]
    assert (locked["found"], locked["matches_first"], locked["reason"]) == (
        False,
        False,
        "forbidden",
    )
    assert (other_tenant["found"], other_tenant["reason"]) == (False, "forbidden")
    assert (last["found"], last["matches_first"]) == (True, True)
    assert "secret/tenants/someone-else/a" not in fake.reads


async def test_path_with_mount_prefix_still_fails_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.vault.ops import VaultPathShapeError

    _install_store(monkeypatch, {})
    with pytest.raises(VaultPathShapeError):
        await _compare([{"path": f"secret/{_BASE}/a", "key": "k"}, {"path": "b", "key": "k"}])


async def test_compare_uses_constant_time_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = kv_compare_module.hmac.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(kv_compare_module.hmac, "compare_digest", _spy)
    _install_store(
        monkeypatch,
        {f"secret/{_BASE}/a": (1, {"k": _VALUE}), f"secret/{_BASE}/b": (1, {"k": _VALUE})},
    )
    result = await _compare(
        [{"path": f"{_BASE}/a", "key": "k"}, {"path": f"{_BASE}/b", "key": "k"}]
    )
    assert result["same"] is True
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# ---------------------------------------------------------------------------
# Leak test: no value and no hash in the answer, audit row, broadcast, trace
# ---------------------------------------------------------------------------


def _leak_markers(value: str) -> list[str]:
    raw = value.encode("utf-8")
    return [
        value,
        base64.b64encode(raw).decode("ascii"),
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha1(raw).hexdigest(),
        hashlib.md5(raw).hexdigest(),
    ]


async def test_no_value_or_hash_reaches_result_audit_broadcast_or_trace(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    _install_store(
        monkeypatch,
        {
            f"secret/{_BASE}/a": (1, {"k": _VALUE}),
            f"secret/{_BASE}/b": (2, {"k": _OTHER_VALUE}),
        },
    )
    params = {"paths": [{"path": f"{_BASE}/a", "key": "k"}, {"path": f"{_BASE}/b", "key": "k"}]}

    result = await dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.compare",
        target=None,
        params=params,
    )
    assert result.status == "ok", result.error
    assert result.result["same"] is False
    # The credential-read answer scrub keeps every field of this answer.
    assert result.result["copies"][0]["path"] == f"{_BASE}/a"
    assert result.result["copies"][0]["key"] == "k"

    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "vault.kv.compare")))
            .scalars()
            .all()
        )
    assert rows, "the dispatch wrote no audit row"
    audit_blob = json.dumps(
        [{c.key: getattr(row, c.key) for c in AuditLog.__mapper__.column_attrs} for row in rows],
        default=str,
    )

    broadcast = redact_payload(classify_op("vault.kv.compare"), {"params": params}, "ok")
    assert broadcast == {"op_class": "credential_read", "result_status": "ok"}

    # The flight recorder never stores a vendor body for this op: a span
    # whose response carries the values records only the omission marker.
    span = redact_span(
        op_id="vault.kv.compare",
        method="GET",
        response_body={"data": {"data": {"k": _VALUE}}},
        response_content_type="application/json",
        delete_shaped_patterns=(),
    )
    assert span.body_recorded is False

    blobs = {
        "result": result.model_dump_json(),
        "audit": audit_blob,
        "broadcast": json.dumps(broadcast),
        "trace": span.model_dump_json(),
    }
    for where, blob in blobs.items():
        for value in (_VALUE, _OTHER_VALUE):
            for marker in _leak_markers(value):
                assert marker not in blob, f"value or hash leaked into the {where}"

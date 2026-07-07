# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-1 defence-in-depth on the dispatch broadcast path (meho-internal #151).

``publish_broadcast`` used to decide the payload detail with
:func:`~meho_backplane.broadcast.events.classify_op` alone: a
secret-bearing write op missing from the ``credential_*`` allowlists
fell through to the ``write`` / ``other`` class and shipped its raw
request params to every co-tenant feed subscriber. These tests pin the
hardened contract:

* every params dict runs through
  :func:`~meho_backplane.broadcast.events.scrub_broadcast_params`
  (key-name scrub + Tier-1 deterministic redactor) before the payload
  is built;
* any secret detection collapses the broadcast to **aggregate-only**
  regardless of ``op_class`` — the payload for a mis/unclassified
  secret-bearing op carries no ``params`` key at all;
* params in which no secret material is detected keep decision #3's
  full-detail default, so the feed's mutation signal for vetted benign
  writes is unchanged;
* the scrub itself fails **closed**: an exception inside redaction
  yields aggregate-only, never raw passthrough.

Leak assertions are by-exclusion on the whole serialised event (the
``test_broadcast_credential_write_dispatch`` posture): a regression
that moved the secret into a different field would still fail.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.broadcast.events import (
    _is_secret_param_name,
    scrub_broadcast_params,
)
from meho_backplane.operations._audit import publish_broadcast

# Secret fixtures assembled from fragments so gitleaks' built-in rules
# do not false-positive on the test source.
_PASSWORD_SECRET = "hunter2" + "longenough"
_BEARER_SECRET = "abcdef12" + "345678ab" + "cd"
_API_KEY_SECRET = "abcdefgh" + "12345678"

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000151")


class _Descriptor:
    """Duck-typed stand-in — ``publish_broadcast`` reads ``op_id`` only."""

    def __init__(self, op_id: str) -> None:
        self.op_id = op_id


def _make_operator() -> Operator:
    return Operator(
        sub="op-broadcast-tier1-test",
        name="Broadcast Tier1 Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with an in-memory recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


async def _publish(
    op_id: str,
    params: dict[str, Any],
    captured_events: list[BroadcastEvent],
) -> BroadcastEvent:
    await publish_broadcast(
        audit_id=uuid.uuid4(),
        operator=_make_operator(),
        descriptor=_Descriptor(op_id),  # type: ignore[arg-type]
        target=None,
        params=params,
        result_status="ok",
    )
    assert len(captured_events) == 1
    return captured_events[0]


# ---------------------------------------------------------------------------
# Secret-bearing ops outside the credential_* allowlists collapse
# ---------------------------------------------------------------------------


async def test_synthetic_unknown_write_op_does_not_broadcast_params(
    captured_events: list[BroadcastEvent],
) -> None:
    """An unpinned ``*.write`` op with a secret param ships no params at all.

    ``acme.widget.write`` is in no allowlist — pre-hardening it
    classified ``write`` and broadcast ``{"password": ...}`` verbatim.
    """
    event = await _publish(
        "acme.widget.write",
        {"password": _PASSWORD_SECRET, "widget": "w-1"},
        captured_events,
    )
    assert event.op_class == "write"
    assert event.payload == {"op_class": "write", "result_status": "ok"}
    assert "params" not in event.payload
    serialised = event.model_dump_json()
    assert _PASSWORD_SECRET not in serialised
    assert "hunter2" not in serialised


async def test_unpinned_create_op_with_bearer_string_collapses(
    captured_events: list[BroadcastEvent],
) -> None:
    """A ``Bearer <token>`` inside a benign-named param is Tier-1-detected."""
    event = await _publish(
        "acme.widget.create",
        {"note": "header was Bearer " + _BEARER_SECRET},
        captured_events,
    )
    assert event.payload == {"op_class": "write", "result_status": "ok"}
    assert _BEARER_SECRET not in event.model_dump_json()


async def test_unpinned_write_op_with_api_key_value_collapses(
    captured_events: list[BroadcastEvent],
) -> None:
    """An ``api_key=...`` value embedded in a string param is detected."""
    event = await _publish(
        "acme.widget.update",
        {"cmd": "deploy --api_key=" + _API_KEY_SECRET},
        captured_events,
    )
    assert event.payload == {"op_class": "write", "result_status": "ok"}
    assert _API_KEY_SECRET not in event.model_dump_json()


async def test_other_class_op_with_secret_param_collapses(
    captured_events: list[BroadcastEvent],
) -> None:
    """The ``other`` fall-through class gets the same aggregate floor."""
    event = await _publish(
        "some.unknown.op",
        {"client_secret": _PASSWORD_SECRET},
        captured_events,
    )
    assert event.op_class == "other"
    assert event.payload == {"op_class": "other", "result_status": "ok"}
    assert _PASSWORD_SECRET not in event.model_dump_json()


async def test_read_class_op_with_secret_param_collapses(
    captured_events: list[BroadcastEvent],
) -> None:
    """Even a ``read``-class op never ships detected secret material."""
    event = await _publish(
        "acme.session.list",
        {"session_token": _BEARER_SECRET},
        captured_events,
    )
    assert event.op_class == "read"
    assert event.payload == {"op_class": "read", "result_status": "ok"}
    assert _BEARER_SECRET not in event.model_dump_json()


async def test_credential_write_op_stays_aggregate(
    captured_events: list[BroadcastEvent],
) -> None:
    """Pinned credential_write posture is unchanged by the new gate."""
    event = await _publish(
        "vault.kv.put",
        {"path": "secret/db", "data": {"password": _PASSWORD_SECRET}},
        captured_events,
    )
    assert event.op_class == "credential_write"
    assert event.payload == {"op_class": "credential_write", "result_status": "ok"}
    assert _PASSWORD_SECRET not in event.model_dump_json()


# ---------------------------------------------------------------------------
# Benign params keep decision #3's full-detail default
# ---------------------------------------------------------------------------


async def test_benign_write_op_keeps_full_params(
    captured_events: list[BroadcastEvent],
) -> None:
    """No secret material → the feed's full mutation signal is preserved."""
    event = await _publish(
        "vsphere.vm.create",
        {"cluster": "prod", "replicas": 3, "labels": ["a", "b"]},
        captured_events,
    )
    assert event.payload == {
        "op_class": "write",
        "params": {"params": {"cluster": "prod", "replicas": 3, "labels": ["a", "b"]}},
        "result_status": "ok",
    }


async def test_benign_read_op_keeps_full_params(
    captured_events: list[BroadcastEvent],
) -> None:
    event = await _publish(
        "vsphere.vm.list",
        {"folder": "prod-vms"},
        captured_events,
    )
    assert event.payload["params"] == {"params": {"folder": "prod-vms"}}


async def test_vault_approle_config_write_keeps_full_params(
    captured_events: list[BroadcastEvent],
) -> None:
    """AppRole config attrs (bool/int under secret-ish names) don't collapse.

    ``bind_secret_id`` / ``secret_id_ttl`` are configuration scalars,
    not secret material — the vetted full-detail broadcast for
    ``vault.auth.approle.write`` must survive the new gate.
    """
    event = await _publish(
        "vault.auth.approle.write",
        {
            "role_name": "ci",
            "bind_secret_id": True,
            "secret_id_ttl": 3600,
            "token_policies": ["deploy"],
        },
        captured_events,
    )
    assert event.op_class == "write"
    assert event.payload["params"]["params"]["bind_secret_id"] is True


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


async def test_scrub_failure_collapses_to_aggregate(
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction blowing up must yield aggregate-only, never passthrough."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr("meho_backplane.broadcast.events.redact", _boom)
    event = await _publish(
        "vsphere.vm.create",
        {"cluster": "prod"},
        captured_events,
    )
    assert event.payload == {"op_class": "write", "result_status": "ok"}
    assert "prod" not in event.model_dump_json()


# ---------------------------------------------------------------------------
# scrub_broadcast_params unit contract
# ---------------------------------------------------------------------------


def test_scrub_replaces_secret_named_keys_recursively() -> None:
    scrubbed, found = scrub_broadcast_params(
        {"spec": {"env": [{"name": "X", "sessionToken": _BEARER_SECRET}]}},
    )
    assert found is True
    assert scrubbed == {
        "spec": {"env": [{"name": "X", "sessionToken": "[REDACTED:param_name]"}]},
    }


def test_scrub_benign_params_pass_through_unchanged() -> None:
    params = {"cluster": "prod", "replicas": 3, "labels": ["a", "b"]}
    scrubbed, found = scrub_broadcast_params(params)
    assert found is False
    assert scrubbed == params


def test_scrub_scalar_values_under_secretish_names_pass_through() -> None:
    params = {"bind_secret_id": True, "secret_id_ttl": 3600, "secret_id_num_uses": 0}
    scrubbed, found = scrub_broadcast_params(params)
    assert found is False
    assert scrubbed == params


def test_scrub_tier1_pass_redacts_labelled_string_shapes() -> None:
    scrubbed, found = scrub_broadcast_params(
        {"note": "Authorization: Bearer " + _BEARER_SECRET},
    )
    assert found is True
    assert _BEARER_SECRET not in str(scrubbed)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("password", True),
        ("user_password", True),
        ("passphrase", True),
        ("clientSecret", True),
        ("client-secret", True),
        ("sessionToken", True),
        ("api_key", True),
        ("api-key", True),
        ("apikey", True),
        ("private_key", True),
        ("credentials", True),
        ("kubeconfig", True),
        ("secret_id", True),
        ("continue_token", True),
        # attribute / reference names — not the secret itself
        ("token_policies", False),
        ("token_ttl", False),
        ("secret_name", False),
        ("secret_path", False),
        ("role_name", False),
        ("ssh_public_key", False),
        ("label_key", False),
        ("keyspace", False),
        ("", False),
    ],
)
def test_is_secret_param_name(name: str, expected: bool) -> None:
    assert _is_secret_param_name(name) is expected

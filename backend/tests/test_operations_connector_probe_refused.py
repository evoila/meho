# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_probe_refused`` structured error.

#2784, the sixth structured cause in the dispatcher's taxonomy and the
sibling of ``test_operations_connector_vault_forbidden`` (#2091) /
``test_operations_connector_tls_verify_failed`` (#1782) /
``test_operations_connector_unsupported`` (#1627). Coverage:

* A ``net.*`` dispatch whose destination is outside
  ``MEHO_NETDIAG_PROBE_ALLOWLIST`` returns a structured
  ``connector_probe_refused`` :class:`OperationResult` — **not** the
  reading-shaped ``status="ok"`` payload (``connected=false``,
  ``reason="not_in_probe_allowlist"``) that made a reverted allowlist
  read as a down host.
* **The narrowing.** Every sibling arm keys off a distinctive exception
  type (``hvac.exceptions.Forbidden``, an ``ssl``-caused
  ``httpx.ConnectError``, ``NotImplementedError``). This one keys off a
  :class:`ValueError` subclass **inside the catch-all ``except
  Exception`` arm**, so it is the widest discriminator in the family: a
  plain ``ValueError`` escaping a handler MUST still flatten to
  ``connector_error``. Widening ``is_probe_refused`` to
  ``isinstance(exc, ValueError)`` would relabel every bad-input error in
  every connector as "your probe allowlist refused this" and ship an
  allowlist remediation for an unrelated bug — the same
  plausible-answer-for-something-that-did-not-happen failure #2784 exists
  to remove. These tests pin the boundary against that regression.
* Builder shape per ``docs/codebase/error-message-shape.md``: a stable
  code, a message naming the env var + the chart key that renders it +
  the remediation + both doc references, and a structured ``extras``
  payload — with the message deliberately **address-free** so the guard
  never becomes an internal-topology oracle.
* Degenerate inputs: an exception carrying no ``host`` still produces a
  well-formed envelope (the builder runs on the never-raises error
  path); an oversized message is capped; an empty message leaves no
  dangling ``Allowlist said:`` tail.

The dispatch-level tests drive the **real** ``net.tcp_check`` op, so the
classification is exercised through the same path production takes. The
narrowing test reuses that same op and changes only the exception type
``asyncio.open_connection`` raises, which isolates the discriminator from
every other variable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import ops as net_ops
from meho_backplane.connectors.net.allowlist import (
    PROBE_ALLOWLIST_ENV,
    ProbeNotAllowedError,
)
from meho_backplane.connectors.net.ops import register_net_typed_operations
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import (
    is_probe_refused,
    result_connector_probe_refused,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.tcp_check"
_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000027a4")

#: Doc references the builder's message is contracted to carry.
_DOC_REFS = (
    "docs/codebase/connectors-net-diagnostics.md",
    "docs/codebase/error-message-shape.md",
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env; reset dispatcher caches around every test.

    ``MEHO_NETDIAG_PROBE_ALLOWLIST`` is cleared by default — unset means
    deny-all under the allowlist's inverted semantics, and clearing it
    explicitly stops an ambient value in the developer's shell from
    turning a refusal test into a real dial.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_net_probe_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the ``net.tcp_check`` descriptor row for dispatch-driving tests."""
    await register_net_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="op-probe-refused",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_check(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``net.tcp_check`` through the real targetless path."""
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# Classification through the real dispatch path
# ---------------------------------------------------------------------------


async def test_dispatch_allowlist_refusal_yields_structured_probe_refused(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A refused destination fails the dispatch with the structured cause.

    Not a reading: ``result`` is empty, so nothing downstream (a Sensor
    assertion, an agent) can mistake the refusal for a reachability
    answer.
    """

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("open_connection must not run when the probe is refused")

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _boom)

    result = await _dispatch_check({"host": "192.0.2.1", "port": 443})

    assert result.status == "error"
    assert result.result is None
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["allowlist_env"] == PROBE_ALLOWLIST_ENV
    assert result.extras["host"] == "192.0.2.1"
    assert result.extras["exception_class"] == "ProbeNotAllowedError"
    assert PROBE_ALLOWLIST_ENV in result.extras["detail"]


async def test_dispatch_plain_value_error_falls_through_to_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A non-refusal ``ValueError`` is NOT relabelled as an allowlist refusal.

    The load-bearing narrowing test. ``ProbeNotAllowedError`` subclasses
    :class:`ValueError`, and the classification lives inside the generic
    ``except Exception`` arm — so this pins that the arm discriminates on
    the *subclass*, not on ``ValueError``. Same op, same allowlisted
    host, same dispatch path as the test above; only the exception type
    the connect raises differs.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")

    async def _raise_value_error(*_a: object, **_kw: object) -> object:
        raise ValueError("some unrelated connector bug")

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _raise_value_error)

    result = await _dispatch_check({"host": "192.0.2.1", "port": 443})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["error_code"] != "connector_probe_refused"
    assert result.extras["exception_class"] == "ValueError"
    # No allowlist remediation is offered for an unrelated bug.
    assert result.error is not None
    assert PROBE_ALLOWLIST_ENV not in result.error


# ---------------------------------------------------------------------------
# The narrowing predicate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ProbeNotAllowedError("refused", host="10.0.0.1"), True),
        (ProbeNotAllowedError("refused"), True),
        (ValueError("plain"), False),
        (OSError("connection reset"), False),
        (RuntimeError("boom"), False),
    ],
    ids=["refusal-with-host", "refusal-bare", "plain-value-error", "oserror", "runtimeerror"],
)
def test_is_probe_refused_discriminates_on_the_subclass(
    exc: BaseException,
    expected: bool,
) -> None:
    """The predicate keys on ``ProbeNotAllowedError``, never on ``ValueError``."""
    assert is_probe_refused(exc) is expected


# ---------------------------------------------------------------------------
# Builder shape (docs/codebase/error-message-shape.md discipline)
# ---------------------------------------------------------------------------


def test_builder_names_env_var_chart_key_and_docs_without_echoing_the_host() -> None:
    """Code prefix → diagnostic → remediation → doc refs, and no destination.

    The message must stay address-free: ``assert_probe_allowed`` never
    echoes a destination, and the builder inherits that posture so the
    guard cannot be used as an internal-topology oracle. The caller's own
    host rides ``extras`` instead.
    """
    exc = ProbeNotAllowedError(
        f"probe destination refused: address is not listed in {PROBE_ALLOWLIST_ENV}",
        host="198.51.100.7",
    )

    result = result_connector_probe_refused("net.tcp_check", exc, 4.2)

    assert result.status == "error"
    assert result.op_id == "net.tcp_check"
    assert result.error is not None
    assert result.error.startswith("connector_probe_refused: ")
    assert PROBE_ALLOWLIST_ENV in result.error
    # Remediation names the knob an operator actually edits on a deploy.
    assert "netdiag.probeAllowlist" in result.error
    # States that nothing was dialed, so a caller cannot read it as "down".
    assert "no socket was opened" in result.error
    for doc_ref in _DOC_REFS:
        assert doc_ref in result.error
    # Address-free message; the destination is machine-readable only.
    assert "198.51.100.7" not in result.error
    assert result.extras["host"] == "198.51.100.7"
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["allowlist_env"] == PROBE_ALLOWLIST_ENV
    assert result.extras["exception_class"] == "ProbeNotAllowedError"


def test_builder_without_host_degrades_gracefully() -> None:
    """An exception carrying no ``host`` still yields a well-formed envelope.

    The builder runs on the dispatcher's never-raises path, so the
    ``getattr(exc, "host", None)`` branch must produce a null field rather
    than fault.
    """
    result = result_connector_probe_refused(
        "net.dns_lookup", ProbeNotAllowedError("probe destination refused: empty host"), 1.0
    )

    assert result.status == "error"
    assert result.extras["host"] is None
    assert result.error is not None
    assert PROBE_ALLOWLIST_ENV in result.error


def test_builder_ignores_a_non_string_host_attribute() -> None:
    """A non-``str`` ``host`` is dropped, not stringified into the envelope."""

    class _WeirdHostError(ProbeNotAllowedError):
        pass

    exc = _WeirdHostError("probe destination refused: host is not listed")
    exc.host = 12345  # type: ignore[assignment]

    result = result_connector_probe_refused("net.ping", exc, 1.0)

    assert result.extras["host"] is None


def test_builder_caps_oversized_message() -> None:
    """An over-cap guard message is truncated before it reaches the envelope."""
    exc = ProbeNotAllowedError("x" * 400, host="10.0.0.1")

    result = result_connector_probe_refused("net.tcp_check", exc, 1.0)

    assert result.extras["detail"].endswith("...<truncated>")
    assert len(result.extras["detail"]) < 400


def test_builder_empty_message_has_no_dangling_tail() -> None:
    """An empty guard message leaves no dangling ``Allowlist said:`` clause."""
    result = result_connector_probe_refused("net.tcp_check", ProbeNotAllowedError(""), 1.0)

    assert result.error is not None
    assert "Allowlist said:" not in result.error
    assert result.extras["detail"] == ""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector registry (G0.2-T2).

Coverage matrix (per Task #241 acceptance criteria):

* ``register_connector`` + ``get_connector`` + ``all_connectors`` are
  importable from ``meho_backplane.connectors``.
* Round-trip: register a class, get it back.
* Duplicate registration raises :exc:`RuntimeError` with a clear message.
* ``get_connector`` returns ``None`` for an unknown product.
* ``_eager_import_connectors`` walks ``connectors/`` subpackages and
  imports each one — verified with a fake subpackage injected via
  ``sys.modules`` + monkeypatched ``pkgutil.iter_modules``.
* Lifespan calls ``_eager_import_connectors`` — verified by patching
  the function and exercising the FastAPI ``TestClient`` startup.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
    all_connectors,
    get_connector,
    register_connector,
)
from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    clear_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Minimal concrete connector for registry tests."""

    product = "fake"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _AnotherFakeConnector(Connector):
    product = "another"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Isolate each test: clear the registry before and after."""
    clear_registry()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Public API importability
# ---------------------------------------------------------------------------


def test_registry_functions_importable_from_package_root() -> None:
    assert register_connector is not None
    assert get_connector is not None
    assert all_connectors is not None


# ---------------------------------------------------------------------------
# register_connector / get_connector round-trip
# ---------------------------------------------------------------------------


def test_register_then_get_returns_class() -> None:
    register_connector("vsphere", _FakeConnector)
    assert get_connector("vsphere") is _FakeConnector


def test_get_unknown_product_returns_none() -> None:
    assert get_connector("does-not-exist") is None


def test_all_connectors_reflects_registered() -> None:
    register_connector("vsphere", _FakeConnector)
    register_connector("another", _AnotherFakeConnector)
    snapshot = all_connectors()
    assert snapshot == {"vsphere": _FakeConnector, "another": _AnotherFakeConnector}


def test_all_connectors_returns_copy() -> None:
    register_connector("vsphere", _FakeConnector)
    snapshot = all_connectors()
    snapshot["injected"] = _FakeConnector  # type: ignore[assignment]
    assert "injected" not in all_connectors()


# ---------------------------------------------------------------------------
# Duplicate registration
# ---------------------------------------------------------------------------


def test_duplicate_registration_raises_runtime_error() -> None:
    register_connector("vsphere", _FakeConnector)
    with pytest.raises(RuntimeError, match="connector already registered"):
        register_connector("vsphere", _AnotherFakeConnector)


def test_duplicate_registration_error_message_names_both_classes() -> None:
    register_connector("vsphere", _FakeConnector)
    with pytest.raises(RuntimeError) as exc_info:
        register_connector("vsphere", _AnotherFakeConnector)
    msg = str(exc_info.value)
    assert "_FakeConnector" in msg
    assert "_AnotherFakeConnector" in msg
    assert "vsphere" in msg


def test_non_connector_class_raises_type_error() -> None:
    class NotAConnector:
        pass

    with pytest.raises(TypeError, match="must subclass Connector"):
        register_connector("vsphere", NotAConnector)  # type: ignore[arg-type]


def test_non_class_raises_type_error() -> None:
    with pytest.raises(TypeError, match="must subclass Connector"):
        register_connector("vsphere", "not-a-class")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# register_connector logs connector_registered
# ---------------------------------------------------------------------------


def test_register_connector_emits_structlog_event(capfd: pytest.CaptureFixture[str]) -> None:
    """registration emits a connector_registered log line."""
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector("vsphere", _FakeConnector)
    out, _ = capfd.readouterr()
    assert "connector_registered" in out
    assert "vsphere" in out


# ---------------------------------------------------------------------------
# _eager_import_connectors — fake subpackage fixture
# ---------------------------------------------------------------------------


def test_eager_import_walks_subpackages_and_imports_them() -> None:
    """_eager_import_connectors discovers subpackages and imports each one.

    A fake subpackage is injected into sys.modules before the call; the
    fake module calls register_connector at "import time" (i.e. when
    importlib.import_module resolves it from sys.modules). We monkeypatch
    pkgutil.iter_modules to yield one fake subpackage entry so the walker
    discovers exactly the module we planted.
    """
    pkg_name = "meho_backplane.connectors.testprod"

    # Create a fake module that self-registers when loaded.
    fake_mod = types.ModuleType(pkg_name)

    def _fake_import(name: str) -> types.ModuleType:
        if name == pkg_name:
            # Simulate the register_connector side-effect.
            register_connector("testprod", _FakeConnector)
            return fake_mod
        import importlib as _imp

        return _imp.import_module(name)

    fake_iter = MagicMock(return_value=iter([("", "testprod", True)]))
    _import_target = "meho_backplane.connectors.registry.importlib.import_module"

    with (
        patch("meho_backplane.connectors.registry.pkgutil.iter_modules", fake_iter),
        patch(_import_target, side_effect=_fake_import),
    ):
        _eager_import_connectors()

    assert get_connector("testprod") is _FakeConnector


def test_eager_import_skips_non_packages() -> None:
    """_eager_import_connectors does not import plain modules (ispkg=False)."""
    fake_iter = MagicMock(
        return_value=iter(
            [
                ("", "schemas", False),
                ("", "base", False),
                ("", "registry", False),
            ]
        )
    )
    imported: list[str] = []

    def _track_import(name: str) -> types.ModuleType:
        imported.append(name)
        return types.ModuleType(name)

    _import_target = "meho_backplane.connectors.registry.importlib.import_module"

    with (
        patch("meho_backplane.connectors.registry.pkgutil.iter_modules", fake_iter),
        patch(_import_target, side_effect=_track_import),
    ):
        _eager_import_connectors()

    assert imported == []


def test_eager_import_orders_subpackages_by_name() -> None:
    """_eager_import_connectors imports subpackages in name-sorted order.

    Pins the determinism contract: regardless of the order
    ``pkgutil.iter_modules`` yields entries (filesystem order on POSIX
    is not guaranteed), :func:`_eager_import_connectors` must call
    ``importlib.import_module`` in name-sorted order so startup log
    lines are stable across hosts.
    """
    # Yield unsorted to prove the sort happens here, not at iter_modules.
    fake_iter = MagicMock(
        return_value=iter(
            [
                ("", "vsphere", True),
                ("", "bind9", True),
                ("", "vault", True),
            ]
        )
    )
    imported_order: list[str] = []

    def _track_import(name: str) -> types.ModuleType:
        imported_order.append(name)
        return types.ModuleType(name)

    _import_target = "meho_backplane.connectors.registry.importlib.import_module"

    with (
        patch("meho_backplane.connectors.registry.pkgutil.iter_modules", fake_iter),
        patch(_import_target, side_effect=_track_import),
    ):
        _eager_import_connectors()

    assert imported_order == [
        "meho_backplane.connectors.bind9",
        "meho_backplane.connectors.vault",
        "meho_backplane.connectors.vsphere",
    ]


def test_eager_import_no_subpackages_is_noop() -> None:
    """_eager_import_connectors handles empty connector directory gracefully."""
    fake_iter = MagicMock(return_value=iter([]))

    with patch("meho_backplane.connectors.registry.pkgutil.iter_modules", fake_iter):
        _eager_import_connectors()  # must not raise

    assert all_connectors() == {}


# ---------------------------------------------------------------------------
# Lifespan integration — _eager_import_connectors is called at startup
# ---------------------------------------------------------------------------


def test_lifespan_calls_eager_import_connectors() -> None:
    """Lifespan hook calls _eager_import_connectors at startup.

    Runs the lifespan generator directly with all heavy dependencies
    (logging, probes, engine) patched out so the test doesn't require
    env vars or a real database.
    """
    import asyncio

    from meho_backplane.main import lifespan

    called = []

    async def _run() -> None:
        with (
            patch("meho_backplane.main.configure_logging"),
            patch("meho_backplane.main.register_probe"),
            patch("meho_backplane.main.get_engine"),
            patch("meho_backplane.main.dispose_engine"),
            patch("meho_backplane.main.get_broadcast_client"),
            patch("meho_backplane.main.dispose_broadcast_client"),
            patch(
                "meho_backplane.main._eager_import_connectors", side_effect=lambda: called.append(1)
            ),
            patch("meho_backplane.main.run_typed_op_registrars", new=AsyncMock()),
            patch("meho_backplane.main.eager_import_mcp_modules"),
            patch("meho_backplane.main._assert_mcp_resource_uri_configured"),
            patch("meho_backplane.main.get_embedding_service"),
            # G5.2-T1 (#623) added a memory-expiry sweeper start to the
            # lifespan body; G9.3-T6 (#858) added a topology-history
            # retention sweeper; G11.2-T6 (#819) added a grant-expiry
            # sweeper; G11.3-T4 (#825) added an agent_run reaper. All
            # read ``get_settings`` to decide whether to start; patch
            # all flags off so this test (which doesn't pin env vars)
            # does not regress on the env-var lookup ``get_settings``
            # would otherwise hit.
            patch(
                "meho_backplane.main.get_settings",
                return_value=MagicMock(
                    memory_expiry_enabled=False,
                    topology_history_prune_enabled=False,
                    grant_expiry_enabled=False,
                    scheduler_enabled=False,
                    agent_run_reaper_enabled=False,
                    event_drain_enabled=False,
                ),
            ),
            patch("meho_backplane.main.start_memory_expiry_sweeper"),
            patch("meho_backplane.main.stop_memory_expiry_sweeper", new=AsyncMock()),
            patch("meho_backplane.main.start_topology_history_retention_sweeper"),
            patch(
                "meho_backplane.main.stop_topology_history_retention_sweeper",
                new=AsyncMock(),
            ),
            patch("meho_backplane.main.start_grant_expiry_sweeper"),
            patch("meho_backplane.main.stop_grant_expiry_sweeper", new=AsyncMock()),
            # G11.3-T2 (#823) scheduler + G11.3-T4 (#825) agent_run-reaper
            # + G11.3-T3 (#824) event-drain patches combined via
            # ``patch.multiple`` to stay under CPython's "too many
            # statically nested blocks" limit (20) the parenthesised
            # ``with`` form imposes.
            # G3.11-T10 #1253 added validate_catalog_registry_coverage
            # to the lifespan after load_catalog. With _eager_import_connectors
            # mocked out the registry is empty by construction, so patch
            # load_catalog + the validator into the same patch.multiple
            # block (avoids tripping CPython's 20-statically-nested-block
            # limit the parenthesised ``with`` form imposes).
            patch.multiple(
                "meho_backplane.main",
                start_scheduler=MagicMock(),
                stop_scheduler=AsyncMock(),
                start_agent_run_reaper=MagicMock(),
                stop_agent_run_reaper=AsyncMock(),
                start_event_drain=MagicMock(),
                stop_event_drain=AsyncMock(),
                load_catalog=MagicMock(),
                validate_catalog_registry_coverage=MagicMock(),
            ),
        ):
            # Manually step through the lifespan async generator.
            gen = lifespan(None)  # type: ignore[arg-type]
            await gen.__aenter__()
            await gen.__aexit__(None, None, None)

    asyncio.run(_run())
    assert called == [1], "_eager_import_connectors must be called exactly once in lifespan"


def test_lifespan_runs_broadcast_dispose_even_when_engine_dispose_fails() -> None:
    """Lifespan shutdown runs every disposer even if an earlier one raises.

    asyncpg pool teardown under FastAPI lifespan exit has a documented
    loop-attached failure surface — a raise in ``dispose_engine`` must
    not short-circuit ``dispose_broadcast_client`` and leak the redis
    connection pool. The per-disposer try/except in
    :mod:`~meho_backplane.main` is the load-bearing fix this test pins.
    """
    import asyncio

    from meho_backplane.main import lifespan

    async def _run() -> None:
        broadcast_disposed = AsyncMock()
        with (
            patch("meho_backplane.main.configure_logging"),
            patch("meho_backplane.main.register_probe"),
            patch("meho_backplane.main.get_engine"),
            patch(
                "meho_backplane.main.dispose_engine",
                new=AsyncMock(side_effect=RuntimeError("engine dispose boom")),
            ),
            patch("meho_backplane.main.get_broadcast_client"),
            patch(
                "meho_backplane.main.dispose_broadcast_client",
                new=broadcast_disposed,
            ),
            patch("meho_backplane.main._eager_import_connectors"),
            patch("meho_backplane.main.run_typed_op_registrars", new=AsyncMock()),
            patch("meho_backplane.main.eager_import_mcp_modules"),
            patch("meho_backplane.main._assert_mcp_resource_uri_configured"),
            patch("meho_backplane.main.get_embedding_service"),
            # G5.2-T1 (#623) + G9.3-T6 (#858) + G11.2-T6 (#819) +
            # G11.3-T4 (#825) — same lifespan-task patches as the
            # sibling test. All background-task flags are pinned off
            # so this dispose-error test exercises only the disposer
            # ordering, not the start-task race.
            patch(
                "meho_backplane.main.get_settings",
                return_value=MagicMock(
                    memory_expiry_enabled=False,
                    topology_history_prune_enabled=False,
                    grant_expiry_enabled=False,
                    scheduler_enabled=False,
                    agent_run_reaper_enabled=False,
                    event_drain_enabled=False,
                ),
            ),
            patch("meho_backplane.main.start_memory_expiry_sweeper"),
            patch("meho_backplane.main.stop_memory_expiry_sweeper", new=AsyncMock()),
            patch("meho_backplane.main.start_topology_history_retention_sweeper"),
            patch(
                "meho_backplane.main.stop_topology_history_retention_sweeper",
                new=AsyncMock(),
            ),
            patch("meho_backplane.main.start_grant_expiry_sweeper"),
            patch("meho_backplane.main.stop_grant_expiry_sweeper", new=AsyncMock()),
            # G11.3-T2 (#823) scheduler + G11.3-T4 (#825) agent_run-reaper
            # + G11.3-T3 (#824) event-drain patches combined via
            # ``patch.multiple`` to stay under CPython's "too many
            # statically nested blocks" limit (20) the parenthesised
            # ``with`` form imposes.
            # Same G3.11-T10 #1253 shape as the sibling lifespan test —
            # registry is empty under the mocks, so skip the catalog
            # coverage validator. Folded into patch.multiple to fit
            # under CPython's 20-statically-nested-block limit.
            patch.multiple(
                "meho_backplane.main",
                start_scheduler=MagicMock(),
                stop_scheduler=AsyncMock(),
                start_agent_run_reaper=MagicMock(),
                stop_agent_run_reaper=AsyncMock(),
                start_event_drain=MagicMock(),
                stop_event_drain=AsyncMock(),
                load_catalog=MagicMock(),
                validate_catalog_registry_coverage=MagicMock(),
            ),
        ):
            gen = lifespan(None)  # type: ignore[arg-type]
            await gen.__aenter__()
            # Lifespan __aexit__ must NOT propagate the engine-dispose
            # failure (otherwise the per-disposer try/except in main.py
            # didn't catch it) and MUST still call dispose_broadcast_client.
            await gen.__aexit__(None, None, None)
        broadcast_disposed.assert_awaited_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# canonical_product_token — product-token alias reconciliation
# (G0.18-T2 #1355, RDC #789 Finding 6; closes #1312 acceptance B)
# ---------------------------------------------------------------------------


def test_canonical_product_token_sddc_is_identity_after_realignment() -> None:
    """``"sddc"`` is now a canonical registry token — canonicalised to itself.

    The ``"sddc" -> "sddc-manager"`` alias was retired by #1814 (Initiative
    #1810), which realigned the connector to register under ``"sddc"``
    directly. With :data:`PRODUCT_ALIASES` empty the canonicaliser is the
    identity, so the connector-list spelling ``"sddc"`` passes through
    unchanged and validates directly at ``POST /api/v1/targets``.
    """
    from meho_backplane.connectors.registry import canonical_product_token

    assert canonical_product_token("sddc") == "sddc"


def test_canonical_product_token_is_identity_for_canonical_tokens() -> None:
    """A canonical registry token is returned verbatim (never re-aliased)."""
    from meho_backplane.connectors.registry import canonical_product_token

    assert canonical_product_token("sddc") == "sddc"
    assert canonical_product_token("vmware") == "vmware"
    assert canonical_product_token("k8s") == "k8s"


def test_canonical_product_token_passes_unknown_tokens_through() -> None:
    """An unknown / unregistered token is returned unchanged (no guessing).

    Canonicalisation only normalises *known* aliases; an arbitrary
    string is the validator's problem (it 422s with ``unknown_product``),
    not the canonicaliser's.
    """
    from meho_backplane.connectors.registry import canonical_product_token

    assert canonical_product_token("totally-unknown") == "totally-unknown"


def test_canonical_product_token_is_idempotent() -> None:
    """``canonical(canonical(x)) == canonical(x)`` for identity + unknown cases.

    With :data:`PRODUCT_ALIASES` empty post-#1814 every token is its own
    canonical form; idempotency holds trivially but is pinned so a future
    sanctioned alias addition keeps the one-hop contract.
    """
    from meho_backplane.connectors.registry import canonical_product_token

    for token in ("sddc", "vmware", "totally-unknown"):
        once = canonical_product_token(token)
        assert canonical_product_token(once) == once


def test_product_aliases_keys_are_not_themselves_canonical() -> None:
    """No alias key is also an alias value — guards against a chained alias.

    The canonicaliser is a single ``dict.get``; if an alias key were
    also a value, a token would canonicalise to another alias and the
    one-hop idempotency contract would break. Pin the invariant
    structurally so a future alias addition can't introduce a chain.
    """
    from meho_backplane.connectors.registry import PRODUCT_ALIASES

    alias_values = set(PRODUCT_ALIASES.values())
    assert set(PRODUCT_ALIASES) & alias_values == set()

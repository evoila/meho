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
from unittest.mock import MagicMock, patch

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

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _AnotherFakeConnector(Connector):
    product = "another"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
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
            patch(
                "meho_backplane.main._eager_import_connectors", side_effect=lambda: called.append(1)
            ),
        ):
            # Manually step through the lifespan async generator.
            gen = lifespan(None)  # type: ignore[arg-type]
            await gen.__aenter__()
            await gen.__aexit__(None, None, None)

    asyncio.run(_run())
    assert called == [1], "_eager_import_connectors must be called exactly once in lifespan"

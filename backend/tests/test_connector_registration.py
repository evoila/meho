# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the auto-shim near-miss guard + dispatch wording.

G0.25-T2 (#1753) acceptance criteria:

* Ingesting under a **near-miss** ``impl_id`` — one for which no class
  is registered while a hand-rolled class already ships for the same
  ``(product, version)`` under a DIFFERENT ``impl_id`` — surfaces a
  structured ``connector_ingest_near_miss_impl_id`` warning naming the
  sibling. The canonical repro is ``(nsx, 9.0, nsx-rest-probe)`` when
  ``(nsx, 9.0, nsx-rest)`` (the real :class:`NsxConnector`) is
  registered.
* A genuinely novel ``(product, version)`` triple is unchanged: it
  still info-logs ``connector_ingest_orphaned_class`` and proceeds; the
  near-miss warning does NOT fire.
* The auto-shim ``connector_unsupported`` dispatch error names the
  registered sibling ``impl_id`` and recommends re-ingesting under it
  (rather than the misleading "write a subclass — it's future work"),
  reusing the structured ``connector_unsupported`` shape (#1627) per
  ``docs/codebase/error-message-shape.md``.

The defense-in-depth split: the resolver tie-break that makes a
hand-rolled class actually outrank the shim is the load-bearing fix in
sibling Task T1 #1750 — this task is the ingest-time warning + the
dispatch-time messaging only.

Log-capture surface (#1254): these tests bind a private
:class:`structlog.testing.LogCapture` onto a freshly-wrapped logger and
monkeypatch the subject module's ``_log`` rather than using
:func:`structlog.testing.capture_logs`. Under pytest-xdist a concurrent
:func:`structlog.configure` (lifespan boot / observability fixtures) can
race ``capture_logs``' global processor-list swap and drop the event
(the flake documented on the sibling orphan test in
``test_operations_register_ingested.py``). The private-logger pattern is
process-local, contextvar-free, and auto-restored on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog.testing

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.nsx import NsxConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.operations._errors import result_connector_unsupported
from meho_backplane.operations.ingest import connector_registration
from meho_backplane.operations.ingest.connector_registration import (
    check_version_covered_by_registered_class,
    sibling_handrolled_impl_id,
)


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    """Reset the v2 connector registry around every test.

    The near-miss / orphan branches read the live registry, so tests
    that register a sibling must not leak into the next test's
    empty-registry expectation.
    """
    clear_registry()
    yield
    clear_registry()


class _FakeHandRolledConnector(Connector):
    """A hand-rolled connector stand-in (NOT a :class:`GenericRestConnector`).

    Used where a synthetic sibling is clearer than dragging in a real
    vendor class; the only property under test is that it is a
    ``Connector`` subclass and not a ``GenericRestConnector`` auto-shim.
    """

    product = "acme"
    version = "1.0"
    impl_id = "acme-rest"
    supported_version_range = ">=1.0,<2.0"
    priority = 1

    async def fingerprint(self, target: Any) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError


def _private_log_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> structlog.testing.LogCapture:
    """Bind an xdist-safe private LogCapture onto the subject module's ``_log``."""
    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
    )
    monkeypatch.setattr(connector_registration, "_log", private_log)
    return capture


def _events(capture: structlog.testing.LogCapture, event: str) -> list[dict[str, Any]]:
    return [entry for entry in capture.entries if entry.get("event") == event]


# --------------------------------------------------------------------------- #
# sibling_handrolled_impl_id — the shared registry scan
# --------------------------------------------------------------------------- #


def test_sibling_helper_finds_handrolled_class_under_different_impl() -> None:
    """A hand-rolled class for the same ``(product, version)`` is returned."""
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=NsxConnector,
    )
    assert (
        sibling_handrolled_impl_id(
            product="nsx",
            version="9.0",
            exclude_impl_id="nsx-rest-probe",
        )
        == "nsx-rest"
    )


def test_sibling_helper_excludes_self() -> None:
    """The ``impl_id`` being ingested never flags itself as its own sibling."""
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=NsxConnector,
    )
    assert (
        sibling_handrolled_impl_id(
            product="nsx",
            version="9.0",
            exclude_impl_id="nsx-rest",
        )
        is None
    )


def test_sibling_helper_returns_none_on_version_mismatch() -> None:
    """A sibling at a DIFFERENT version is not a near-miss (resolver keys on version)."""
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=NsxConnector,
    )
    assert (
        sibling_handrolled_impl_id(
            product="nsx",
            version="4.1",
            exclude_impl_id="nsx-rest-probe",
        )
        is None
    )


def test_sibling_helper_ignores_auto_shims() -> None:
    """A registry holding only ``GenericRestConnector`` shims yields no sibling."""
    shim = connector_registration._synthesise_shim_class(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url=None,
    )
    register_connector_v2(product="acme", version="1.0", impl_id="acme-rest", cls=shim)
    assert (
        sibling_handrolled_impl_id(
            product="acme",
            version="1.0",
            exclude_impl_id="acme-rest-probe",
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Ingest near-miss guard — check_version_covered_by_registered_class
# --------------------------------------------------------------------------- #


def test_near_miss_impl_id_warns_naming_the_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``(nsx, 9.0, nsx-rest-probe)`` with ``(nsx, 9.0, nsx-rest)`` registered → warn.

    The structured warning names the sibling ``impl_id`` and the
    orphan info-log does NOT fire (this is a near-miss, not a novel
    triple). No raise — the guard is advisory.
    """
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=NsxConnector,
    )
    capture = _private_log_capture(monkeypatch)

    # Must not raise — the near-miss guard warns and proceeds.
    check_version_covered_by_registered_class(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest-probe",
    )

    near_miss = _events(capture, "connector_ingest_near_miss_impl_id")
    assert len(near_miss) == 1
    event = near_miss[0]
    assert event["product"] == "nsx"
    assert event["version"] == "9.0"
    assert event["impl_id"] == "nsx-rest-probe"
    assert event["sibling_impl_id"] == "nsx-rest"
    # The human-readable message names the sibling + the "did you mean".
    assert "nsx-rest" in event["message"]
    assert "nsx-rest-probe" in event["message"]
    assert "did you mean nsx-rest?" in event["message"]

    # The orphan branch must NOT fire on a near-miss.
    assert _events(capture, "connector_ingest_orphaned_class") == []


def test_novel_product_version_still_logs_orphan_and_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely novel ``(product, version)`` is unchanged: orphan info-log, no warn.

    Negative half of the acceptance contract — the near-miss guard must
    NOT fire when no hand-rolled sibling exists for the
    ``(product, version)``.
    """
    capture = _private_log_capture(monkeypatch)

    check_version_covered_by_registered_class(
        product="brand-new-vendor",
        version="1.0",
        impl_id="brand-new-impl",
    )

    orphan = _events(capture, "connector_ingest_orphaned_class")
    assert len(orphan) == 1
    assert orphan[0]["product"] == "brand-new-vendor"
    assert orphan[0]["version"] == "1.0"
    assert orphan[0]["impl_id"] == "brand-new-impl"

    # The near-miss warning must NOT fire for a novel triple.
    assert _events(capture, "connector_ingest_near_miss_impl_id") == []


def test_near_miss_does_not_fire_when_sibling_is_only_an_auto_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing shim under another ``impl_id`` is not a near-miss sibling.

    Only a hand-rolled class counts — a shim shadowing a shim carries
    no "you meant the working one" signal, so the orphan branch (not the
    near-miss branch) fires.
    """
    shim = connector_registration._synthesise_shim_class(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url=None,
    )
    register_connector_v2(product="acme", version="1.0", impl_id="acme-rest", cls=shim)
    capture = _private_log_capture(monkeypatch)

    check_version_covered_by_registered_class(
        product="acme",
        version="1.0",
        impl_id="acme-rest-probe",
    )

    assert _events(capture, "connector_ingest_near_miss_impl_id") == []
    assert len(_events(capture, "connector_ingest_orphaned_class")) == 1


def test_near_miss_with_synthetic_handrolled_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The near-miss path is connector-agnostic — any hand-rolled sibling triggers it."""
    register_connector_v2(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        cls=_FakeHandRolledConnector,
    )
    capture = _private_log_capture(monkeypatch)

    check_version_covered_by_registered_class(
        product="acme",
        version="1.0",
        impl_id="acme-soap",
    )

    near_miss = _events(capture, "connector_ingest_near_miss_impl_id")
    assert len(near_miss) == 1
    assert near_miss[0]["sibling_impl_id"] == "acme-rest"


# --------------------------------------------------------------------------- #
# Dispatch-time wording — result_connector_unsupported sibling remediation
# --------------------------------------------------------------------------- #


def test_dispatch_error_names_sibling_and_recommends_reingest() -> None:
    """``unreplaced_auto_shim`` + a sibling → name it, recommend re-ingest under it.

    Per ``docs/codebase/error-message-shape.md`` the structured shape
    carries a stable code + diagnostic-bearing message + structured
    ``extras``. With a sibling present the remediation must NOT tell the
    operator to write a subclass (one exists) — it must name the sibling
    ``impl_id`` and recommend re-ingesting under it.
    """
    exc = NotImplementedError(
        "auto-registered shim for ('nsx', '9.0', 'nsx-rest-probe') "
        "must be replaced with a per-product Connector subclass before dispatch"
    )
    result = result_connector_unsupported(
        "list_segments",
        exc,
        cause="unreplaced_auto_shim",
        connector_class="AutoShim_nsx_9_0_nsx_rest_probe",
        duration_ms=1.0,
        sibling_impl_id="nsx-rest",
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["cause"] == "unreplaced_auto_shim"
    assert result.extras["sibling_impl_id"] == "nsx-rest"
    # The raise-site detail survives verbatim in extras + the error string.
    assert "must be replaced with a per-product Connector subclass" in result.extras["detail"]

    # The remediation names the sibling and recommends re-ingesting under
    # it; it must NOT instruct writing a new subclass on this path.
    assert "nsx-rest" in result.error
    assert "re-ingest" in result.error.lower()
    assert "do NOT write a new subclass" in result.error
    # `grep -n "did you mean\|sibling\|re-ingest"` acceptance hook —
    # the dispatch error carries the re-ingest remediation.
    assert "impl_id='nsx-rest'" in result.error


def test_dispatch_error_without_sibling_keeps_register_subclass_remediation() -> None:
    """No sibling → original ``unreplaced_auto_shim`` wording is preserved.

    Regression guard for the genuinely-novel case (no hand-rolled class
    anywhere): the message keeps "register the per-product subclass —
    re-ingesting will NOT replace the shim" and ``sibling_impl_id`` is
    ``None``.
    """
    exc = NotImplementedError(
        "auto-registered shim for ('widget', '3.0', 'widget-rest') "
        "must be replaced with a per-product Connector subclass before dispatch"
    )
    result = result_connector_unsupported(
        "do_thing",
        exc,
        cause="unreplaced_auto_shim",
        connector_class="AutoShim_widget_3_0_widget_rest",
        duration_ms=1.0,
        sibling_impl_id=None,
    )

    assert result.extras["sibling_impl_id"] is None
    assert "register the hand-rolled per-product connector subclass" in result.error.lower()
    assert "re-ingesting the spec will NOT replace the shim" in result.error
    # The sibling-specific remediation must NOT leak onto the no-sibling path.
    assert "do NOT write a new subclass" not in result.error


def test_dispatch_error_unsupported_feature_unaffected_by_sibling_param() -> None:
    """The ``unsupported_feature`` cause keeps its auth-contract remediation.

    ``sibling_impl_id`` only forks the ``unreplaced_auto_shim`` branch;
    a hand-rolled connector rejecting the target's ``auth_model`` is a
    config matter and its remediation is unchanged even if the param is
    (spuriously) supplied.
    """
    exc = NotImplementedError("NsxConnector does not support auth_model='basic'")
    result = result_connector_unsupported(
        "list_segments",
        exc,
        cause="unsupported_feature",
        connector_class="NsxConnector",
        duration_ms=1.0,
        sibling_impl_id="nsx-rest",
    )

    assert result.extras["cause"] == "unsupported_feature"
    assert "docs/architecture/connector-auth.md" in result.error
    assert "do NOT write a new subclass" not in result.error

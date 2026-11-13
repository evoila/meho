# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every connector class says which product versions it fits, or says why not.

A class that leaves ``supported_version_range`` as ``None`` is picked for
every version of its product. That is right only when no range applies (a
cloud API, a product that reports no version). So the class must say so in
``version_range_reason``. This test walks every connector class
and fails when a class has neither.

``_WAITING_FOR_RANGE`` lists the classes whose range is still to be decided.
Each entry names the Task that decides it. An entry fails this test as soon as
its class gets a range or a reason, so the Task that adds the range also
removes the entry.
"""

from __future__ import annotations

import inspect

import pytest

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import _eager_import_connectors

# Class name -> the Task that decides its range. Remove the entry in that Task.
_WAITING_FOR_RANGE: dict[str, str] = {
    "PrometheusConnector": "#6262",
    "VaultConnector": "#6263",
    "HypervConnector": "the Windows connectors (no Task yet)",
    "MsadConnector": "the Windows connectors (no Task yet)",
    "WindowsDnsConnector": "the Windows connectors (no Task yet)",
    "WinsrvConnector": "the Windows connectors (no Task yet)",
    "WsfcConnector": "the Windows connectors (no Task yet)",
}


def classes_without_range_or_reason(classes: list[type[Connector]]) -> list[str]:
    """Names of the classes with no range and no written reason."""
    return sorted(
        cls.__name__
        for cls in classes
        if cls.supported_version_range is None and not (cls.version_range_reason or "").strip()
    )


def _all_subclasses(base: type[Connector]) -> list[type[Connector]]:
    found: list[type[Connector]] = []
    for sub in base.__subclasses__():
        found.append(sub)
        found.extend(_all_subclasses(sub))
    return found


def _connector_classes() -> list[type[Connector]]:
    """Every concrete connector class in the source tree.

    Walks the class tree, not the registry: other tests empty the registry, and
    a class that never registers would be missed there.
    """
    _eager_import_connectors()
    unique = {
        id(cls): cls
        for cls in _all_subclasses(Connector)
        if cls.__module__.startswith("meho_backplane.connectors.")
        and hasattr(cls, "product")
        and not inspect.isabstract(cls)
    }
    return list(unique.values())


def test_every_registered_class_has_a_range_or_a_reason() -> None:
    """A class with no range and no reason is a mistake, unless its Task is still open."""
    missing = classes_without_range_or_reason(_connector_classes())
    unexpected = [name for name in missing if name not in _WAITING_FOR_RANGE]
    assert unexpected == [], (
        f"{unexpected} set no supported_version_range and no version_range_reason. "
        "Set a range, or set version_range_reason to say why none applies."
    )


def test_waiting_list_has_no_stale_entry() -> None:
    """An entry whose class now has a range or a reason (or is gone) must be removed."""
    missing = set(classes_without_range_or_reason(_connector_classes()))
    stale = sorted(name for name in _WAITING_FOR_RANGE if name not in missing)
    assert stale == [], f"Remove these from _WAITING_FOR_RANGE, their range is set: {stale}"


def test_check_fails_on_a_new_class_with_no_range_and_no_reason() -> None:
    """The check refuses a class with ``None`` and no reason, also a blank reason."""

    class _NoRange(Connector):
        product = "x"

    class _BlankReason(Connector):
        product = "x"
        version_range_reason = "   "

    assert classes_without_range_or_reason([_NoRange, _BlankReason]) == [
        "_BlankReason",
        "_NoRange",
    ]


@pytest.mark.parametrize(
    ("range_", "reason"),
    [(">=1.0,<2.0", None), (None, "The vendor API has no product version.")],
)
def test_check_accepts_a_range_or_a_reason(range_: str | None, reason: str | None) -> None:
    """A range passes. ``None`` with a written reason passes."""

    class _Ok(Connector):
        product = "x"
        supported_version_range = range_
        version_range_reason = reason

    assert classes_without_range_or_reason([_Ok]) == []

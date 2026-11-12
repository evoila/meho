# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every connector class of the package has a version range, or says why not.

A connector with no ``supported_version_range`` is picked for every version of
its product. That is right only when no range applies, for example a cloud API
with no product version. Then the class sets ``version_range_reason`` to one
plain sentence. The test fails for a class that sets neither.
"""

from __future__ import annotations

import importlib

from meho_connector_kit import Connector

#: The package's own folder, as a module name. Rename it with the folder.
PACKAGE = "meho_backplane.connectors.example_widget"


def _subclasses(base: type[Connector]) -> list[type[Connector]]:
    found: list[type[Connector]] = []
    for sub in base.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


def classes_without_range_or_reason(classes: list[type[Connector]]) -> list[str]:
    """Names of the classes with no range and no written reason."""
    return sorted(
        cls.__name__
        for cls in classes
        if getattr(cls, "supported_version_range", None) is None
        and not (getattr(cls, "version_range_reason", None) or "").strip()
    )


def test_every_connector_class_has_a_range_or_a_reason() -> None:
    """No connector class of the package leaves the range out without a reason."""
    importlib.import_module(PACKAGE)
    own = [
        cls
        for cls in _subclasses(Connector)
        if cls.__module__ == PACKAGE or cls.__module__.startswith(f"{PACKAGE}.")
    ]
    assert own, f"no connector class found in {PACKAGE}"
    missing = classes_without_range_or_reason(own)
    assert missing == [], (
        f"{missing} set no supported_version_range and no version_range_reason. "
        "Set a range, or set version_range_reason to say why none applies."
    )


def test_check_finds_a_class_with_neither() -> None:
    """The check fails for ``None`` with no reason, and passes with a range or a reason."""

    class _NoRange(Connector):
        product = "x"

    class _WithReason(Connector):
        product = "x"
        version_range_reason = "The vendor API has no product version."

    class _WithRange(Connector):
        product = "x"
        supported_version_range = ">=1.0,<2.0"

    assert classes_without_range_or_reason([_NoRange, _WithReason, _WithRange]) == ["_NoRange"]

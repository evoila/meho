# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The version ranges of the pfSense, BIND 9, Argo CD and Loki connectors match current releases."""

from __future__ import annotations

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.loki import LokiConnector
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.pfsense.connector import _release_number


def _accepts(cls: type, version: str) -> bool:
    spec = cls.supported_version_range  # type: ignore[attr-defined]
    assert spec, f"{cls.__name__} declares no range"
    return Version(version) in SpecifierSet(spec)


@pytest.mark.parametrize(
    ("cls", "inside", "outside"),
    [
        (PfSenseConnector, ["2.7.0", "2.7.2", "2.8.1", "2.9.0"], ["2.6.0", "2.10.0", "3.0.0"]),
        (Bind9Connector, ["9.18.0", "9.18.24", "9.20.15"], ["9.16.50", "9.21.0", "10.0.0"]),
        (ArgoCdConnector, ["3.0.0", "3.3.4", "3.6.0"], ["2.14.0", "4.0.0"]),
        (LokiConnector, ["2.9.0", "3.5.0"], ["2.8.0", "4.0.0"]),
    ],
)
def test_range_takes_current_versions_and_refuses_others(
    cls: type, inside: list[str], outside: list[str]
) -> None:
    assert all(_accepts(cls, v) for v in inside)
    assert not any(_accepts(cls, v) for v in outside)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2.7.2-RELEASE", "2.7.2"), ("2.8.1", "2.8.1"), (None, None), ("", "")],
)
def test_pfsense_release_number(raw: str | None, expected: str | None) -> None:
    assert _release_number(raw) == expected


def test_pfsense_probed_version_is_readable_by_the_resolver() -> None:
    """``2.7.2-RELEASE`` is not a PEP 440 version; the fingerprint must not carry it."""
    with pytest.raises(InvalidVersion):
        Version("2.7.2-RELEASE")
    probed = _release_number("2.8.1-RELEASE")
    assert probed is not None
    assert _accepts(PfSenseConnector, probed)

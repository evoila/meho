# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Kubernetes and RKE2 connectors declare which versions they serve."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from meho_backplane.checks.connector_fit import connector_fit
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.resolver import connector_serves_target
from meho_backplane.connectors.rke2 import Rke2SshConnector

_RANGE = ">=1.32,<1.38"


@dataclass
class _Target:
    product: str
    version: str | None = None
    fingerprint: dict[str, Any] | None = None


def test_both_connectors_declare_the_same_range() -> None:
    assert KubernetesConnector.supported_version_range == _RANGE
    assert Rke2SshConnector.supported_version_range == _RANGE


@pytest.mark.parametrize(
    ("product", "version", "impl_id", "connector_id"),
    [
        ("k8s", "1.x", "k8s", "k8s-1.x"),
        ("rke2", "1.x", "rke2-ssh", "rke2-ssh-1.x"),
    ],
)
class TestRange:
    @pytest.mark.parametrize(
        "detected",
        ["v1.32.5", "v1.32.5+k3s1", "v1.35.0+rke2r1", "v1.37.2", "1.37"],
    )
    def test_version_in_range_is_served(
        self, product: str, version: str, impl_id: str, connector_id: str, detected: str
    ) -> None:
        target = _Target(product, fingerprint={"version": detected})
        assert connector_serves_target(product, version, impl_id, target)
        assert connector_fit(connector_id, target).warning is None

    @pytest.mark.parametrize("detected", ["v1.31.9", "v1.24.0+rke2r1", "v1.38.0", "v2.0.0"])
    def test_version_outside_range_is_refused_and_names_the_range(
        self, product: str, version: str, impl_id: str, connector_id: str, detected: str
    ) -> None:
        target = _Target(product, fingerprint={"version": detected})
        assert not connector_serves_target(product, version, impl_id, target)
        fit = connector_fit(connector_id, target)
        assert fit.warning is not None
        assert _RANGE in fit.warning

    def test_unknown_version_gives_no_warning(
        self, product: str, version: str, impl_id: str, connector_id: str
    ) -> None:
        assert connector_fit(connector_id, _Target(product)).warning is None

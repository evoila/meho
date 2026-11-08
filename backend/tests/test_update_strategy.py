# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``update-strategy`` pre-upgrade Job (#5962) against a fake Kubernetes API.

The chart side (when the Job renders, its rights) is covered by
``test_chart_update_strategy_job.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane import update_strategy
from meho_backplane.update_strategy import (
    FIELD_MANAGER,
    UpdateStrategyError,
    switch_to_recreate,
)


def _deployment(strategy: Any) -> SimpleNamespace:
    return SimpleNamespace(spec=SimpleNamespace(strategy=strategy))


def _rolling() -> SimpleNamespace:
    return SimpleNamespace(
        type="RollingUpdate",
        rolling_update=SimpleNamespace(max_surge="25%", max_unavailable="25%"),
    )


class _FakeApps:
    def __init__(self, deployment: Any = None, read_error: int | None = None) -> None:
        self.deployment = deployment
        self.read_error = read_error
        self.patch_error: int | None = None
        self.patches: list[dict[str, Any]] = []

    async def read_namespaced_deployment(self, name: str, namespace: str, **_: Any) -> Any:
        if self.read_error is not None:
            raise ApiException(status=self.read_error, reason="Error")
        return self.deployment

    async def patch_namespaced_deployment(
        self, name: str, namespace: str, body: Any, **kwargs: Any
    ) -> None:
        if self.patch_error is not None:
            raise ApiException(status=self.patch_error, reason="Forbidden")
        self.patches.append({"name": name, "namespace": namespace, "body": body, **kwargs})


async def test_rolling_update_block_is_removed_with_one_merge_patch() -> None:
    api = _FakeApps(_deployment(_rolling()))

    assert await switch_to_recreate(api, "meho", "rel-meho") == "changed"

    (patch,) = api.patches
    assert patch["name"] == "rel-meho"
    assert patch["namespace"] == "meho"
    # `rollingUpdate: null` in a merge patch removes the block.
    assert patch["body"] == {"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}}}
    assert patch["_content_type"] == "application/merge-patch+json"
    assert patch["field_manager"] == FIELD_MANAGER


@pytest.mark.parametrize(
    "strategy",
    [
        None,
        SimpleNamespace(type="Recreate", rolling_update=None),
    ],
)
async def test_no_block_means_no_patch(strategy: Any) -> None:
    api = _FakeApps(_deployment(strategy))

    assert await switch_to_recreate(api, "meho", "rel-meho") == "unchanged"
    assert api.patches == []


async def test_missing_deployment_is_fine() -> None:
    api = _FakeApps(read_error=404)

    assert await switch_to_recreate(api, "meho", "rel-meho") == "absent"
    assert api.patches == []


async def test_read_error_stops_the_job() -> None:
    api = _FakeApps(read_error=403)

    with pytest.raises(UpdateStrategyError, match='cannot read Deployment "rel-meho": HTTP 403'):
        await switch_to_recreate(api, "meho", "rel-meho")


async def test_patch_error_stops_the_job() -> None:
    api = _FakeApps(_deployment(_rolling()))
    api.patch_error = 403

    with pytest.raises(UpdateStrategyError, match='cannot set Recreate on Deployment "rel-meho"'):
        await switch_to_recreate(api, "meho", "rel-meho")


def test_main_needs_namespace_and_name(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("POD_NAMESPACE", raising=False)
    monkeypatch.delenv("MEHO_DEPLOYMENT_NAME", raising=False)

    assert update_strategy.main() == 1
    assert (
        "update_strategy_failed: POD_NAMESPACE and MEHO_DEPLOYMENT_NAME" in capfd.readouterr().err
    )


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        ("changed", "removed the rollingUpdate block and set Recreate"),
        ("unchanged", "nothing to change"),
        ("absent", "not found"),
    ],
)
def test_main_says_what_happened(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], outcome: str, said: str
) -> None:
    monkeypatch.setenv("POD_NAMESPACE", "meho")
    monkeypatch.setenv("MEHO_DEPLOYMENT_NAME", "rel-meho")

    async def fake_run(namespace: str, name: str) -> str:
        assert (namespace, name) == ("meho", "rel-meho")
        return outcome

    monkeypatch.setattr(update_strategy, "_run_in_cluster", fake_run)

    assert update_strategy.main() == 0
    out = capfd.readouterr().out
    assert out.startswith('update_strategy: Deployment "rel-meho": ')
    assert said in out

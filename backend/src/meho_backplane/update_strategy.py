# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Before a chart upgrade, switch the live MEHO Deployment to ``Recreate``.

The chart runs this module as a ``pre-upgrade`` Helm hook Job
(``deploy/charts/meho/templates/update-strategy-job.yaml``). The chart renders
the Job only when the chart asks for ``Recreate`` and the live Deployment still
has a ``spec.strategy.rollingUpdate`` block (#5962).

Why: a chart before #5024 set no update strategy, so Kubernetes filled in
``RollingUpdate`` and a ``rollingUpdate`` block. Helm 4 upgrades with
server-side apply. Helm never set that block, so the apply does not remove it,
and Kubernetes refuses ``Recreate`` together with a ``rollingUpdate`` block.
This Job removes the block and sets ``Recreate`` in one merge patch, before
Helm applies the new chart. The pod template does not change, so no pod
restarts here.

Exit code ``0`` on success, also when there is nothing to do. Exit code ``1``
on any failure, with the reason on stderr as
``update_strategy_failed: <sentence>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Literal

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

NAMESPACE_ENV = "POD_NAMESPACE"
DEPLOYMENT_ENV = "MEHO_DEPLOYMENT_NAME"
# The name the change carries in the Deployment's managedFields.
FIELD_MANAGER = "meho-update-strategy"
_API_TIMEOUT_S = 30
_PATCH = {"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}}}

Outcome = Literal["changed", "unchanged", "absent"]


class UpdateStrategyError(Exception):
    """A failure the Job reports in one sentence."""


def _describe(exc: ApiException) -> str:
    """Status line plus the API's own message; never headers or the raw body."""
    message = ""
    try:
        body = json.loads(exc.body) if exc.body else None
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict):
        message = str(body.get("message") or "")
    status = f"HTTP {exc.status} {exc.reason}"
    return f"{status}: {message}" if message else status


async def switch_to_recreate(api: Any, namespace: str, name: str) -> Outcome:
    """Remove the ``rollingUpdate`` block and set ``Recreate`` on the Deployment.

    ``api`` is an ``AppsV1Api``. Returns ``absent`` when the Deployment does
    not exist, ``unchanged`` when it has no ``rollingUpdate`` block, and
    ``changed`` after the patch.
    """
    try:
        deployment = await api.read_namespaced_deployment(
            name, namespace, _request_timeout=_API_TIMEOUT_S
        )
    except ApiException as exc:
        if exc.status == 404:
            return "absent"
        raise UpdateStrategyError(f'cannot read Deployment "{name}": {_describe(exc)}') from None
    strategy = deployment.spec.strategy if deployment.spec else None
    if strategy is None or strategy.rolling_update is None:
        return "unchanged"
    try:
        await api.patch_namespaced_deployment(
            name,
            namespace,
            _PATCH,
            field_manager=FIELD_MANAGER,
            _content_type="application/merge-patch+json",
            _request_timeout=_API_TIMEOUT_S,
        )
    except ApiException as exc:
        raise UpdateStrategyError(
            f'cannot set Recreate on Deployment "{name}": {_describe(exc)}'
        ) from None
    return "changed"


async def _run_in_cluster(namespace: str, name: str) -> Outcome:
    # kubernetes_asyncio ships no type hints (see the mypy override in pyproject.toml).
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    async with client.ApiClient() as api_client:
        return await switch_to_recreate(client.AppsV1Api(api_client), namespace, name)


_SAY = {
    "changed": 'Deployment "{name}": removed the rollingUpdate block and set Recreate',
    "unchanged": 'Deployment "{name}": nothing to change',
    "absent": 'Deployment "{name}": not found, nothing to change',
}


def main() -> int:
    try:
        namespace = os.environ.get(NAMESPACE_ENV)
        name = os.environ.get(DEPLOYMENT_ENV)
        if not namespace or not name:
            raise UpdateStrategyError(
                f"{NAMESPACE_ENV} and {DEPLOYMENT_ENV} must be set; the chart sets them on the Job."
            )
        outcome = asyncio.run(_run_in_cluster(namespace, name))
    except UpdateStrategyError as exc:
        print(f"update_strategy_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"update_strategy_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"update_strategy: {_SAY[outcome].format(name=name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

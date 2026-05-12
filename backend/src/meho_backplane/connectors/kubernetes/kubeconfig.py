# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Kubeconfig loading for the Kubernetes connector.

The skeleton lands ahead of the Target model (G0.3 / #224) and the
operator-context Vault read path. Both surfaces are stubbed via a
narrow :class:`KubernetesTargetLike` Protocol plus an injectable
``kubeconfig_loader`` callable — once G0.3 lands its concrete ``Target``
model, the structural shape here will satisfy the Protocol unchanged.

The default loader, :func:`load_kubeconfig_from_vault`, raises
:exc:`NotImplementedError` until G0.3 + the operator-context Vault
connector reads land. T2+ tasks under #320 wire the live read by
overriding the loader at connector construction time; unit and
integration tests inject their own (mock) loader the same way.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import yaml

__all__ = [
    "KubeconfigLoader",
    "KubernetesTargetLike",
    "load_kubeconfig_from_vault",
    "parse_kubeconfig_yaml",
]


@runtime_checkable
class KubernetesTargetLike(Protocol):
    """Minimum target shape :class:`KubernetesConnector` reads.

    Structural Protocol — once G0.3 (#224) lands a concrete ``Target``
    model in :mod:`meho_backplane.targets`, the model satisfies this
    Protocol without code changes here. ``secret_ref`` is the Vault
    path the operator-context Vault read resolves to a kubeconfig YAML
    string under the ``kubeconfig`` field (consumer's ``targets.yaml``
    convention, locked in decision #8).
    """

    name: str
    host: str
    port: int | None
    secret_ref: str


KubeconfigLoader = Callable[[KubernetesTargetLike], Awaitable[dict[str, Any]]]
"""Async callable resolving a target to a parsed kubeconfig dict.

Injection point for the connector's auth flow. Tests pass a mock
returning a pre-built dict; production passes
:func:`load_kubeconfig_from_vault` (or a wrapper bound to an operator
context). The dict shape matches what
``kubernetes_asyncio.config.new_client_from_config_dict`` accepts —
top-level keys ``apiVersion`` / ``clusters`` / ``contexts`` /
``current-context`` / ``users``.
"""


def parse_kubeconfig_yaml(kubeconfig_text: str) -> dict[str, Any]:
    """Parse a kubeconfig YAML string into the dict shape k_a consumes.

    Wraps :func:`yaml.safe_load` so callers get a single failure path
    when a Vault secret's ``kubeconfig`` field is malformed. Both
    failure shapes — syntactically invalid YAML (parser/scanner
    errors) and structurally wrong YAML (scalar, empty, list) — raise
    :exc:`ValueError`, never the underlying :exc:`yaml.YAMLError`
    subclass, so callers don't need to import ``yaml`` just to catch
    parse failures.
    """
    try:
        parsed = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"kubeconfig YAML failed to parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"kubeconfig YAML must parse to a mapping, got {type(parsed).__name__}")
    return parsed


async def load_kubeconfig_from_vault(target: KubernetesTargetLike) -> dict[str, Any]:
    """Default kubeconfig loader — Vault read by ``target.secret_ref``.

    Stub until G0.3 (#224) lands the ``Target`` model and the
    operator-context Vault read path. T2+ tasks under #320 override
    this loader with the live implementation. Raising
    :exc:`NotImplementedError` here keeps the wiring shape stable: a
    production caller without an override receives a clear error
    rather than a silent fallback or a hallucinated kubeconfig.
    """
    raise NotImplementedError(
        "load_kubeconfig_from_vault requires G0.3 Target model + operator-context "
        f"Vault read; target={target.name!r} secret_ref={target.secret_ref!r}. "
        "Inject a custom kubeconfig_loader on KubernetesConnector until G0.3 lands."
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which parts of the policy are on. The one place that decides it.

Three parts of the policy have their own switch, each off by default:

* ``POLICY_CLASSIFICATION``: category, labels and "always wait" on
  connectors, operation groups and operations;
* ``POLICY_ENGINE_SHADOW``: the policy evaluator runs next to today's
  checks and decides nothing;
* ``POLICY_APPROVER_ROUTING``: approvals go to the teams the policy file
  names.

The one switch ``CUSTOM_POLICY`` (off by default) turns on all three. A
part counts as on when its own switch is on or ``CUSTOM_POLICY`` is on.
Each old switch still works alone. :func:`custom_policy_on` answers
whether ``CUSTOM_POLICY`` itself is on. See ``docs/codebase/policy.md``.

Code asks these functions, never the settings or the environment.
"""

from __future__ import annotations

import os

from meho_backplane.settings import Settings, parse_bool_env

__all__ = [
    "approver_routing_on",
    "classification_on",
    "classification_on_in_environment",
    "custom_policy_on",
    "custom_policy_on_in_environment",
    "engine_shadow_on",
]


def custom_policy_on(settings: Settings) -> bool:
    """True when ``CUSTOM_POLICY`` is on."""
    return settings.custom_policy


def classification_on(settings: Settings) -> bool:
    """True when ``POLICY_CLASSIFICATION`` or ``CUSTOM_POLICY`` is on."""
    return settings.policy_classification or custom_policy_on(settings)


def engine_shadow_on(settings: Settings) -> bool:
    """True when ``POLICY_ENGINE_SHADOW`` or ``CUSTOM_POLICY`` is on."""
    return settings.policy_engine_shadow or custom_policy_on(settings)


def approver_routing_on(settings: Settings) -> bool:
    """True when ``POLICY_APPROVER_ROUTING`` or ``CUSTOM_POLICY`` is on."""
    return settings.policy_approver_routing or custom_policy_on(settings)


def classification_on_in_environment() -> bool:
    """:func:`classification_on`, read from the environment.

    For the registrars of the ``policy.*`` operations. They run also where
    the other settings are not set (registry checks in tests), so they
    cannot build :class:`~meho_backplane.settings.Settings`. The parsing is
    the same as in ``Settings``.
    """
    return (
        parse_bool_env(os.environ.get("POLICY_CLASSIFICATION")) or custom_policy_on_in_environment()
    )


def custom_policy_on_in_environment() -> bool:
    """:func:`custom_policy_on`, read from the environment.

    For code that runs before the settings are loaded, as
    :func:`classification_on_in_environment` is. The parsing is the same as
    in ``Settings``.
    """
    return parse_bool_env(os.environ.get("CUSTOM_POLICY"))

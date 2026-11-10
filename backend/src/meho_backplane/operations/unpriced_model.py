# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whether MEHO has a price for an agent model (#5771).

A model with no row in
:data:`~meho_backplane.operations.identity_budget.MODEL_PRICING` costs 0
(see :func:`~meho_backplane.operations.identity_budget.compute_cost`), so
the spend limit per login does not count runs on it. This module makes
that visible: a warning at start, and the ``agent_model_spend`` field of
the status read.
"""

from __future__ import annotations

from typing import Final

import structlog

from meho_backplane.operations.identity_budget import MODEL_PRICING

__all__ = ["is_model_priced", "warn_if_model_unpriced"]

_log = structlog.get_logger(__name__)

#: Provider of a model id without a ``provider:`` part. The same rule as
#: ``meho_backplane.agent.invocation._split_model_id``, which builds the
#: id the cost lookup uses.
_DEFAULT_PROVIDER: Final[str] = "anthropic"


def is_model_priced(model_id: str) -> bool:
    """Return whether runs on *model_id* add their cost to the spend limits.

    A bare id (no ``provider:`` part) counts as an Anthropic id, the same
    way the runtime stores it.
    """
    if ":" not in model_id:
        model_id = f"{_DEFAULT_PROVIDER}:{model_id}"
    return model_id in MODEL_PRICING


def warn_if_model_unpriced(model_id: str) -> bool:
    """Log one warning when *model_id* has no price; return whether it has one.

    Called at start with ``settings.agent_default_model``. MEHO still
    starts: an install may pick a new model before MEHO knows its price.
    """
    priced = is_model_priced(model_id)
    if not priced:
        _log.warning(
            "agent_default_model_unpriced",
            model_id=model_id,
            reason=(
                "no entry in MODEL_PRICING; runs on this model cost 0, "
                "so spend limits do not count them"
            ),
        )
    return priced

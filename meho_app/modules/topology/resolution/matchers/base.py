# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Base matcher ABC for deterministic entity resolution.
"""

from abc import ABC, abstractmethod

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution.evidence import MatchEvidence, MatchPriority


class BaseMatcher(ABC):
    """Abstract base class for attribute matchers."""

    priority: MatchPriority

    @abstractmethod
    def match(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """Return MatchEvidence if entities match, None otherwise."""
        ...

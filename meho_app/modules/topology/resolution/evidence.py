# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Match evidence data structures for deterministic entity resolution.
"""

from dataclasses import dataclass
from enum import IntEnum


class MatchPriority(IntEnum):
    """Priority ordering for matchers. Lower value = higher priority."""

    PROVIDER_ID = 1
    IP_ADDRESS = 2
    HOSTNAME = 3


@dataclass
class MatchEvidence:
    """Evidence of a deterministic match between two entities."""

    match_type: str
    matched_values: dict
    confidence: float
    auto_confirm: bool

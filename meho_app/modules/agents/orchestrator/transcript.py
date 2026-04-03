# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Transcript collector management for the Orchestrator Agent.

This module re-exports the shared create_transcript_collector function
for backward compatibility. New code should import directly from
meho_app.modules.agents.persistence.helpers.
"""

from __future__ import annotations

# Re-export from shared location for backward compatibility
from meho_app.modules.agents.persistence.helpers import create_transcript_collector

__all__ = ["create_transcript_collector"]

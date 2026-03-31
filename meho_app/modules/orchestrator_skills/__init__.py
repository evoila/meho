# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Orchestrator Skills module for MEHO.

Provides a platform for orchestrator-level cross-system reasoning skills.
Unlike connector skills (file-based, loaded by specialist agents), orchestrator
skills are DB-stored, tenant-scoped, and injected into the orchestrator's
system prompt. Summaries are always present; full content loaded on-demand.
"""

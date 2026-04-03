# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Scheduled Tasks module for MEHO.

Provides cron-based task scheduling that creates tenant-visible group sessions
with predefined prompts. Uses APScheduler with PostgreSQL persistence for
restart-safe scheduling and croniter for cron expression parsing/validation.
"""

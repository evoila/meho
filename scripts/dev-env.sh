#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Compatibility shim. The development CLI lives at `meho-dev` (a Typer app
# at `meho_app/tools/dev.py`, see #310 / PR-M). This script forwards to it
# unchanged so muscle memory, the Makefile, `preflight.sh`,
# `validate-install.sh`, and existing docs keep working while doc
# reconciliation lands in Phase 7 PR-N.
#
# The previous 580-line bash implementation grew accidental business logic
# (PID tracking, signal handling, .env sourcing, hardcoded migration
# loops). All of that now lives in tested Python.
#
# Override `MEHO_DEV_USE_BASH=1` to disable the shim during local
# debugging; nothing in CI or the docs sets that env var.

set -euo pipefail

if [[ "${MEHO_DEV_USE_BASH:-0}" == "1" ]]; then
  echo "MEHO_DEV_USE_BASH=1 set but the bash implementation has been removed." >&2
  echo "Run 'meho-dev <command>' (or 'uv run meho-dev <command>') instead." >&2
  exit 2
fi

exec uv run meho-dev "$@"

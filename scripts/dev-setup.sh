#!/bin/bash
# One-time setup for developers after cloning the repo.
# Installs pre-commit hooks for all stages declared in .pre-commit-config.yaml
# (pre-commit, pre-push, commit-msg).
#
# Usage: ./scripts/dev-setup.sh
set -e

echo "=== MEHO Developer Setup ==="

# Legacy clones may still have `core.hooksPath = .githooks` set, pointing at a
# directory that no longer exists. pre-commit refuses to install in this state
# ("Cowardly refusing to install hooks with core.hooksPath set"), so unset it
# preemptively.
if legacy_path=$(git config --get core.hooksPath 2>/dev/null); then
  echo "Unsetting legacy core.hooksPath override (was: ${legacy_path})"
  git config --unset core.hooksPath
fi

# Prefer the project venv's pre-commit (installed via `uv sync --group dev`)
# over a global install — matches the documented dev setup and avoids forcing
# a second global tool on PATH.
if [ -x .venv/bin/pre-commit ]; then
  PRE_COMMIT=".venv/bin/pre-commit"
elif command -v pre-commit >/dev/null 2>&1; then
  PRE_COMMIT="pre-commit"
else
  echo "ERROR: 'pre-commit' not found in .venv/bin or on PATH." >&2
  echo "Install via one of:" >&2
  echo "  uv sync --group dev      (project-local, recommended)" >&2
  echo "  uv tool install pre-commit" >&2
  echo "  pipx install pre-commit" >&2
  exit 1
fi

"$PRE_COMMIT" install --install-hooks

echo ""
echo "Setup complete. Pre-commit hooks installed (pre-commit, pre-push, commit-msg)."
echo "See AGENTS.md § Pre-commit setup for the rules and troubleshooting."

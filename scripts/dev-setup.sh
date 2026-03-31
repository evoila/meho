#!/bin/bash
# One-time setup for developers after cloning the private repo.
# Configures git hooks and verifies the development environment.
#
# Usage: ./scripts/dev-setup.sh
set -e

echo "=== MEHO Developer Setup ==="
echo ""

# Configure git hooks
echo "Configuring git hooks..."
git config core.hooksPath .githooks
chmod +x .githooks/*
echo "  Pre-push hook installed (blocks direct push to public repo)"
echo ""

echo "Setup complete. Push to origin (private repo) as normal."
echo "The mirror GitHub Action syncs changes to the public repo automatically."

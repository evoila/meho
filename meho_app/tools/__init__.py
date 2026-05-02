# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Developer-facing CLIs that ship with the wheel.

Currently exposes the ``meho-dev`` command (see :mod:`meho_app.tools.dev`),
which replaces the pre-#310 ``scripts/dev-env.sh`` bash wrapper. The CLI is
intentionally light on logic -- it is a typed dispatch layer over docker
compose, alembic, and a handful of helper scripts under ``scripts/`` -- so
each subcommand stays unit-testable.

New CLIs added here must register their entry point in
``[project.scripts]`` of ``pyproject.toml`` to be picked up by the wheel.
"""

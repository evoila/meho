# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-run one-shot scripts.

Members here are invoked by operators from a repo checkout (``uv run
python -m scripts.<name>``); they are intentionally NOT shipped in the
wheel (see ``backend/pyproject.toml`` -- ``[tool.hatch.build.targets.
wheel] packages = ["src/meho_backplane"]``). Production code paths that
need the same behaviour at request time go through the MCP /
REST surfaces (``meho.connector.review.edit_op`` etc.).
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks package -- versioned procedure templates + guided execution.

Initiative #1197 (G12.2 Template lifecycle), under Goal #1195 (G12
Runbooks). A runbook template is an ordered list of steps an operator
walks through during a procedure; each step gates advance on a
``verify`` outcome. Templates version on edit and lock on publish.

This package's first file is :mod:`meho_backplane.runbooks.schemas` --
the Pydantic shape contract (discriminated step / verify unions,
substitution allowlist) every downstream surface (G12.2 service,
routes, MCP tools; G12.3 execution engine) validates against. The
storage-level shapes live as SQLAlchemy models in
:mod:`meho_backplane.db.models` (G12.1-T1, #1292).
"""

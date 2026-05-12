# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tool implementations — auto-discovered at app startup.

Every module under this subpackage that calls
:func:`~meho_backplane.mcp.registry.register_mcp_tool` at its top level
registers a tool against the MCP server. The
:func:`~meho_backplane.mcp.registry.eager_import_mcp_modules` helper —
invoked from the FastAPI ``lifespan`` (see
:mod:`meho_backplane.main`) — walks every module under this package via
``pkgutil.iter_modules`` so the registrations land before the first
``tools/list`` request arrives.

T4 (#249) lands the first tool: ``meho.status`` at
:mod:`meho_backplane.mcp.tools.meho_status` — the reference impl
downstream G3-G9 connector tools copy.
"""

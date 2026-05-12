# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP resource-template implementations — auto-discovered at app startup.

Every module under this subpackage that calls
:func:`~meho_backplane.mcp.registry.register_mcp_resource` at its top
level registers a resource template against the MCP server. The
:func:`~meho_backplane.mcp.registry.eager_import_mcp_modules` helper —
invoked from the FastAPI ``lifespan`` (see
:mod:`meho_backplane.main`) — walks every module under this package via
``pkgutil.iter_modules`` so the registrations land before the first
``resources/templates/list`` request arrives.

T3 (#248) ships the registry shell; this subpackage stays empty until
T4 (#249) lands the reference ``meho://tenant/{tenant_id}/info``
resource.
"""

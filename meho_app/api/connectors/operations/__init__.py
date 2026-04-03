# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector operations sub-modules.

Each sub-module contains related route handlers for connector management.
"""

from meho_app.api.connectors.operations.connector_operations import router as operations_router
from meho_app.api.connectors.operations.credentials import router as credentials_router
from meho_app.api.connectors.operations.crud import router as crud_router
from meho_app.api.connectors.operations.endpoints import router as endpoints_router
from meho_app.api.connectors.operations.export_import import router as export_import_router
from meho_app.api.connectors.operations.soap import router as soap_router
from meho_app.api.connectors.operations.specs import router as specs_router
from meho_app.api.connectors.operations.testing import router as testing_router
from meho_app.api.connectors.operations.types import router as types_router
from meho_app.api.connectors.operations.vmware import router as vmware_router

__all__ = [
    "credentials_router",
    "crud_router",
    "endpoints_router",
    "export_import_router",
    "operations_router",
    "soap_router",
    "specs_router",
    "testing_router",
    "types_router",
    "vmware_router",
]

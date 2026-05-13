# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vault — VaultConnector package.

Importing the package registers :class:`VaultConnector` against the
connector registry. The registry is imported eagerly at app startup via
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
(called from the FastAPI lifespan hook); by the time the first request
arrives, ``get_connector("vault")`` always returns :class:`VaultConnector`.
"""

from meho_backplane.connectors.registry import register_connector
from meho_backplane.connectors.vault.connector import VaultConnector, VaultTarget

register_connector("vault", VaultConnector)

__all__ = ["VaultConnector", "VaultTarget"]

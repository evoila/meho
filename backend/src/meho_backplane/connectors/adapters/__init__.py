# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.adapters — transport-layer adapter base classes.

Current adapters:
* :class:`~meho_backplane.connectors.adapters.http.HttpConnector` — abstract
  HTTP-API connector with httpx.AsyncClient pooling, retry/timeout, and
  auth-bearer plumbing (G0.2-T3).
"""

from meho_backplane.connectors.adapters.http import HttpConnector

__all__ = ["HttpConnector"]

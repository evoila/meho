# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""UI surface for the ``event_source`` registry (#2880).

Server-rendered tenant-scoped CRUD at ``/ui/event-sources``: a list page,
create / edit forms, and a soft-delete action. Reads run at operator
role; writes gate to ``tenant_admin`` (403 otherwise) and reuse the REST
handler functions in :mod:`meho_backplane.api.v1.event_source` so the UI
never re-implements persistence or Vault secret custody.
"""

from meho_backplane.ui.routes.event_source.routes import build_event_source_router

__all__ = ["build_event_source_router"]

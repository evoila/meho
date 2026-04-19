# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Knowledge chunk reconciler -- self-healing startup check.

Applies the Kubernetes controller pattern to knowledge indexing:
on every startup, compare desired state (one knowledge_chunk per
connector_operation) against actual state (rows in knowledge_chunk
table). If any typed connector has zero chunks, recreate them.

This is the single safety net that catches ALL failure modes:
  - Embedding provider down during connector creation
  - Rate limits during initial chunk generation
  - Database migrations that dropped chunks
  - Interrupted deployments
  - Manual deletions

The per-connector sync files handle version upgrades (new operations).
This reconciler handles chunk presence (self-healing).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.knowledge.models import KnowledgeChunkModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = get_logger(__name__)

# connector_type -> (module_path, function_name)
# Each function has signature: (knowledge_store, connector_id, connector_name, tenant_id) -> int
_CHUNK_SYNC_DISPATCH: dict[str, tuple[str, str]] = {
    "kubernetes": (
        "meho_app.modules.connectors.kubernetes.sync",
        "_sync_kubernetes_knowledge_chunks",
    ),
    "vmware": (
        "meho_app.modules.connectors.vmware.sync",
        "_sync_vmware_knowledge_chunks",
    ),
    "gcp": (
        "meho_app.modules.connectors.gcp.sync",
        "_sync_gcp_knowledge_chunks",
    ),
    "proxmox": (
        "meho_app.modules.connectors.proxmox.sync",
        "_sync_proxmox_knowledge_chunks",
    ),
    "prometheus": (
        "meho_app.modules.connectors.prometheus.sync",
        "_sync_prometheus_knowledge_chunks",
    ),
    "loki": (
        "meho_app.modules.connectors.loki.sync",
        "_sync_loki_knowledge_chunks",
    ),
    "tempo": (
        "meho_app.modules.connectors.tempo.sync",
        "_sync_tempo_knowledge_chunks",
    ),
    "alertmanager": (
        "meho_app.modules.connectors.alertmanager.sync",
        "_sync_alertmanager_knowledge_chunks",
    ),
    "jira": (
        "meho_app.modules.connectors.jira.sync",
        "_sync_jira_knowledge_chunks",
    ),
    "confluence": (
        "meho_app.modules.connectors.confluence.sync",
        "_sync_confluence_knowledge_chunks",
    ),
    "email": (
        "meho_app.modules.connectors.email.sync",
        "_sync_email_knowledge_chunks",
    ),
    "argocd": (
        "meho_app.modules.connectors.argocd.sync",
        "_sync_argocd_knowledge_chunks",
    ),
    "github": (
        "meho_app.modules.connectors.github.sync",
        "_sync_github_knowledge_chunks",
    ),
}


async def reconcile_knowledge_chunks(
    session: AsyncSession,
    knowledge_store: KnowledgeStore,
) -> int:
    """Ensure every typed connector has knowledge chunks indexed.

    Queries all typed connectors that SHOULD have chunks (they have
    operations in connector_operation) and checks whether matching
    knowledge_chunk rows exist. For any connector with zero chunks,
    calls the connector-specific chunk creation function.

    Returns:
        Number of connectors that were repaired.
    """
    typed_connectors = list(_CHUNK_SYNC_DISPATCH.keys())

    # Single query: all typed connectors with their operation count
    # and knowledge chunk count
    connector_stmt = (
        select(ConnectorModel)
        .where(ConnectorModel.connector_type.in_(typed_connectors))
        .where(ConnectorModel.is_active.is_(True))
    )
    result = await session.execute(connector_stmt)
    connectors = result.scalars().all()

    if not connectors:
        return 0

    repaired = 0

    for connector in connectors:
        ctype = connector.connector_type
        cid = str(connector.id)
        tid = str(connector.tenant_id)

        if ctype not in _CHUNK_SYNC_DISPATCH:
            continue

        # Count existing knowledge chunks for this connector
        chunk_count_stmt = select(func.count()).where(
            KnowledgeChunkModel.tenant_id == tid,
            KnowledgeChunkModel.search_metadata["connector_id"].astext == cid,
            KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation",
        )
        chunk_count = (await session.execute(chunk_count_stmt)).scalar() or 0

        if chunk_count > 0:
            continue

        # Zero chunks -- repair
        module_path, func_name = _CHUNK_SYNC_DISPATCH[ctype]  # type: ignore[index]  # SQLAlchemy ORM attribute access
        cname = str(connector.name) if connector.name else ctype

        try:
            # Safe (non-literal-import): module paths from _CHUNK_SYNC_DISPATCH dict with hardcoded entries
            mod = importlib.import_module(module_path)
            sync_fn = getattr(mod, func_name)
            created = await sync_fn(
                knowledge_store=knowledge_store,
                connector_id=cid,
                connector_name=cname,
                tenant_id=tid,
            )
            await session.commit()
            logger.warning(
                f"Reconciled {ctype} connector '{cname}': created {created} knowledge chunks"
            )
            repaired += 1
        except Exception as e:
            logger.error(
                f"Failed to reconcile {ctype} connector '{cname}': {e}",
                exc_info=True,
            )

    return repaired

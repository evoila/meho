# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stage and cut over source-qualified topology identity (#3749).

Before this step a topology node is unique on ``(tenant_id, kind,
name)``, so two targets that both report a ``default`` namespace share
one row. The repair moves the graph to ``(tenant_id, source_scope, kind,
name)`` in two operator steps:

* :func:`~.stage.stage_scope_repair` asks every target again and saves
  a manifest. It never writes to the graph, and graph writes wait while
  it runs.
* :func:`~.cutover.cut_over` applies a staged manifest in one
  transaction, or refuses and changes nothing.

Run both from the backplane image::

    python -m meho_backplane.topology.scope_repair stage
    python -m meho_backplane.topology.scope_repair show <manifest-id>
    python -m meho_backplane.topology.scope_repair cutover <manifest-id>

See ``docs/codebase/topology.md`` (section "Source scope cut-over").
"""

from meho_backplane.topology.scope_repair.cutover import (
    CutoverRefusedError,
    CutoverResult,
    cut_over,
)
from meho_backplane.topology.scope_repair.plan import RepairPlan, build_plan, snapshot_version
from meho_backplane.topology.scope_repair.stage import AlreadyScopedError, stage_scope_repair
from meho_backplane.topology.scope_repair.store import StoredManifest, load_manifest

__all__ = [
    "AlreadyScopedError",
    "CutoverRefusedError",
    "CutoverResult",
    "RepairPlan",
    "StoredManifest",
    "build_plan",
    "cut_over",
    "load_manifest",
    "snapshot_version",
    "stage_scope_repair",
]

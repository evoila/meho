# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backfill curated ``when_to_use`` onto existing ``operation_group`` rows.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-21

Initiative #772 (G0.9.1 v0.3.2 dogfood hardening), Task #774 (T2,
Signal #5 refined). The 2026-05-21 RDC second-cycle dogfood against
the v0.3.1 deploy surfaced that ``list_operation_groups bind9-ssh-9.x``
still returns the auto-derived template literal --

    ``Operations grouped under 'zone' for bind9 bind9-ssh.``

-- for every connector that existed at v0.3.0, even though the bind9 /
kubernetes / vault / vmware-rest typed connectors curated rich,
agent-actionable per-group blurbs in source as part of PRs #731 / #732
(G0.9-T4a / T4b).

Root cause is the first-write-wins contract on
:func:`~meho_backplane.operations.typed_register._resolve_or_create_group`:
when a connector re-registers against a ``(tenant_id=NULL, product,
version, impl_id, group_key)`` row that already exists, the helper
returns the existing row's id **without** writing the (now curated)
``when_to_use`` blurb back. The contract is intentional -- it protects
operator edits made via ``meho.connector.edit_group`` -- but it leaves
existing v0.3.0-era rows stuck on the kill-switched template.

PR #731 killed the auto-derive default at the Python boundary so any
new connector cannot ship a placeholder; PR #732 curated the strings
in source. Neither path reaches the live DB on upgrade. This
migration is the one-shot backfill that closes the gap on existing
deployments.

Fix shape -- Alembic data migration
-----------------------------------

Per Alembic's cookbook (`Data Migrations
<https://alembic.sqlalchemy.org/en/latest/cookbook.html>`_), small
data migrations stay self-contained: a lightweight :func:`sa.table` /
:func:`sa.column` shim mirrors the columns the migration touches,
without importing the live ORM model (which would pin the migration
to one moment in the schema's history and break against any future
column add). The same self-contained discipline migration ``0010``
documents at the helper boundary.

Detection predicate
-------------------

The auto-derive template the v0.6 substrate shipped (and PR #731
killed) is exactly::

    f"Operations grouped under {group_key!r} for {product} {impl_id}."

-> ``Operations grouped under 'kv' for vault vault.`` etc. Every row
that still carries this shape begins with the literal prefix
``Operations grouped under ``. Operator-edited strings (via
``meho.connector.edit_group``) and curated strings registered after
PR #732 do not. The migration filters rows on
``when_to_use LIKE 'Operations grouped under%'`` (portable on PG +
SQLite) so an operator who hand-edited the blurb post-deploy keeps
their edit intact -- the row-narrowing predicate is "this row still
holds the kill-switched template", not "this row was curated by
T4b".

The same substring (``"Operations grouped under"``) is the regression
sentinel :mod:`tests.test_typed_connectors_metadata` already uses to
prove no new connector ships the template; reusing it here keeps the
template's identity defined in exactly one place across the codebase.

Curated payload
---------------

The five connectors that shipped curated ``when_to_use`` strings as
part of PR #732 (G0.9-T4b) are mirrored verbatim below, keyed by the
natural-key tuple ``(product, version, impl_id, group_key)``:

* ``bind9`` 9.x / ``bind9-ssh`` -- four groups (identity, zone,
  record, config), source:
  :data:`~meho_backplane.connectors.bind9.connector._WHEN_TO_USE_BY_GROUP`.
* ``k8s`` 1.x / ``k8s`` -- seven groups (cluster, inventory,
  workload, network, config, events, logs), source:
  :data:`~meho_backplane.connectors.kubernetes.connector._WHEN_TO_USE_BY_GROUP`.
* ``vault`` 1.x / ``vault`` -- three groups (kv, auth, sys),
  source: ``vault.ops.register_vault_typed_operations`` /
  ``ops_auth.register_vault_auth_typed_operations`` /
  ``ops_sys.register_vault_sys_typed_operations``.
* ``vmware`` 9.0 / ``vmware-rest`` -- seven groups (cluster, events,
  performance, storage, networking, vm, host), source:
  :data:`~meho_backplane.connectors.vmware_rest.composites._register._WHEN_TO_USE_BY_GROUP`.
* ``harbor`` 2.x / ``harbor-rest`` -- one group (robot), curated
  here (Signal #5 refined: PR #732 didn't cover Harbor; the
  ``"TODO: curate (T4b #732)"`` placeholders in source are replaced
  in this PR alongside the migration).

The strings are inlined rather than imported because migrations must
be self-contained -- importing connector modules from a migration
pins the migration to those modules' current API and breaks the
schema's history-replay contract (e.g. when the connector module
later moves, renames, or splits).

Reversibility contract
----------------------

``downgrade()`` is a documented no-op. Reconstructing the
kill-switched template text per ``(product, group_key, impl_id)``
serves no operator need -- the template carried no information the
operator can usefully see, and an emergency rollback wants the
schema reverted to the pre-migration shape (table-structure-only),
not the prose reverted to a string nobody wanted to begin with.

The narrower "rollback restores the original prose" semantic would
require remembering each row's pre-upgrade value (a second migration
table or a copy column); the cost outweighs the upside. The
upgrade-only contract is the same shape migration ``0010``'s
``downgrade()`` uses for its row pre-check: explicit refusal beats
silent partial recovery.

Idempotency
-----------

The ``WHERE when_to_use LIKE 'Operations grouped under%'`` predicate
makes the UPDATE safe to re-run: rows already migrated no longer
match the filter, so a second invocation is a no-op. This matters
for the test suite (which exercises upgrade -> downgrade -> upgrade
cycles) and for the test-containers replay path that mirrors prod
deploys.

Cross-references
----------------

* Initiative #772 (G0.9.1) -- v0.3.2 dogfood hardening rollup.
* Sibling PR #731 -- killed the auto-derive default at the Python
  boundary (the structural fix; this migration is the data fix).
* Sibling PR #732 -- curated the per-group strings in source for
  the four typed connectors shipped at v0.3.1 (Harbor was new and
  not covered; this PR adds it).
* :mod:`tests.test_typed_connectors_metadata` -- regression that
  no shipped connector serves the template substring on a fresh DB.
* :mod:`tests.test_migration_0011_backfill_when_to_use` -- proves
  the template-match-only behaviour: operator-edited rows are
  preserved verbatim across upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Substring that uniquely identifies the auto-derive template the
#: v0.6 substrate shipped (and PR #731 killed). Every row whose
#: ``when_to_use`` starts with this prefix is on the kill-switched
#: template and is safe to rewrite; nothing else (curated source
#: strings, operator edits via ``meho.connector.edit_group``) ever
#: starts with these three words.
#:
#: Kept as a SQL LIKE pattern so the predicate is portable across PG
#: and SQLite without needing dialect-specific regex.
_TEMPLATE_LIKE_PATTERN: str = "Operations grouped under%"


#: Curated per-group ``when_to_use`` payload, mirrored verbatim from
#: source. Keyed by ``(product, version, impl_id, group_key)``. The
#: tuple is the natural key of an ``operation_group`` row for a
#: ``tenant_id IS NULL`` (built-in/global) row -- same shape the
#: partial unique index ``operation_group_global_idx`` (migration
#: ``0005``) enforces.
#:
#: Migrations must stay self-contained: we inline the curated text
#: rather than import the connector modules' ``_WHEN_TO_USE_BY_GROUP``
#: dicts. The source files remain the single source of truth for
#: *new* registrations; this dict is a snapshot for the *backfill*
#: of pre-v0.3.1 rows.
_CURATED_WHEN_TO_USE: dict[tuple[str, str, str, str], str] = {
    # bind9 -- mirrors
    # backend/src/meho_backplane/connectors/bind9/connector.py
    # _WHEN_TO_USE_BY_GROUP (PR #732).
    ("bind9", "9.x", "bind9-ssh", "identity"): (
        "Use for bind9 nameserver-identity questions before any per-"
        "zone or per-record drill-in: 'which BIND version is this "
        "target running and on which host OS?'. The single "
        "``bind9.about`` op returns vendor / product / version "
        "(parsed BIND <X.Y.Z>), full named banner, and the host OS "
        "identifier from ``/etc/os-release``. Call this first when "
        "the agent needs to pick a version-flavoured doc page from "
        "the knowledge base, or to confirm the nameserver is "
        "reachable before issuing higher-level DNS ops."
    ),
    ("bind9", "9.x", "bind9-ssh", "zone"): (
        "Use for zone-level inventory and metadata reads: list every "
        "zone the nameserver serves (``bind9.zone.list``) and read "
        "one named zone's metadata + SOA (``bind9.zone.read``). "
        "Read-only; never mutates zone state. The right group when "
        "the agent doesn't yet know which zone to target ('what "
        "zones does this nameserver host?') or needs the zone-level "
        "context (type / file / view binding / SOA serial) before "
        "drilling into records. Pair with the 'record' group once a "
        "zone is identified to query / add / remove RRs inside it, "
        "and with the 'config' group when the question is about "
        "named.conf-level wiring (views, zone clauses) rather than "
        "the zone's own contents."
    ),
    ("bind9", "9.x", "bind9-ssh", "record"): (
        "Use for record-level RR reads and mutations inside a known "
        "zone: query a specific name+type (``bind9.record.get``), "
        "add an RR atomically (``bind9.record.add``), or remove one "
        "(``bind9.record.remove``). Writes route through the atomic-"
        "apply primitive (rndc freeze / journal-sync / journal swap "
        "/ rndc thaw) so a failed apply leaves the zone untouched. "
        "``add`` / ``remove`` are mutating ops -- the future policy "
        "gate keys on their ``caution`` / ``dangerous`` safety_level. "
        "Typically reached after the 'zone' group identifies the "
        "target zone. Pair with the 'config' group when the change "
        "needs a view / zone-clause edit rather than an in-zone RR "
        "edit."
    ),
    ("bind9", "9.x", "bind9-ssh", "config"): (
        "Use for nameserver configuration reads and atomic config "
        "writes: dump the running named.conf "
        "(``bind9.config.show``), apply a single named.conf file "
        "(``bind9.config.apply_file``), apply a multi-file views "
        "bundle (``bind9.config.apply_views``), snapshot the current "
        "config + zones (``bind9.config.backup``), or reload via "
        "rndc (``bind9.config.reload``). ``apply_file`` and "
        "``apply_views`` route through the atomic-apply primitive "
        "(staged write + named-checkconf validation + rollback on "
        "failure); ``backup`` and ``reload`` do not (additive / "
        "single-rndc respectively). The right group for view-level "
        "or server-level changes -- per-RR edits live in the "
        "'record' group, zone-inventory questions in the 'zone' "
        "group. Mutating ops carry ``caution`` / ``dangerous`` "
        "safety_level."
    ),
    # kubernetes -- mirrors
    # backend/src/meho_backplane/connectors/kubernetes/connector.py
    # _WHEN_TO_USE_BY_GROUP (PR #732).
    ("k8s", "1.x", "k8s", "cluster"): (
        "Use for cluster-identity questions before any per-resource "
        "drill-in: 'which K8s flavour / distribution / version is this "
        "target running?' The single ``k8s.about`` op returns the "
        "product slug (rke2 / k3s / eks / gke / aks / vanilla) plus "
        "git_version. Call this first when the agent needs to pick a "
        "version-flavoured doc page from the knowledge base or to "
        "decide whether RKE2-specific vs vanilla-K8s behaviour applies "
        "to a downstream op."
    ),
    ("k8s", "1.x", "k8s", "inventory"): (
        "Use for cluster-wide 'what is in this cluster?' questions -- "
        "namespace and node enumeration plus the namespace-scoped "
        "``k8s.ls`` walker. The right group when the agent doesn't yet "
        "know which namespace to target. Pair with the 'workload' "
        "group once a namespace is identified (``k8s.ls /<ns>`` "
        "returns kind -> count summaries; drill into per-pod / per-"
        "deployment detail via workload-group ops)."
    ),
    ("k8s", "1.x", "k8s", "workload"): (
        "Use for per-namespace pod and deployment drill-in: "
        "``k8s.pod.list`` / ``k8s.pod.info`` / ``k8s.deployment.list`` "
        "/ ``k8s.deployment.info``. The right group once the operator "
        "knows the namespace (typically picked from the 'inventory' "
        "group first). Pair with the 'logs' group when investigating "
        "a CrashLoopBackOff pod, with 'events' when the failure is "
        "scheduler- or admission-controller-driven, and with 'network' "
        "to map a workload's Services and Ingresses."
    ),
    ("k8s", "1.x", "k8s", "network"): (
        "Use for service-routing and ingress questions: "
        "``k8s.service.list`` (ClusterIP / NodePort / LoadBalancer "
        "with endpoint counts) and ``k8s.ingress.list`` (hostname + "
        "path -> backend mappings). The right group for 'how is this "
        "workload exposed?' or 'which hostname routes to which "
        "service?'. Pair with the 'workload' group to map back to "
        "the pods behind each Service via label selectors."
    ),
    ("k8s", "1.x", "k8s", "config"): (
        "Use for ConfigMap data inspection: ``k8s.configmap.list`` "
        "(keys-only -- one row per ConfigMap with its data keys but "
        "no values) and ``k8s.configmap.info`` (full data payload for "
        "one named ConfigMap). The right group for 'what config is "
        "this workload reading?' or 'which env var lives in which "
        "ConfigMap?'. The list / info split is deliberate -- the "
        "agent surveys keys cheaply via list, then fetches values "
        "only for the specific ConfigMap it needs."
    ),
    ("k8s", "1.x", "k8s", "events"): (
        "Use for cluster-event observability and troubleshooting: "
        "``k8s.event.list`` returns the recent Event stream "
        "(scheduler decisions, admission-controller rejections, "
        "image-pull failures, OOMKilled signals, FailedMount, "
        "BackOff). The right group when a workload's status looks "
        "wrong and the agent needs the *why*. Pair with the 'workload' "
        "group to scope events to one pod / deployment, and with the "
        "'logs' group when the event points at a container-internal "
        "failure rather than a K8s-control-plane one."
    ),
    ("k8s", "1.x", "k8s", "logs"): (
        "Use for container stdout / stderr inspection: ``k8s.logs`` "
        "fetches a non-streaming chunk (kubectl-style --tail / "
        "--container / --since / --previous knobs). The right group "
        "once the agent has identified a specific pod (typically from "
        "'workload' or 'events') and needs the application's own log "
        "output. Streaming follow-mode ('kubectl logs -f') is "
        "deliberately out of scope -- request bounded chunks."
    ),
    # vault -- mirrors the inline strings in
    # backend/src/meho_backplane/connectors/vault/ops.py (kv),
    # ops_auth.py (auth), ops_sys.py (sys) (PR #732).
    ("vault", "1.x", "vault", "kv"): (
        "Use for HashiCorp Vault KV-v2 secret CRUD: read a secret, "
        "write a new version, list child paths under a folder, "
        "enumerate version history, soft-delete a version. The right "
        "group when the question names a specific secret path "
        "(``kubeconfig/<cluster>``, ``oidc/clients/<id>``, etc.) and "
        "the operator wants the value, the existence, or the "
        "version trail. Pair with the 'auth' group when 'can this "
        "identity reach that path?' precedes the actual read, and "
        "with the 'sys' group when the question is 'which KV "
        "mountpoint is this secret stored at?' rather than the "
        "value itself."
    ),
    ("vault", "1.x", "vault", "auth"): (
        "Use for per-role inspection of two specific Vault auth "
        "backends, ``userpass`` and ``approle``: list the roles "
        "defined on each backend and read one named role's config "
        "(``vault.auth.userpass.{list,read}`` / "
        "``vault.auth.approle.{list,read}``). Read-only; never "
        "creates, edits, or rotates roles. Route token-identity "
        "questions ('who am I in this Vault?'), auth-backend mount "
        "listings ('which auth methods are mounted at which "
        "paths?'), and any JWT / OIDC / kubernetes-backend role "
        "inspection to the 'sys' group (``sys.auth.list``) -- the "
        "auth group only covers the two backends named above. Pair "
        "with the 'kv' group for the post-auth 'what can this "
        "identity read?' follow-up."
    ),
    ("vault", "1.x", "vault", "sys"): (
        "Use for Vault target diagnostics and mount-surface "
        "introspection: 'is this Vault reachable / unsealed / "
        "serving traffic?' (sys.health), 'is it sealed and how "
        "many key shares are needed?' (sys.seal_status), 'which "
        "secret engines are mounted at which paths?' "
        "(sys.mounts.list), 'which auth methods are mounted?' "
        "(sys.auth.list). Read-only; returns mount metadata, never "
        "secret values. The right group when triaging connectivity "
        "or mapping mountpoints before drilling into a path with "
        "the 'kv' group, or before drilling into per-backend role "
        "config (read-only) via the 'auth' group."
    ),
    # vmware-rest composites -- mirrors
    # backend/src/meho_backplane/connectors/vmware_rest/composites/_register.py
    # _WHEN_TO_USE_BY_GROUP (PR #732).
    ("vmware", "9.0", "vmware-rest", "cluster"): (
        "Use for cluster-level reads and orchestrated cluster ops "
        "that aggregate across hosts: DRS state + active "
        "recommendations (read), and sequential cluster patch (write, "
        "approval-gated). The right group when the question is "
        "'what is DRS suggesting?' or 'patch every host in this "
        "cluster in order'. Pair with the 'host' group when the "
        "follow-up drills into one host's lifecycle (evacuate, "
        "maintenance), and with 'vm' when DRS recommendations need "
        "to translate into actual VM migrations."
    ),
    ("vmware", "9.0", "vmware-rest", "events"): (
        "Use for vCenter event-stream questions: 'what changed in "
        "the last N events?' tail via EventManager.QueryEvents. "
        "Read-only. The right group for live incident triage when "
        "the operator doesn't yet know which entity to drill into. "
        "Pair with 'vm' or 'host' once the event names a target "
        "moid to inspect."
    ),
    ("vmware", "9.0", "vmware-rest", "performance"): (
        "Use for performance-counter inspection on a single entity "
        "(VM, host, cluster, datastore): discover available counters "
        "via QueryAvailablePerfMetric, sample values via QueryPerf, "
        "return both in one call. Read-only. The right group for "
        "'is this VM hot?' / 'what does the last hour of CPU look "
        "like?' questions. Pair with 'vm' / 'host' to convert "
        "moids the operator already knows into one-shot perf "
        "snapshots."
    ),
    ("vmware", "9.0", "vmware-rest", "storage"): (
        "Use for datastore usage and VM-to-datastore placement: "
        "capacity / free space / type per datastore plus the "
        "vm_count + vm_names enrichment via the placement filter. "
        "Read-only. The right group for 'where is this VM stored?', "
        "'which datastores are running low?', or 'how many VMs live "
        "on this datastore?'. Pair with 'vm' when the question moves "
        "from 'which datastore?' to acting on a specific VM."
    ),
    ("vmware", "9.0", "vmware-rest", "networking"): (
        "Use for distributed-switch and portgroup audits: enumerate "
        "DVS + portgroups, then enrich each portgroup with parent "
        "DVS and connected VM names. Read-only. The right group for "
        "'what's connected to this portgroup?' / 'which DVS does "
        "this VM live on?' questions, and a prerequisite read before "
        "the 'host' group's host_detach_from_vds composite write. "
        "Pair with 'vm' for the post-audit drill-in into one VM's "
        "NICs."
    ),
    ("vmware", "9.0", "vmware-rest", "vm"): (
        "Use for VM-lifecycle write composites: create with NIC "
        "attach + optional power-on (rollback on partial failure), "
        "clone from a content-library template (long-running task "
        "polling), revert to a named snapshot (ambiguity-rejecting), "
        "migrate via DRS or explicit host, bulk power across a "
        "filter. Every op is dangerous / approval-required. The "
        "right group for any operator workflow that would otherwise "
        "be a ``govc vm.*`` invocation orchestrating multiple raw "
        "REST calls. Pair with 'storage' / 'networking' / 'cluster' "
        "for the pre-flight reads that shape the create / migrate "
        "parameters."
    ),
    ("vmware", "9.0", "vmware-rest", "host"): (
        "Use for host-lifecycle write composites: evacuate every "
        "VM off a host (recursive composite call into vm.migrate) "
        "then enter maintenance, or detach a host from a DVS after "
        "migrating its VM NICs off. Dangerous / approval-required; "
        "the host_evacuate composite is the first production "
        "composite that calls another composite. The right group "
        "for 'safely take this host offline' workflows. Pair with "
        "'networking' for the DVS-audit prerequisite to "
        "host_detach_from_vds, and with 'cluster' / 'vm' for the "
        "pre-flight reads."
    ),
    # harbor robot lifecycle -- Signal #5 placeholder curation.
    # PR #732 did not cover Harbor (the connector was new in v0.3.1);
    # this entry is the curated replacement for the
    # ``"TODO: curate (T4b #732)"`` placeholder both
    # ``harbor.robot.create`` and ``harbor.robot.delete`` shipped
    # with. The same string lands in source on
    # backend/src/meho_backplane/connectors/harbor/ops.py in this PR
    # so fresh-DB registrations and backfilled rows carry identical
    # text.
    ("harbor", "2.x", "harbor-rest", "robot"): (
        "Use for project-scoped robot-account lifecycle on Harbor: "
        "mint a new robot credential (``harbor.robot.create`` -- "
        "the response carries a freshly-minted secret returned "
        "ONLY on creation, never again) and decommission an "
        "existing one by numeric id (``harbor.robot.delete``). "
        "Both ops are non-idempotent writes; create is "
        "credential_mint-classified so the minted secret never "
        "appears in the SSE broadcast. The right group when "
        "provisioning a CI/CD push/pull token, rotating an "
        "expired robot, or revoking machine access to a project. "
        "Read-only robot inventory (listing existing robots, "
        "checking expiry, auditing permissions) lives in the "
        "``harbor-robots`` group under the curated read core; "
        "this 'robot' group is the write surface. Pair with the "
        "``harbor-projects`` read group when the agent needs to "
        "confirm the target project exists before minting, and "
        "with ``harbor-robots`` when the post-mint audit step "
        "needs to verify the new robot landed with the expected "
        "permissions."
    ),
}


def upgrade() -> None:
    """Backfill curated ``when_to_use`` onto rows still holding the template.

    For every ``(product, version, impl_id, group_key)`` natural-key
    coordinate in :data:`_CURATED_WHEN_TO_USE`, issue one UPDATE that
    matches **only** rows whose ``when_to_use`` starts with the
    auto-derive prefix and whose ``tenant_id IS NULL`` (built-in /
    global rows; tenant-curated rows are operator-owned and never
    rewritten by a migration). The ``updated_at`` column is bumped so
    operator tooling driven off the column sees the change.

    The UPDATE is built with Core ``sa.table`` / ``sa.column`` shims
    rather than the live ORM model so the migration stays self-
    contained (history-replay against any past schema version must
    keep working without importing the connector packages, which
    didn't exist at revision ``0001``). Same shape migration
    ``0010``'s ``downgrade()`` uses for its row pre-check.

    Issued as one UPDATE per natural key rather than one bulk
    statement with a CASE expression -- 22 rows is below any
    performance threshold worth optimising for, and per-tuple
    statements keep the migration auditable in logs (one log line
    per curated group).
    """
    operation_group = sa.table(
        "operation_group",
        sa.column("tenant_id", sa.Text()),
        sa.column("product", sa.Text()),
        sa.column("version", sa.Text()),
        sa.column("impl_id", sa.Text()),
        sa.column("group_key", sa.Text()),
        sa.column("when_to_use", sa.Text()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    bind = op.get_bind()
    # Single ``now`` per migration run so every backfilled row carries
    # a consistent ``updated_at`` -- mirrors the ORM's default-factory
    # discipline (one ``datetime.now(UTC)`` per write) without
    # importing the model.
    now = datetime.now(UTC)

    for (product, version, impl_id, group_key), curated in _CURATED_WHEN_TO_USE.items():
        stmt = (
            sa.update(operation_group)
            .where(
                operation_group.c.tenant_id.is_(None),
                operation_group.c.product == product,
                operation_group.c.version == version,
                operation_group.c.impl_id == impl_id,
                operation_group.c.group_key == group_key,
                operation_group.c.when_to_use.like(_TEMPLATE_LIKE_PATTERN),
            )
            .values(when_to_use=curated, updated_at=now)
        )
        bind.execute(stmt)


def downgrade() -> None:
    """No-op by design.

    Reconstructing the auto-derive template per row would require
    remembering each row's pre-upgrade value (the template embeds the
    row's own coordinates: ``Operations grouped under '<key>' for
    <product> <impl>.``) -- straightforward arithmetic, but the
    resulting prose carries no operator-visible value, and any
    downstream tooling that read the curated string would see the
    template again on rollback. The narrower "preserve original
    prose" semantic would require a copy column or a second migration
    table; the cost outweighs the upside.

    The schema-structure half of the migration is a no-op (no DDL
    runs in ``upgrade()``), so ``downgrade()`` has nothing to undo at
    the column / index / constraint layer. Leave the rows with their
    curated text and document the asymmetry here -- explicit refusal
    beats silent partial recovery, same shape migration ``0010``'s
    ``downgrade()`` uses for its row pre-check.
    """
    # Intentionally empty -- see docstring. Keeping the function
    # defined (rather than omitting it) is the Alembic convention so
    # ``alembic downgrade -1`` resolves the symbol cleanly and runs
    # the no-op DDL without raising AttributeError.

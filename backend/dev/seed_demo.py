# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Demo-data seeder for the local UI-dev loop.

Fills the dev SQLite DB + dev Redis stream with realistic-looking
governance traffic so the operator-console redesign can be judged
against content instead of empty states:

* 4 ``targets`` rows (vCenter / K8s / Vault / Hetzner Robot),
* ~33 ``graph_node`` + ~44 ``graph_edge`` rows (a small but believable
  DC topology: hosts, VMs, datastores, VLANs, namespaces, pods),
* ~60 ``audit_log`` rows spread over the last 24 h, each mirrored as a
  :class:`BroadcastEvent` on the per-tenant stream — every op_class,
  ok/error/denied statuses, and aggregate-only credential events so the
  lock-marker path renders.

Payload shapes go through the REAL ``classify_op`` + ``redact_payload``
so what the feed shows is byte-for-byte what production would show.

Everything is tagged ``discovered_by="dev-seed"`` (nodes/edges) or uses
the fictional ``*@evba.lab`` / ``agent:*`` principals (audit rows), so
``--reset`` can cleanly remove it all.

Usage (from ``backend/``, env sourced like devserver):

    set -a; source dev/.env.dev; set +a
    uv run python -m dev.seed_demo            # one-shot seed (idempotent)
    uv run python -m dev.seed_demo --reset    # wipe dev-seed data, re-seed
    uv run python -m dev.seed_demo --live     # + publish an event every 3-5 s

Never deployed; lives outside ``src/`` like ``devserver.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from meho_backplane.broadcast.client import dispose_broadcast_client, get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent, classify_op, redact_payload
from meho_backplane.broadcast.publisher import publish_event
from meho_backplane.db.engine import dispose_engine, get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Target

DEV_TENANT_ID = uuid.UUID("71cd1935-1017-4601-9fd2-cd21b83497f1")
SEED_SOURCE = "dev-seed"

# Fictional principals — doubles as the --reset selector for audit rows.
HUMANS = (
    ("amira.ops@evba.lab", "Amira Rahman"),
    ("jonas.sre@evba.lab", "Jonas Keller"),
    ("mila.net@evba.lab", "Mila Petrov"),
)
AGENTS = (
    ("agent:vm-rightsizer", "VM Rightsizer"),
    ("agent:patch-scout", "Patch Scout"),
    ("agent:cert-rotator", "Cert Rotator"),
)
ALL_PRINCIPAL_SUBS = tuple(s for s, _ in HUMANS + AGENTS)

TARGETS = (
    ("vcenter-fra1", "vmware-vcenter", "vcenter.fra1.evba.lab"),
    ("k8s-prod", "kubernetes", "k8s-api.fra1.evba.lab"),
    ("vault-core", "vault", "vault.fra1.evba.lab"),
    ("hrobot-fra", "hetzner-robot", "robot-ws.your-server.de"),
)

HOSTS = ("esx-fra1-01", "esx-fra1-02", "esx-fra1-03", "esx-fra1-04")
VMS = (
    "web-prod-01", "web-prod-02", "api-prod-01", "api-prod-02",
    "db-prod-01", "cache-prod-01", "web-staging-01", "db-staging-01",
    "ci-runner-01", "ci-runner-02", "monitor-01", "backup-gw-01",
)
DATASTORES = ("ds-nvme-01", "ds-nvme-02", "ds-hdd-archive")
NETWORKS = ("vlan-mgmt", "vlan-prod", "vlan-staging")
NAMESPACES = ("meho-system", "ingress", "observability")
PODS = ("meho-backplane-7d9f", "meho-broadcast-2k1x")
SERVICES = ("meho-backplane-svc", "grafana-svc")

# (op_id, method, path, target_name, raw_params)
OP_TEMPLATES: tuple[tuple[str, str, str, str | None, dict[str, object]], ...] = (
    ("vsphere.vm.list", "POST", "/mcp", "vcenter-fra1", {"datacenter": "fra1", "limit": 200}),
    ("vsphere.vm.info", "POST", "/mcp", "vcenter-fra1", {"vm": "web-prod-01"}),
    ("vsphere.datastore.info", "POST", "/mcp", "vcenter-fra1", {"datastore": "ds-nvme-01"}),
    ("vsphere.vm.create", "POST", "/api/v1/operations/call", "vcenter-fra1",
     {"name": "ci-runner-03", "cpu": 4, "memory_gb": 8, "datastore": "ds-nvme-02"}),
    ("vsphere.vm.update", "POST", "/api/v1/operations/call", "vcenter-fra1",
     {"vm": "cache-prod-01", "memory_gb": 16}),
    ("GET:/api/v2.0/systeminfo", "POST", "/mcp", "hrobot-fra", {}),
    ("k8s.deployment.update", "POST", "/mcp", "k8s-prod",
     {"namespace": "meho-system", "deployment": "meho-backplane", "replicas": 3}),
    ("k8s.pod.list", "POST", "/mcp", "k8s-prod", {"namespace": "observability"}),
    ("vault.kv.read", "POST", "/mcp", "vault-core", {"mount": "kv", "path": "meho/db-creds"}),
    ("harbor.robot.create", "POST", "/api/v1/operations/call", None,
     {"project": "meho", "name": "ci-pull"}),
    ("audit.query", "POST", "/mcp", None, {"since": "-24h", "op_class": "write"}),
    ("meho.broadcast.announce", "POST", "/mcp", None,
     {"activity": "draining esx-fra1-02 for firmware update", "scope": "tenant"}),
    ("hetzner.server.reset", "POST", "/api/v1/operations/call", "hrobot-fra",
     {"server_number": 2104923, "type": "hw"}),
)

_RESULT_CHOICES = ("ok",) * 10 + ("error", "error") + ("denied",)


def _build_event(
    rng: random.Random,
    occurred_at: datetime,
    audit_id: uuid.UUID,
) -> tuple[AuditLog, BroadcastEvent]:
    """One coherent (audit row, broadcast event) pair from a template."""
    op_id, method, path, target_name, raw_params = rng.choice(OP_TEMPLATES)
    sub, name = rng.choice(HUMANS + AGENTS)
    result_status = rng.choice(_RESULT_CHOICES)
    op_class = classify_op(op_id)
    status_code = {"ok": 200, "error": 502, "denied": 403}[result_status]
    row = AuditLog(
        id=audit_id,
        occurred_at=occurred_at,
        operator_sub=sub,
        method=method,
        path=path,
        status_code=status_code,
        request_id=uuid.uuid4(),
        duration_ms=Decimal(str(round(rng.uniform(8, 900), 1))),
        payload={"op_id": op_id, "params": raw_params},
        tenant_id=DEV_TENANT_ID,
    )
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=occurred_at,
        tenant_id=DEV_TENANT_ID,
        principal_sub=sub,
        principal_name=name,
        target_name=target_name,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=audit_id,
        payload=redact_payload(op_class, raw_params, result_status),
    )
    return row, event


async def _reset(maker) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            delete(GraphEdge).where(
                GraphEdge.tenant_id == DEV_TENANT_ID,
                GraphEdge.discovered_by == SEED_SOURCE,
            )
        )
        await session.execute(
            delete(GraphNode).where(
                GraphNode.tenant_id == DEV_TENANT_ID,
                GraphNode.discovered_by == SEED_SOURCE,
            )
        )
        await session.execute(
            delete(AuditLog).where(
                AuditLog.tenant_id == DEV_TENANT_ID,
                AuditLog.operator_sub.in_(ALL_PRINCIPAL_SUBS),
            )
        )
        await session.execute(
            delete(Target).where(
                Target.tenant_id == DEV_TENANT_ID,
                Target.name.in_(tuple(n for n, _, _ in TARGETS)),
            )
        )
    client = get_broadcast_client()
    await client.delete(f"meho:feed:{DEV_TENANT_ID}")
    print("[seed] reset complete")


async def _already_seeded(maker) -> bool:
    async with maker() as session:
        row = await session.execute(
            select(GraphNode.id)
            .where(
                GraphNode.tenant_id == DEV_TENANT_ID,
                GraphNode.discovered_by == SEED_SOURCE,
            )
            .limit(1)
        )
        return row.first() is not None


async def _seed_topology(maker, rng: random.Random, now: datetime) -> int:
    """Insert targets + graph nodes + edges; return the node count."""
    node_ids: dict[str, uuid.UUID] = {}

    async with maker() as session, session.begin():
        target_ids: dict[str, uuid.UUID] = {}
        for tname, product, host in TARGETS:
            target = Target(
                id=uuid.uuid4(),
                tenant_id=DEV_TENANT_ID,
                name=tname,
                product=product,
                host=host,
                port=443,
                fqdn=host,
            )
            session.add(target)
            target_ids[tname] = target.id

        def add_node(kind: str, name: str, props: dict[str, object], target: str | None = None) -> None:
            # Explicit id: the uuid4 column default only fires at INSERT,
            # so reading node.id pre-flush would hand the edges None.
            node = GraphNode(
                id=uuid.uuid4(),
                tenant_id=DEV_TENANT_ID,
                kind=kind,
                name=name,
                target_id=target_ids.get(target) if target else None,
                properties=props,
                discovered_by=SEED_SOURCE,
                first_seen=now - timedelta(days=rng.randint(7, 90)),
                last_seen=now - timedelta(minutes=rng.randint(1, 120)),
            )
            session.add(node)
            node_ids[f"{kind}:{name}"] = node.id

        for tname, product, _host in TARGETS:
            add_node("target", tname, {"product": product}, target=tname)
        for h in HOSTS:
            add_node("host", h, {"cpu_cores": 64, "memory_gb": 512, "vendor": "Supermicro"}, "vcenter-fra1")
        for vm in VMS:
            add_node(
                "vm", vm,
                {"cpu": rng.choice((2, 4, 8)), "memory_gb": rng.choice((4, 8, 16, 32)),
                 "power_state": "poweredOn" if rng.random() > 0.1 else "poweredOff"},
                "vcenter-fra1",
            )
        for ds in DATASTORES:
            add_node("datastore", ds, {"capacity_tb": rng.choice((2, 4, 8)), "type": "vmfs"}, "vcenter-fra1")
        for net in NETWORKS:
            add_node("network", net, {"vlan_id": rng.randint(10, 400)}, "vcenter-fra1")
        for ns in NAMESPACES:
            add_node("namespace", ns, {}, "k8s-prod")
        for pod in PODS:
            add_node("pod", pod, {"phase": "Running"}, "k8s-prod")
        for svc in SERVICES:
            add_node("service", svc, {"type": "ClusterIP"}, "k8s-prod")

        await session.flush()

        def add_edge(from_key: str, to_key: str, kind: str) -> None:
            session.add(
                GraphEdge(
                    tenant_id=DEV_TENANT_ID,
                    from_node_id=node_ids[from_key],
                    to_node_id=node_ids[to_key],
                    kind=kind,
                    # CHECK ck_graph_edge_source only allows auto|curated;
                    # discovered_by carries the dev-seed marker instead.
                    source="curated",
                    discovered_by=SEED_SOURCE,
                    first_seen=now - timedelta(days=rng.randint(7, 60)),
                    last_seen=now - timedelta(minutes=rng.randint(1, 120)),
                )
            )

        for i, vm in enumerate(VMS):
            add_edge(f"vm:{vm}", f"host:{HOSTS[i % len(HOSTS)]}", "runs-on")
            add_edge(f"vm:{vm}", f"datastore:{DATASTORES[i % len(DATASTORES)]}", "mounts")
        for vm in ("web-prod-01", "web-prod-02", "api-prod-01", "api-prod-02", "db-prod-01", "cache-prod-01"):
            add_edge(f"vm:{vm}", "network:vlan-prod", "routes-via")
        for vm in ("web-staging-01", "db-staging-01"):
            add_edge(f"vm:{vm}", "network:vlan-staging", "routes-via")
        for h in HOSTS:
            add_edge(f"host:{h}", "target:vcenter-fra1", "belongs-to")
            add_edge(f"host:{h}", "network:vlan-mgmt", "routes-via")
        for ns in NAMESPACES:
            add_edge(f"namespace:{ns}", "target:k8s-prod", "belongs-to")
        for pod in PODS:
            add_edge(f"pod:{pod}", "namespace:meho-system", "belongs-to")
        add_edge("service:meho-backplane-svc", "pod:meho-backplane-7d9f", "depends-on")
        add_edge("service:grafana-svc", "namespace:observability", "belongs-to")
        add_edge("target:k8s-prod", "target:vault-core", "authenticates-via")
        add_edge("vm:db-prod-01", "vm:backup-gw-01", "backed-up-by")

    return len(node_ids)


async def _seed_traffic(maker, rng: random.Random, now: datetime) -> int:
    """Insert audit rows + mirror broadcast events; return the row count.

    Chronological over the last 24 h so the history pane reads
    naturally top-to-bottom.
    """
    offsets = sorted(rng.uniform(0, 24 * 3600) for _ in range(60))
    async with maker() as session, session.begin():
        events: list[BroadcastEvent] = []
        for off in offsets:
            row, event = _build_event(rng, now - timedelta(seconds=24 * 3600 - off), uuid.uuid4())
            session.add(row)
            events.append(event)
    for event in events:
        await publish_event(event)
    return len(offsets)


async def _seed(maker, rng: random.Random) -> None:
    now = datetime.now(UTC)
    node_count = await _seed_topology(maker, rng, now)
    event_count = await _seed_traffic(maker, rng, now)
    print(f"[seed] {len(TARGETS)} targets, {node_count} nodes, ~44 edges, "
          f"{event_count} audit rows + broadcast events")


async def _live(maker, rng: random.Random) -> None:
    print("[seed] live mode: publishing one event every 3-5 s (Ctrl-C to stop)")
    while True:
        audit_id = uuid.uuid4()
        row, event = _build_event(rng, datetime.now(UTC), audit_id)
        async with maker() as session, session.begin():
            session.add(row)
        await publish_event(event)
        print(f"[live] {event.op_id} ({event.op_class}/{event.result_status}) by {event.principal_sub}")
        await asyncio.sleep(rng.uniform(3, 5))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete dev-seed data first, then re-seed")
    parser.add_argument("--live", action="store_true", help="after seeding, publish a synthetic event every 3-5 s")
    args = parser.parse_args()

    maker = get_sessionmaker()
    rng = random.Random(42)
    try:
        if args.reset:
            await _reset(maker)
        if await _already_seeded(maker):
            print("[seed] already seeded (use --reset to re-seed)")
        else:
            await _seed(maker, rng)
        if args.live:
            await _live(maker, random.Random())
    except KeyboardInterrupt:
        pass
    finally:
        await dispose_engine()
        await dispose_broadcast_client()


if __name__ == "__main__":
    asyncio.run(main())

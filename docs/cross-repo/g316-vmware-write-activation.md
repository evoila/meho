# G3.16 — Activate the vSphere L2 primitives the 8 write composites depend on

**Audience:** the RDC operator running a deployed MEHO backplane with a
reachable vCenter 9.0 appliance.
**Goal:** make the 8 `vmware.composite.*` *write* composites dispatchable
by ingesting the vSphere specs, enabling the carrying groups, and
confirming every L2 sub-op the composites declare resolves to an enabled
`endpoint_descriptor` row (so `preflight_l2_dependencies()` stops raising
`CompositeL2DependencyMissing`).

This is the **operational half** of [#1414](https://github.com/evoila/meho/issues/1414).
The **code half** — the proof that the op_ids reconcile — is the test
`backend/tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`
(no live backplane needed; runs in CI). Read the reconciliation summary
in [§"Why this works without a code change"](#why-this-works-without-a-code-change)
before running the live steps.

## Prerequisites

- **Production ingest LLM client is wired.** Landed in
  [#1386](https://github.com/evoila/meho/issues/1386) /
  [#1418](https://github.com/evoila/meho/pull/1418): the chassis installs
  `build_anthropic_ingest_llm_client` at FastAPI lifespan startup, so a
  non-dry-run ingest groups for real when `ANTHROPIC_API_KEY` is set. A
  deploy with **no key** fails closed (HTTP 503 `LlmClientUnavailable`).
  This is why #1414 was blocked on #1386 — the grouping pass 503'd before
  it landed.
- **Role.** The write verbs (`ingest`, `edit-group`, `enable`) require
  `tenant_admin`.
- **The two vSphere specs.** `vcenter.yaml` (REST API) and `vi-json.yaml`
  (Managed Object API). vCenter ships these under
  `info.version="9.0.0.0"`; the catalog labels the line `9.0` and treats
  `9.0` ↔ `9.0.0.0` as a match (see `operations/ingest/catalog.yaml`,
  `spec_info_versions_compatible: ["9.0.x"]`). MEHO does **not**
  redistribute the vendor specs — fetch them from the appliance / VMware
  portal yourself.

## The 8 write composites and their L2 sub-ops

Each composite declares the L2 ops it dispatches into via a `_SUB_OPS_*`
tuple in
[`connectors/vmware_rest/composites/_write.py`](../../backend/src/meho_backplane/connectors/vmware_rest/composites/_write.py).
The union of raw (non-composite) sub-op_ids — the ones that must be
present as enabled descriptor rows — is:

| Composite | Required L2 sub-op_ids |
| --- | --- |
| `vm.create` | `GET:/vcenter/folder`, `POST:/vcenter/vm`, `DELETE:/vcenter/vm/{vm}`, `PATCH:/vcenter/vm/{vm}/network`, `POST:/vcenter/vm/{vm}/power?action=start` |
| `vm.clone` | `GET:/vcenter/vm/{vm}`, `POST:/vcenter/vm-template/library-items?action=deploy`, `GET:/cis/tasks/{task}` |
| `vm.snapshot.revert` | `GET:/vcenter/vm/{vm}/snapshot`, `POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert` |
| `vm.migrate` | `GET:/vcenter/cluster/{cluster}/drs/recommendations`, `POST:/vcenter/vm/{vm}?action=relocate` |
| `vm.power.bulk` | `GET:/vcenter/vm`, `POST:/vcenter/vm/{vm}/power?action=start`, `?action=stop`, `?action=suspend`, `?action=reset` |
| `host.evacuate` | `GET:/vcenter/vm`, `PATCH:/vcenter/host/{host}/maintenance?action=enter` (plus `vmware.composite.vm.migrate`, skipped by pre-flight) |
| `host.detach_from_vds` | `GET:/vcenter/network/distributed-portgroup`, `GET:/vcenter/vm`, `PATCH:/vcenter/vm/{vm}/network`, `POST:/vcenter/network/dvs/{dvs}?action=remove_host` |
| `cluster.patch` | `GET:/vcenter/cluster/{cluster}/host`, `PATCH:/vcenter/host/{host}/maintenance?action=enter`, `?action=exit`, `POST:/vcenter/host/{host}?action=patch` |

22 distinct raw L2 op_ids in total. These span both specs: the
`/vcenter/*` REST paths come from `vcenter.yaml`; `vm.create`'s and the
others' dependencies are all REST (`vcenter.yaml`). `host.evacuate` also
recurses into `vmware.composite.vm.migrate` — a composite-to-composite
edge the pre-flight deliberately skips (its registration is guaranteed by
the same lifespan registrar).

## Why this works without a code change

The reconciliation hinge is the **action-discriminated** ops
(`?action=start`, `?action=enter`, `?action=relocate`, …). vCenter's
OpenAPI spec keys these endpoints with the `?action=<verb>` query suffix
**in the path key itself** — not as a body/query parameter on a shared
base path. The ingest pipeline builds every op_id as
`op_id = f"{method}:{path}"`
([`operations/ingest/openapi.py` `_build_proto`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
and passes the path key through verbatim (no query-string stripping). So
the descriptor row vCenter ingest writes for "power on a VM" is exactly
`POST:/vcenter/vm/{vm}/power?action=start` — byte-for-byte the string the
composite's `_power_vm_op_id("start")` helper builds and that
`preflight_l2_dependencies()` looks up.

The plain paths (`GET:/vcenter/vm`, `POST:/vcenter/vm`, …) reconcile
trivially. **No `_SUB_OPS_*` tuple needs an alias or correction.** The
test linked above proves this against a vCenter-shaped fixture and goes
red if any future edit drifts a sub-op_id away from a form the parser
emits.

## Step-by-step (live backplane)

The general ingest workflow is documented in
[`connector-ingestion.md`](./connector-ingestion.md); this is the
write-composite-activation-specific path.

### 1. Ingest both specs into `vmware-rest-9.0`

The carrying connector spans both specs. Ingest `vcenter.yaml` first
(REST), then `vi-json.yaml` (Managed Object), into the same connector:

```bash
# REST spec — the source of all 22 write-composite L2 sub-ops.
meho connector ingest --catalog vmware/9.0 --spec vcenter.yaml=/path/to/vcenter.yaml

# Managed-Object spec — completes the connector's op surface
# (read composites need it; write composites do not, but ingest both
# so `meho connector enable` cascades a complete connector).
meho connector ingest --catalog vmware/9.0 --spec vi-json.yaml=/path/to/vi-json.yaml --append
```

`--catalog vmware/9.0` resolves the `(product, version, impl_id)` triple
and the `spec_info_versions_compatible` band from
`operations/ingest/catalog.yaml`. The catalog entry is `spec-only`
(MEHO does not host the specs), so the explicit `--spec` path is
required. After ingest the connector lands `review_status='staged'` with
every op `is_enabled=false`.

### 2. Confirm the write-composite L2 ops were ingested

Before enabling, verify the 22 required op_ids are present as descriptor
rows. The fastest check is `meho connector review --json` filtered to the
required set:

```bash
meho connector review vmware-rest-9.0 --json \
  | jq -r '.operations[].op_id' | sort > /tmp/ingested_op_ids.txt

# The 22 required op_ids (from the table above), one per line:
cat > /tmp/required_op_ids.txt <<'EOF'
DELETE:/vcenter/vm/{vm}
GET:/cis/tasks/{task}
GET:/vcenter/cluster/{cluster}/drs/recommendations
GET:/vcenter/cluster/{cluster}/host
GET:/vcenter/folder
GET:/vcenter/network/distributed-portgroup
GET:/vcenter/vm
GET:/vcenter/vm/{vm}
GET:/vcenter/vm/{vm}/snapshot
PATCH:/vcenter/host/{host}/maintenance?action=enter
PATCH:/vcenter/host/{host}/maintenance?action=exit
PATCH:/vcenter/vm/{vm}/network
POST:/vcenter/host/{host}?action=patch
POST:/vcenter/network/dvs/{dvs}?action=remove_host
POST:/vcenter/vm
POST:/vcenter/vm-template/library-items?action=deploy
POST:/vcenter/vm/{vm}/power?action=reset
POST:/vcenter/vm/{vm}/power?action=start
POST:/vcenter/vm/{vm}/power?action=stop
POST:/vcenter/vm/{vm}/power?action=suspend
POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert
POST:/vcenter/vm/{vm}?action=relocate
EOF
sort -o /tmp/required_op_ids.txt /tmp/required_op_ids.txt

# Any line printed here is a required op_id the ingest did NOT produce.
comm -23 /tmp/required_op_ids.txt /tmp/ingested_op_ids.txt
```

Empty output ⇒ all 22 present. Record the command output in the #1414
thread (acceptance criterion 2). If any line prints, the spec rev you
ingested keyed that path differently than expected — capture it as a
follow-up (op_id alias or `_SUB_OPS_*` correction), per acceptance
criterion 3. (None expected against a stock vCenter 9.0 `vcenter.yaml`.)

### 3. Review + polish groups, then enable

Walk Steps 4–6 of [`connector-ingestion.md`](./connector-ingestion.md)
(`meho connector review` / `edit-group` / `edit-op`) to polish the
LLM-proposed groups. Note the write-composite L2 ops are split across
groups by the grouping pass (VM lifecycle, host/maintenance, network) —
enabling the connector enables all of them at once:

```bash
meho connector enable vmware-rest-9.0 --confirm
```

`enable` cascades every group to `review_status='enabled'` and every op
to `is_enabled=true`. After this the 22 L2 sub-ops are enabled descriptor
rows.

### 4. Confirm the carrying groups are enabled

```bash
meho operation groups vmware-rest-9.0          # all groups review_status=enabled
meho connector review vmware-rest-9.0 --json \
  | jq '[.operations[] | select(.is_enabled==false)] | length'   # expect 0
```

### 5. Confirm pre-flight passes

The pre-flight is a per-process cache keyed on the composite op_id; it
walks the DB on first call and short-circuits after. The cleanest live
confirmation is to dispatch the cheapest write composite with a dry/
no-op shape, or simply trust Step 2's reconciliation (the pre-flight
queries the same `endpoint_descriptor.op_id` column Step 2 enumerated).
A missing L2 sub-op would surface as a `composite_l2_missing` structured
error naming the absent op_ids; a clean Step 2 means that error cannot
fire.

## Rollback

`meho connector disable vmware-rest-9.0 --confirm` flips every group to
`review_status='disabled'` and every op to `is_enabled=false`. The write
composites immediately return `composite_l2_missing` again (pre-flight's
negative result is **not** cached, so the next call re-walks and sees the
disabled state). Per-op operator overrides are preserved for a later
re-enable. There is no `delete` verb.

## References

- Reconciliation test (the code-verifiable proof):
  [`backend/tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`](../../backend/tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py).
- Composite sub-op declarations:
  [`connectors/vmware_rest/composites/_write.py`](../../backend/src/meho_backplane/connectors/vmware_rest/composites/_write.py)
  (`_SUB_OPS_*`, `_power_vm_op_id`, `_host_maintenance_op_id`).
- Pre-flight walk:
  [`connectors/vmware_rest/composites/_preflight.py`](../../backend/src/meho_backplane/connectors/vmware_rest/composites/_preflight.py).
- op_id construction: [`operations/ingest/openapi.py`](../../backend/src/meho_backplane/operations/ingest/openapi.py)
  (`_build_proto`, `op_id = f"{method}:{path}"`).
- General ingest workflow: [`connector-ingestion.md`](./connector-ingestion.md).
- Engineering doc: [`docs/codebase/connectors-vmware-rest.md`](../codebase/connectors-vmware-rest.md).
- Out of scope (other G3.16 tasks): composite end-to-end dispatch
  (G3.16-T2), soak / wrapper retirement (G3.16-T3).

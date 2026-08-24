# vmware-rest — create a nested-hypervisor-ready VM (governed recipe)

Verified recipe for building a VM that can itself run ESXi (a nested
hypervisor) through the `vmware-rest-9.0` connector — every mutation on
the governed dispatch path (policy → approval → audit → broadcast), no
govc, no direct vCenter access. Filed as #3087; first consumer is the
governed nested-ESXi substrate build for management-domain bring-up on
nested hosts (the deploy-automation add-on renders the plan, MEHO
executes it).

Verification basis: pinned-spec (`vcenter-9.0/{vcenter.yaml,vi-json.yaml}`
on the operator spec shelf) + dispatcher-path wire pins with mocked
vendor responses. **No step below has been exercised against a live
vCenter yet** — live-appliance verify is the deferred follow-up, and the
one place it matters is called out in gap 4.

Scope split: importing the ESXi installer ISO into the environment
(content-library import-from-URL, `iso.image` mount) is the sibling
recipe (#3086). This recipe starts where an installer ISO is already
addressable as a datastore path (`[datastore1] iso/esxi-9.iso`).

## The recipe

| # | Step | Op (governed id) | Kind |
|---|------|------------------|------|
| 1 | Base VM **+ VHV** | `vmware.composite.vm.create` (`nested_hv: true`, #3093) | composite |
| 2 | VHV, 9.x-only alternative | `POST:/VirtualMachine/{moId}/ReconfigVM_Task` | raw ingested (VI-JSON) |
| 3 | Data disks (× N) | `POST:/vcenter/vm/{vm}/hardware/disk` | raw ingested |
| 4 | Resize later (optional) | `PATCH:/vcenter/vm/{vm}/hardware/cpu` (or `vmware.composite.vm.resize`) | raw ingested / composite |
| 5 | Installer media | `POST:/vcenter/vm/{vm}/hardware/cdrom` | raw ingested |
| 6 | Power on | `vmware.composite.vm.power` (`{"vm": …, "verb": "on"}`) | composite |

Steps 3–5 target the powered-off VM step 1 created; power on last. Step
2 exists only for a VM created *without* `nested_hv` on a 9.x fleet. All
raw bodies are the **flat `/api` `*Spec` shape** — never the legacy
`/rest`-style `{"spec": {...}}` envelope (#2973/#3071 class; the one
`"spec"` key in step 2 is the vim request type's genuine required
parameter, not an envelope).

### 1. Base VM — `vmware.composite.vm.create`

```json
{
  "folder_name": "nested-lab",
  "name": "esx-nested-01",
  "guest_os": "VMKERNEL_8",
  "cpu_count": 8,
  "memory_mib": 16384,
  "nics": [{"network": "<portgroup-moid>", "backing_type": "DISTRIBUTED_PORTGROUP"}],
  "nested_hv": true,
  "power_on_after_create": false
}
```

- **The 8.0.x arm is first-class (#3099).** On a live pre-9.0
  `about.version` major the composite creates through vim
  `Folder.CreateVM_Task` (task-polled, same governed vmomi substrate as
  the VHV leg) instead of REST `POST:/vcenter/vm` — the bare REST create
  is vendor-defective on vCenter 8.0.x (opaque
  `500 UNABLE_TO_ALLOCATE_RESOURCE` for every spec shape and placement,
  proven live; the identical vim create succeeds). On that arm the NICs
  (vmxnet3; DVPG backing resolved to `switchUuid` + `portgroupKey`,
  standard portgroup to `deviceName`) and `nested_hv` fold into the one
  `VirtualMachineConfigSpec`, `resource_pool` **and** `datastore` are
  required (fail-closed `placement_params` — vim has no placement
  defaulting; the datastore name becomes the `files.vmPathName` VM home),
  and `guest_os` maps through a curated spec-grounded guestId table
  (`VMKERNEL_8` → `vmkernel8Guest`; an unmapped enum is a fail-closed
  `guest_id_mapping` refusal — the pre-9.0 arm closes gap 5's silent-typo
  hole). 9.0+ and unresolved versions keep the REST arm below
  byte-identical.
- `guest_os` passes through **verbatim on the REST arm** — no machine
  enum constrains it there (composite schema: any non-empty string;
  pinned spec: prose-only enum). `VMKERNEL_8` / `VMKERNEL_9` are
  documented values in the pinned 9.0 spec, so an ESXi guest is a
  legitimate, spec-grounded identifier (gap 5 on typo behaviour, gap 4
  on casing).
- `nested_hv: true` (#3093) enables nested hardware-assisted
  virtualization as part of the create: after NIC attach and **before any
  power-on**, the composite issues the vim `ReconfigVM_Task`
  (`VirtualMachineConfigSpec.nestedHVEnabled=true`) through its governed
  vmomi substrate — the `/sdk/vim25/{release}` mount that is
  version-correct on vCenter 8.0.x **and** 9.x, unlike the raw step 2 —
  and polls the task to terminal. A failed VHV leg rolls the VM back like
  any other post-create step; the `created` envelope echoes the applied
  `nested_hv` state.
- The composite resolves the folder by display name, attaches NICs, and
  rolls back (`DELETE:/vcenter/vm/{vm}`) on partial failure. **On a
  multi-datacenter vCenter (#3115)** a name matching more than one folder
  (every datacenter ships a default VM folder named `vm`) is re-scoped to
  the placement pins' datacenter; residual ambiguity refuses with the
  candidate moids in `candidate_folders`. An explicit `folder` moid (in
  place of `folder_name`) skips the lookup entirely — the deterministic
  spelling for automation.
- **Placement is first-class on the composite (#3096):** optional
  `resource_pool`, `datastore`, `host` (moids) thread into the CreateSpec
  `placement` alongside the resolved folder moid — the way to pin a
  multi-host cluster with host-local datastores. Folder-only placement
  leaves the pick to vCenter's defaulting, and the pinned spec marks
  `resource_pool` as currently required when neither host nor cluster is
  given, so an underspecified create can fail outright on such
  inventories. Absent pins keep the create body byte-identical to a
  pre-#3096 call. (`cluster` is served by the pinned PlacementSpec but
  not exposed — pin the cluster via its root resource pool.)
- The composite does **not** expose the CreateSpec's `disks`, `cdroms`,
  `boot`, `boot_devices`, or `hardware_version` — vCenter creates a
  single guest-specific blank boot disk by default. When the shape needs
  those knobs at create time (e.g. EFI + CDROM-first boot order in one
  call), drop to the raw `POST:/vcenter/vm` — its pinned CreateSpec
  carries all of them inline, keyed `guest_os` (required, snake_case).

### 2. VHV — raw VI-JSON `ReconfigVM_Task` (9.x-only alternative)

Nested hardware-assisted virtualization (VHV — required for the nested
ESXi to run 64-bit guests) has **no field on the vSphere Automation REST
surface**: the pinned `vcenter.yaml` serves no `hardware_virtualization`
or `nestedHV` spelling anywhere, and `Cpu.UpdateSpec` is exactly
`{count, cores_per_socket, hot_add_enabled, hot_remove_enabled}` (gap 1
— issue #3087's `PATCH …/hardware/cpu` premise does not hold on the
pinned spec).

**The primary governed path is step 1's `vm.create(nested_hv=true)`
(#3093)** — the composite's vim substrate mounts `ReconfigVM_Task` on the
documented release-versioned base, so it works on vCenter 8.0.x *and*
9.x, and the leg is task-polled with the composite's rollback contract.
Use this raw form only when the VM already exists without VHV (e.g. a
pre-#3093 create) **and** the target is a 9.x fleet:

```json
{"moId": "<vm-id>", "body": {"spec": {"nestedHVEnabled": true}}}
```

`VirtualMachineConfigSpec.nestedHVEnabled` is served by the pinned
`vi-json.yaml`, both specs are ingested for this connector (G3.1-T2/T3),
and the same op_id is governance-registered as a composite sub-op
(`vm.create`'s VHV leg and `vm.disk.grow` both gate on it).

- Issue against the **powered-off** VM; the 200 body is a Task MoRef
  (`{"type": "Task", "value": "task-…"}`) — confirm completion via the
  `vmware.tasks.recent` typed read or re-read the VM config.
- **Mount caveat (gap 2):** raw ingested dispatch mounts this op at
  `/api/VirtualMachine/{moId}/ReconfigVM_Task` — the observed 9.x
  accommodation (#2466). The documented release-versioned base
  (`/sdk/vim25/{release}`) is applied only by the typed
  `_post_vmomi_json` helper inside composites, so this raw step is
  **9.x-fleet-only**: on vCenter 8.0.x the `/api`-mounted vmomi form
  404s — on an 8.0.x outer vCenter, `vm.create(nested_hv=true)` is the
  only governed route.

### 3. Data disks — raw disk add, one call per disk

```json
{"vm": "<vm-id>", "body": {"type": "SCSI", "new_vmdk": {"capacity": 137438953472}}}
```

- `capacity` is **bytes** (int64). `new_vmdk` and `backing` are
  exactly-one-of; `scsi`/`ide`/`sata`/`nvme` address specs are optional
  (server picks a free address — the guest-default SCSI adapter from
  step 1 serves the usual nested-lab shapes).
- **Provisioning format is not expressible** (gap 3): `VmdkCreateSpec`
  is `{name, capacity, storage_policy}` — thin vs thick follows the
  target datastore's default unless a `storage_policy` id is passed.
- The 200 body is the new disk id.

### 4. Sizing adjustments (optional)

`PATCH:/vcenter/vm/{vm}/hardware/cpu` with `{"count": 8, "cores_per_socket": 2}`
(flat `Cpu.UpdateSpec`; 204-no-body success, #3082), or the
`vmware.composite.vm.resize` composite for the typed
`requires_power_off` refusal semantics. This step is sizing only — VHV
never rides the CPU PATCH (gap 1).

### 5. Installer media — raw CD-ROM add with ISO backing

```json
{
  "vm": "<vm-id>",
  "body": {
    "type": "SATA",
    "start_connected": true,
    "allow_guest_control": false,
    "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] iso/esxi-9.iso"}
  }
}
```

- The existing `vmware.composite.vm.device.cdrom` composite covers
  **update / remove / disconnect of an existing device only** (its
  sub-op manifest is pinned); attaching a new device is this raw op —
  that division is deliberate, not a missing feature (gap 6).
- `backing.type: ISO_FILE` + `iso_file` datastore path are served by the
  pinned `Cdrom.BackingSpec`; `start_connected: true` makes the ISO
  visible at first power-on.

## Why no new composite

Every step is a **single governed call** — there is no multi-call
choreography whose partial failure needs coordinated rollback (the bar
#3087 sets for adding one). A failed disk/cdrom add leaves an
inspectable powered-off VM; the operator retries or deletes. The one
genuinely composite-shaped step (base VM create with rollback) already
exists. A `vm.nested_prepare` wrapper would add agent-surface weight
without changing governance: each raw op above is individually
policy-gated, approval-routed, and audited. #3093 follows the same
logic from the other side: the VHV flag folded into the *existing*
`vm.create` composite as a param (where the before-power-on ordering
and rollback contract already live) instead of growing a new op.

## Gap list (each pinned by a test)

1. **VHV is not expressible on the REST surface.** No
   `hardware_virtualization` / `nestedHV` anywhere in the pinned
   `vcenter.yaml` (neither `Cpu.UpdateSpec` nor `Vm.CreateSpec.cpu`).
   Governed path = `vm.create(nested_hv=true)` (#3093, the composite's
   vim substrate); raw VI-JSON `ReconfigVM_Task` (step 2) is the
   9.x-only alternative for an already-created VM. When a re-pinned
   spec grows the REST field, the gap lane fails → move the recipe to
   the REST field.
2. **Raw VI-JSON dispatch mounts on `/api`, not `/sdk/vim25/{release}`.**
   Works on the 9.x fleet (observed accommodation, #2466); 404s on
   vCenter 8.0.x. Only typed composite helpers do release-versioned
   VI-JSON mounting — which is why #3093 routes the recipe's VHV flag
   through `vm.create` rather than the raw op. Follow-up candidate if
   8.x nested targets ever need the *raw* vim surface beyond this flag.
3. **Thin provisioning is not expressible on the disk add.**
   `VmdkCreateSpec` = `{name, capacity, storage_policy}`; provisioning
   follows the datastore default / storage policy.
4. **Field-casing divergence on the create/memory bodies.** The
   composite emits the vSphere-6.7-8.x documented `guest_OS` /
   `size_MiB`; the pinned 9.0 spec keys them `guest_os` / `size_mib`
   (snake_case, `guest_os` required). Recorded gap
   (`connectors-vmware-rest.md`, "field-casing gap"), deliberately not
   flipped here: vcsim accepts both spellings, and which form the live
   9.x wire accepts is exactly what the deferred live-appliance verify
   must answer before touching five composite bodies. Until then the
   recipe's step 1 carries this as its one named risk.
5. **No machine-readable guest-OS enum on the REST arm.** The identifier
   is a free string there; a typo (`VMKERNEL8`) parks nothing at dispatch
   and surfaces as a vCenter 400. The pre-9.0 vim arm (#3099) closes this
   for its targets: the curated guestId map refuses an unmapped enum with
   a structured `guest_id_mapping` rollback before any write.
6. **`vm.device.cdrom` has no ADD leg** — by design; the raw op is the
   governed ADD (step 5).
7. **Boot-order / firmware / hardware-version knobs** are not exposed by
   the `vm.create` composite; use the raw `POST:/vcenter/vm` CreateSpec
   (`boot`, `boot_devices`, `hardware_version`) when the ESXi guest
   needs them pinned at create time.

## Where each claim is pinned

| Claim | Test |
|-------|------|
| Recipe REST ops served by pinned spec; flat bodies; VHV/thin gaps; `guest_os` casing + prose enum; ISO backing; `nestedHVEnabled` served | `backend/tests/test_connectors_vmware_rest_nested_esxi_spec_reconcile.py` |
| Wire shape of every raw call (path/verb/mount/body), 204 handling, `/api` vmomi mount caveat, schema validation of the examples, `VMKERNEL_*` pass-through | `backend/tests/test_connectors_vmware_rest_nested_esxi_recipe.py` |
| cdrom composite manifest (update/remove/disconnect only) | `test_cdrom_composite_covers_update_remove_disconnect_only` (reconcile module) |
| `vm.create(nested_hv=true)` leg: reconfigure-before-power-on ordering, body shape, gating, rollback on leg failure, param-absent byte-identity | `test_vm_create_nested_hv_*` in `backend/tests/test_connectors_vmware_rest_composites_write.py`; vim manifest + pinned-spec lane in `..._composites_l2_ingest_reconcile.py` (#3093) |
| `vm.create` placement pins (`resource_pool` / `datastore` / `host`) thread into the create body; pin-absent body byte-identity; pinned `VM.PlacementSpec` field set | `test_vm_create_placement_pins_thread_into_the_create_body` / `test_vm_create_without_placement_pins_create_body_is_byte_identical` in `backend/tests/test_connectors_vmware_rest_composites_write.py`; `test_vm_placement_spec_serves_the_composite_placement_pins` (reconcile module) (#3096) |
| `vm.create` pre-9.0 arm: CreateVM_Task body shape (placement, NIC backings, inline nestedHVEnabled), no REST create attempted, fail-closed guest/placement/backing refusals, task fault/timeout, gate short-circuit, rollback contract; 9.0+/unresolved REST byte-identity | `test_vm_create_pre9_*` + `test_vm_create_v9_and_unresolved_keep_rest_create_byte_identical` in `backend/tests/test_connectors_vmware_rest_composites_write.py`; vim manifest triple + guestId-map spec grounding in `..._composites_l2_ingest_reconcile.py` (#3099) |

## References

- #3087 (this recipe), #3086 (sibling: ISO import + mount), #3093
  (`vm.create` `nested_hv` — the composite VHV leg), #3096 (`vm.create`
  placement pins), #3099 (`vm.create` pre-9.0 vim `CreateVM_Task` arm —
  the 8.0.x REST create is vendor-defective)
- #2973 / #3071 — the `/rest`-vs-`/api` body-envelope class the raw
  bodies were checked against; #2466 — VI-JSON mount bases; #3082 —
  204-no-body writes
- `docs/decisions/spec-reconcile-guards-standard.md` — the lane protocol
- `docs/codebase/connectors-vmware-rest.md` — connector architecture,
  composites, the recorded field-casing gap

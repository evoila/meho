# Connector: vmware-rest (vSphere 8.5+ / 9.0)

## Overview

The `vmware-rest` connector is the hand-rolled `HttpConnector` subclass
that dispatches ingested vCenter REST operations under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple. It pairs with the G0.7 ingestion pipeline's auto-shim (which
makes ~1,275 + ~2,195 `endpoint_descriptor` rows resolvable but not
dispatchable) to deliver real session-authenticated calls against
vSphere 8.5+ / ESXi 8.5+ targets, plus 38 hand-authored composites
that orchestrate cross-spec workflows: 9 read composites
(G3.1-T5 / `#508`; the `host.network_uplinks` / `#2080` and
`host.vsan_health` / `#2135` reads were later re-shipped as typed ops
in `#2258`; plus the four guest-operations reads `#3100`) and 29 write
composites (G3.1-T6 / `#509`, incl. the destructive-tier `vm.destroy` / `#3198`, the
single-VM `vm.power` verb incl. Tools soft shutdown / `#2301`, the
mutating VI-JSON `vm.disk.grow` / `#2893` + the WSFC/FCI shared-attach
`vm.disk.attach` / `#3256`, the folder-template
`vm.clone_from_template` / `#2894`, the vim cluster / inventory writes
`cluster.drs_rule.create` + `folder.create` / `#2895`, the `#2891`
post-clone hardware reconfigure trio `vm.resize` / `vm.nic.repoint` /
`vm.device.cdrom`, the two guest-customization (GOSC) composites
`guest.customization_spec.create` + `vm.customize` / `#2892`, the
OVF/OVA content-library deploy `vm.deploy_from_library` / `#2909`, and
the three host-domain writes `host.datastore_mount_nfs` /
`host.disk_mark_flash` / `host.service_control` / `#3182`, the two vim
distributed-portgroup writes `network.portgroup.create` +
`network.portgroup.security.set` / `#3091`, the content-library import
`vm.import_from_library` / `#3229`, plus the two governed
guest-operations writes `vm.guest.file.write` / `#3100` +
`vm.guest.program.run` / `#3255` (see
`connectors-vmware-rest-guest-ops.md`)). The
write composites cover every state-mutating operator workflow named
in [#214](https://github.com/evoila/meho/issues/214) as required for
govc-wrapper retirement.

Source: `backend/src/meho_backplane/connectors/vmware_rest/`.

## Key types

- **`VmwareRestConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vmware"`, `version="9.0"`,
  `impl_id="vmware-rest"`, `supported_version_range=">=8.5,<10.0"`,
  `priority=1`.
- **Read composites** (`composites/_read.py`) — seven module-level
  `async def` handlers (`cluster_drs_recommendations_composite`,
  `event_tail_composite`, `performance_summary_composite`,
  `datastore_usage_composite`, `network_portgroup_audit_composite`,
  `host_network_uplinks_composite`, `host_vsan_health_composite`).
  Since `#2253` each accepts `(operator, target, params, connector)`
  and issues its 1-3 sub-ops **directly on the resolved connector
  session** — `connector._get_json` / `connector._post_json` mounted
  through `connector.mount_op_path` (`_read_sub_op`) — with **no**
  `dispatch_child`, **no** ingested `endpoint_descriptor` lookup, and
  **no** L2 pre-flight, so the read composites work on a fresh boot
  with zero vCenter-catalog ingest (the missing-L2 dead-end defect
  class, consumer signal 20, is gone for reads). The `connector` kwarg
  is the substrate `#2251` added to the composite handler contract; the
  dispatcher forwards the instance it already resolved for the
  composite's target. The direct path drops two of `dispatch_child`'s
  four `#508` guarantees (bounded recursion, per-sub-op param
  validation) and relocates the other two (audit-tree linkage collapses
  to the top-level composite audit row; per-sub-op policy/broadcast is
  the top-level op's). The two dropped guarantees are acceptable for
  **reads**; the relocated per-sub-op policy gate is load-bearing for
  **writes**, so the write composites (`#2256`) re-apply it per governed
  write sub-call through the reusable
  `operations.composite.enforce_subop_policy` seam (`#2254`) — see the
  write-composites bullet. Registered with `safety_level="safe"` +
  `requires_approval=False` — read-only overrides of
  `register_composite_operation()`'s `dangerous` / `True` defaults.
  `host_network_uplinks_composite` (`#2080`) lists hosts via
  `GET:/vcenter/host`, then per host reads `config.network.pnic` +
  `config.network.proxySwitch` through the vi-json PropertyCollector
  `RetrievePropertiesEx` method — the pnic link-state / uplink mapping
  the plain REST surface cannot reproduce (drives physical
  switch-port-occupancy reasoning); the per-host property read is
  best-effort (a failed read nulls the network detail with a
  `read_note` rather than sinking the composite).
  `host_vsan_health_composite` (`#2135`) queries the vSAN
  health-service vmomi method `VsanQueryVcClusterHealthSummary` on the
  `vsan-cluster-health-system` singleton, scoped to the target
  cluster's MoRef — the `govc vsan.health.*` equivalent, returning the
  cluster-wide `overall_health` colour plus the health-test `groups`
  list. It is likewise best-effort (a failed health-service read nulls
  `groups` / `overall_health` with a `read_note`).
- **Write composites** (`composites/_write.py`) — eighteen module-level
  `async def` handlers (`vm_create_composite`, `vm_clone_composite`,
  `vm_deploy_from_library_composite`, `vm_import_from_library_composite`,
  `vm_snapshot_revert_composite`, `vm_migrate_composite`,
  `vm_power_composite`, `vm_power_bulk_composite`,
  `vm_resize_composite`, `vm_nic_repoint_composite`,
  `vm_device_cdrom_composite`,
  `host_evacuate_composite`, `host_detach_from_vds_composite`,
  `network_portgroup_create_composite`,
  `network_portgroup_security_set_composite`,
  `cluster_patch_composite`, `guest_customization_spec_create_composite`,
  `vm_customize_composite`). The `vm_resize` / `vm_nic_repoint` /
  `vm_device_cdrom` trio (`#2891`) is the post-clone hardware reconfigure
  trio — see the **Hardware write composites** subsection under Control
  flow. Since
  `#2256` each accepts `(operator, target, params, connector)` and
  issues its raw-REST sub-ops **directly on the resolved connector
  session** — `connector._get_json` (`_read_sub_op`) for the resolution
  reads, `connector._post_json` (`_write_sub_op`) for the mutating
  writes, both mounted through `connector.mount_op_path` — with **no**
  ingested `endpoint_descriptor` lookup and **no** L2 pre-flight, so the
  write composites also work on a fresh boot with zero catalog ingest.
  Each orchestrates 2-N sub-ops with documented status enums on the
  response envelope (`{"status": "created" | "rolled_back" | …}`).
  Registered with T4's defaults `safety_level="dangerous"` +
  `requires_approval=True` so the **top-level** composite pops the
  approval queue on every dispatch — that top-level gate stays the single
  primary approval decision.

  **Preserving write governance on the direct path.** Because a direct
  session call bypasses the dispatcher, each mutating sub-call first
  routes through `operations.composite.enforce_subop_policy` (`#2254`)
  before `_post_json` fires: the seam re-runs the same `policy_gate`
  against an in-memory descriptor carrying the sub-op's declared
  governance (`safety_level="dangerous"`, `requires_approval=False`) and
  returns an `awaiting_approval` / `denied` `OperationResult` when the
  gate does not clear. The handler returns that verbatim (the dispatcher
  passes a handler-returned `OperationResult` straight through), so an
  internal write **queues** or is **denied** instead of executing
  un-gated — property 3 of `#508`'s four guarantees preserved. The
  sub-op posture is `requires_approval=False` on purpose: flooring it to
  `True` would double-gate the approval-resume path (the resume re-runs
  the handler with the top-level gate satisfied, but the seam is not
  resume-aware). A human/service operator whose composite was already
  approved therefore auto-executes each write; an agent without a grant
  is denied a `dangerous` sub-op (governance not lowered); an agent with
  a per-`(principal, op, target)` `needs_approval` grant queues it.
  `host_evacuate_composite` is the first production composite that
  dispatches another composite (`vmware.composite.vm.migrate`) via
  `dispatch_child` — that composite→composite recursion is a
  registrar-guaranteed `source_kind="composite"` row (never an ingested
  primitive), so it stays on the `dispatch_child` path per `#2248`; the
  recursion-depth contextvar (default cap 8) handles the depth-1 nesting
  cleanly, and the resolved `vm.migrate` runs its own relocate write on
  the direct session under the same governance seam.
- **Guest customization (GOSC) composites** (`#2892`, group `guest`) —
  `guest_customization_spec_create_composite`
  (`POST:/vcenter/guest/customization-specs`) creates a reusable named
  customization spec from the tractable provisioning subset (hostname as a
  FIXED `HostnameGenerator`, per-NIC static IP / prefix / gateways, global
  DNS, for a Linux `linux_config` or a Windows `windows_config` sysprep);
  `vm_customize_composite`
  (`PUT:/vcenter/vm/{vm}/guest/customization`) resolves a VM by display
  name and applies a saved spec by name, refusing a powered-on VM with a
  structured `precondition_failed` status (vCenter only accepts a pending
  customization on a powered-off VM) and optionally powering it on
  afterward so the customization applies on that boot. GOSC is how a cloned
  VM gets its hostname + network identity on first boot; the create op's
  `spec_name` is what `vm.customize` — and a clone's
  `CloneSpec.guest_customization_spec` (consumed by the Task-D clone op) —
  reference. The body builders map the agent subset onto the vCenter
  `CreateSpec {name, description, spec:CustomizationSpec}` /
  `SetSpec {name}`, sent at the **top level** of the `/api` request body
  (#2973). The `CreateSpec`'s own `spec` field (the `CustomizationSpec`) is
  a legitimate top-level property here — not a `/rest`-style envelope, which
  the modern `/api` surface rejects (see the request-body envelope note
  under Control flow).

  **GOSC secret hygiene (`#1503`) — the load-bearing property.** A
  customization spec can carry Windows admin passwords, sysprep product
  keys, and domain-join credentials
  (`windows_admin_password` / `windows_product_key` /
  `windows_domain_admin_password`). Those values are consumed into the
  sysprep request body — the real API call that provisions the guest — but
  must never serialize onto a reviewer / preview / broadcast / audit
  surface. Three independent surfaces enforce this:
  1. **`proposed_effect` (approval park).** The bespoke park-time preview
     builder (`_write_preview._guest_customization_spec_create_preview`)
     reads ONLY identity keys and echoes
     `{spec_name, os_type, hostname_scheme, nic_count, static_ip_summary}`
     — it never touches the credential params, so no secret can reach the
     durable `ApprovalRequest.proposed_effect` row by construction.
  2. **Broadcast frame.** `guest.customization_spec.create` is pinned into
     `broadcast/events._CREDENTIAL_WRITE_OPS`, so `redact_payload` collapses
     its params to aggregate-only on the feed. The pin is required (not just
     nice-to-have): the `password`-suffixed fields trip the runtime
     key-name scrub, but `product_key` does not (`key` is neither an
     anywhere- nor final-position secret token), and the classifier-coverage
     CI gate (`test_broadcast_classifier_coverage`) fails a secret-bearing
     schema that is not pinned to a credential class.
  3. **Audit row.** The durable `audit_log` payload stores only a
     `params_hash`, never the raw params — safe by construction.

  `vm.customize` carries only a spec-name reference (no secret), so it is
  not pinned; its preview live-reads the VM's power state for the reviewer.
  The end-to-end proof across all three surfaces lives in
  `test_connectors_vmware_rest_composites_write_e2e`
  (`test_gosc_create_secret_hygiene_across_all_surfaces`).
- **Host-domain write composites** (`composites/_host.py`, `#3182`,
  group `host`) — three module-level `async def` handlers
  (`datastore_mount_nfs_composite`, `disk_mark_flash_composite`,
  `service_control_composite`) filling the `#2907` register's
  host-domain coverage gap. All three are vCenter-mediated vim (VI-JSON)
  writes riding the **same** governed `_write._write_vmomi_sub_op` seam
  the `#2893` disk-grow write established (no `pyvmomi`): `datastore_`
  `mount_nfs` issues the synchronous `HostDatastoreSystem.CreateNasDatastore`
  (returns the new Datastore MoRef directly, no poll); `disk_mark_flash`
  fans out `HostStorageSystem.MarkAsSsd_Task` / `MarkAsNonSsd_Task`
  (the HDD direction is the same op keyed on the `mode` param — the real
  vim method is `MarkAsNonSsd_Task`, not the issue's `MarkAsHdd_Task`
  mis-spelling) per `scsiDiskUuid`, task-polled, with a set-shaped
  per-disk `results` array (partial failure tolerated, JSONFlux-reduced
  when large); `service_control` applies a `HostServiceSystem`
  start/stop/restart + optional `UpdateServicePolicy` **bounded to a
  curated server-side allowlist** (`TSM-SSH` / `TSM` / `ntpd` / `ptpd`)
  — an out-of-list service name is refused (`status='service_not_allowed'`)
  before any resolution or write, never passed through. On a **vCenter**
  target the host is selected by display name **or** moref
  (`GET:/vcenter/host` name lookup, moref fallback, ambiguity refused).
  On a **standalone-ESXi** target (`#3332`) — a host no vCenter manages
  yet, distinguished by the probe fingerprint `product=esxi` via
  `host_target.classify_host_target` — there is no `GET:/vcenter/host`
  surface, so the handler resolves the well-known singleton `ha-host`
  MoRef directly through the host vmomi seam (`_post_vmomi_json`, which is
  hand-rolled SOAP over `/sdk` on ESXi) and the `host` param is
  optional / ignored (the host is the target). Obtaining the session on
  such a target is the standalone-ESXi SOAP `SessionManager.Login` branch
  (`#3363`; see the standalone-ESXi session branch under **Per-target
  session**) — `#3332` shipped the host resolution + vmomi seam but not
  the session, so before `#3363` these ops could not authenticate. A
  reachable target that is
  neither vCenter nor ESXi fails closed (`status='unsupported_host_target'`)
  and a vCenter target given no host refuses `host_required`. Because
  these vim methods live on the per-host config sub-managers, not
  `HostSystem`, each handler then reads the needed
  `HostSystem.configManager.{datastoreSystem,storageSystem,serviceSystem}`
  MoRef (one un-gated `RetrievePropertiesEx`, issued the same way for both
  flavors — so a standalone ESXi that cannot answer it fails
  `config_manager_unreadable` exactly like a vCenter host) and mounts the
  method on it. The park-time preview builders take the same
  `classify_host_target` branch, so a preview that passes on an ESXi
  target is not denied at call (the `#3312` parity rule). Registered with
  T4's `dangerous` + `requires_approval=True`.
- **Guest-operations channel** (`#3100`, group `guest_ops`,
  `composites/_guest.py`) — the governed replacement for out-of-band
  `govc guest.run`, reaching *inside* a running VM's guest OS via VMware
  Tools guest operations (vim `GuestOperationsManager`) over the same
  VI-JSON seam. Four `safe` reads —
  `guest_process_list_composite` (`ListProcessesInGuest`),
  `guest_env_read_composite` (`ReadEnvironmentVariableInGuest`),
  `guest_net_show_composite` (Tools-reported `guest.net` / `guest.ipStack`
  via `RetrievePropertiesEx`, needs no guest credential), and
  `guest_file_read_composite` (`InitiateFileTransferFromGuest`, returns the
  transfer handle) — plus two `dangerous` / approval-gated writes,
  `guest_file_write_composite` (`InitiateFileTransferToGuest` + a direct
  PUT of the bytes) and `guest_program_run_composite`
  (`StartProgramInGuest`, with a `ListProcessesInGuest` exit-code poll when
  `wait=true`; #3255). **Guest OS credentials resolve from the target's
  Vault `secret_ref` (`guest_username` / `guest_password`) and never
  travel in params** — a stronger posture than GOSC's credential-class
  params. The sub-manager MoRefs are resolved dynamically off the
  overridable `guestOperationsManager` default (no verified literal is
  hard-coded). `guest.program.run`'s `arguments` / `env` values may carry
  secrets and are kept off the governed surfaces (result / audit-hash /
  preview / gate) and clamped to aggregate-only on broadcast via the
  `_CREDENTIAL_WRITE_OPS` pin; the resume-bound `ApprovalRequest.params` and
  flight-recorder spans still carry them (same characteristic as
  `guest.file.write`'s `content`), so operators must not pass bare secrets
  there. The design fork (vim guest-ops over a
  still-deferred generic-ssh tier for Tools-less guests) and the full
  safety model live in `connectors-vmware-rest-guest-ops.md`; the freeform
  in-guest program-exec tier `StartProgramInGuest` #3100 deferred is now
  lifted as `guest.program.run` (#3255).
- **`register_vmware_composite_operations`** (`composites/_register.py`)
  — async registrar function called from `run_typed_op_registrars` at
  lifespan startup. Iterates a single `_COMPOSITES` tuple of 37
  `_CompositeSpec` rows (9 read + 24 write); each row carries its
  own `safety_level` + `requires_approval` so the policy posture is
  implied by the spec, not by global defaults. Idempotent on re-run
  via the body-hash skip path.
- **Typed ops** (`typed_ops.py`, `#2257`) — the first vmware
  `source_kind="typed"` op, `vmware.host.usage`. Unlike a composite, a
  typed op is a **bound method** on `VmwareRestConnector`
  (`host_usage(self, operator, target, params)`) that the dispatcher
  binds to the resolved connector instance and calls directly — no
  `dispatch_child`, no ingested-descriptor sub-ops, no L2 pre-flight. It
  therefore works on a **fresh boot with zero catalog ingest** — the same
  direct-session property the 5 read composites (`#2253`) and, since
  `#2256`, the 14 write composites now share. The only `dispatch_child`
  leg left on the whole vmware surface is the `host.evacuate` →
  `vm.migrate` composite→composite recursion (a registrar-guaranteed
  `source_kind="composite"` row, not an ingested primitive, `#2248`). The metadata
  lives in a frozen `VmwareTypedOp` dataclass (mirroring
  `argocd/ops.py::ArgoCdOp`); the implementation
  (`host_usage_impl(connector, operator, target, params)`) lists hosts
  via `GET /vcenter/host` then, per host, reads `summary.quickStats`
  (CPU/memory load, MHz/MB), `summary.hardware` (capacity totals:
  `cpu_mhz` per core, core/package/thread counts, `memory_size_bytes`),
  and `runtime.inMaintenanceMode` through a direct PropertyCollector
  `RetrievePropertiesEx` on the connector session. The host listing is
  routed through `mount_op_path` (`/api` modern / `/rest` legacy); the
  vmomi `RetrievePropertiesEx` read is routed through `_post_vmomi_json`,
  which mounts it on the documented VI-JSON base `/sdk/vim25/{release}`
  (see the VI-JSON mount section below) so it resolves on vCenter 8.0.x
  instead of 404ing (`#2466`). The per-host property read is best-effort (a failed
  read nulls the detail with a `read_note`, mirroring
  `host_network_uplinks_composite`); the host listing is load-bearing.
  This op is why the plain REST host summary (liveness only) is not
  enough — `overallCpuUsage` / `overallMemoryUsage` live on the WS-API
  `HostSystem`, not the REST resource. It establishes the vmware typed-op
  pattern future per-host/per-VM typed reads reuse.
- **Incident-survival typed reads** (`#2300`) — three more
  `source_kind="typed"` bound-method reads in sibling modules, each
  registered through `VMWARE_TYPED_OPS` (so no registrar change) and
  working on a fresh boot with zero catalog ingest:
  - **`vmware.vm.info`** (`typed_ops_vm_info.py`, handler `vm_info`) —
    single-VM incident triage. Addressed by `vm` moid or `name`
    (`oneOf` in the schema; a `name` is resolved via
    `GET /vcenter/vm?filter.names=`, and an unknown / ambiguous name
    raises). One PropertyCollector read of the `VirtualMachine`'s
    `runtime.powerState`, `guest.ipAddress` / `guest.hostName` /
    `guest.toolsStatus` / `guest.toolsRunningStatus`,
    `guestHeartbeatStatus`, and `storage.perDatastoreUsage`. Unlike
    `host.usage`'s per-host best-effort leg, this single-object read is
    **load-bearing** (a failure propagates). The "poweredOn but no
    guest IP" hung-appliance shape is representable in one call — the
    plain REST VM detail reports configuration, not these live guest
    signals.
  - **`vmware.object.collect`** (`typed_ops_object_collect.py`, handler
    `object_collect`) — the **bounded generic** escape hatch: read a
    caller-specified property-path list off one `(type, moid)` object.
    Bounded by construction (keeping the `#1177` dumb-substrate line):
    a single `objectSet` entry with **no `TraversalSpec`** (cannot walk
    the inventory), and the size / shape cap lives entirely in
    `parameter_schema` — at most 64 paths, each a dotted vim identifier
    ≤16 segments deep, wildcards / array-indices rejected by pattern.
    An oversized / malformed request fails `validate_params` in the
    dispatcher and returns a structured `invalid_params` result before
    any read is issued. Returns `{properties: {path: val}, missing:
    [path]}`, surfacing the `ObjectContent.missingSet`.
  - **`vmware.tasks.recent`** (`typed_ops_tasks_recent.py`, handler
    `tasks_recent`) — recent vCenter Task objects for change-window
    monitoring (distinct from `event.tail`, which reads the *event*
    log). Two PropertyCollector reads: `TaskManager.recentTask` for the
    Task MoRefs, then `Task.info` on those MoRefs (capped by the
    optional `max_tasks`, default 50, in a single round trip). Each row
    carries operation (`descriptionId`), target entity + type + name,
    state (`queued`/`running`/`success`/`error`), progress, cancelled
    flag, the queue/start/complete timestamps, and the localized error
    message when a task faulted.
- **`vmware.host.storage_devices`** (`typed_ops_host_storage_devices.py`,
  handler `host_storage_devices`, `#3332`) — per-host raw SCSI storage
  devices, the `safe` read that supplies the runtime input
  `vmware.composite.host.disk_mark_flash` needs (the other host storage
  reads — `vsan_health` / `datastore.usage` / `network_uplinks` — do not
  enumerate the raw device set). Resolves the host (a vCenter name/moref
  via `GET:/vcenter/host`, or the standalone-ESXi `ha-host` when the
  probe fingerprint is `product=esxi`, via the shared
  `host_target.classify_host_target` — the same branch the host write
  composites take, so the read works on the **pre-vCenter standalone
  ESXi** the `#3332` bring-up needs), then reads
  `HostSystem.config.storageDevice.scsiLun` (the host-side read mirror of
  `HostStorageSystem.storageDeviceInfo.scsiLun`) plus
  `configManager.bootDeviceSystem` off one PropertyCollector
  `RetrievePropertiesEx`. Each LUN maps to
  `{uuid, canonical_name, device_type, capacity_bytes, ssd, local, model,
  vendor, is_boot}` — `ssd` / `local` / `capacity_bytes` are
  `HostScsiDisk`-only (null on a non-disk LUN). **`is_boot` needs no
  esxcli seam**: when the host exposes a `bootDeviceSystem`, the op calls
  `HostBootDeviceSystem.QueryBootDevices` — a vim query over the *same*
  VI-JSON seam the write composites use — and matches its
  `currentBootDeviceKey` against the rows. That key is **vendor-defined**
  (it *typically* embeds the canonical name / uuid, but the format is not
  contractual, and ESXi frequently leaves `bootDeviceSystem` unpopulated),
  so the match is a **labelled best-effort heuristic** whose outcome the
  top-level `boot_device_resolution` reports: `matched` (a LUN positively
  matched — then, and *only* then, `is_boot` is authoritative: `true` on
  the match, `false` on the rest), `no_match` (the key resolved but matched
  no LUN), or `unavailable` (no config manager, or an unsupported/erroring
  query — an **expected** outcome on many hosts, not an error). On both
  `no_match` and `unavailable` every `is_boot` is `null` — the op **never
  asserts `false` in an ambiguous state**, so a caller must trust `is_boot`
  for boot-exclusion only under `boot_device_resolution == 'matched'` and
  treat `null` as unknown (this is the fail-safe #3332 review B2 hardened).
  The follow-up to verify the key format on real hardware + tighten the
  match is `#3336`. Boot resolution is **fail-safe** (an absent/erroring
  boot query nulls `is_boot` and records `boot_device_note` without sinking
  the listing); the device read itself is **fail-closed**
  (`status='storage_devices_unreadable'` with an empty set on a VI-JSON
  failure). Set-shaped (JSONFlux-reduced when large), with `host_required`
  / `host_not_found` / `ambiguous_host` / `unsupported_host_target`
  covering the resolution refusals.
- **`register_vmware_typed_operations`** (`typed_ops.py`) — async
  registrar wrapper queued onto `run_typed_op_registrars` (via
  `register_typed_op_registrar` in the package `__init__`, alongside the
  composite registrar). Walks `VMWARE_TYPED_OPS`, resolves each op's
  `handler_attr` to the connector bound method, looks the group's
  curated `when_to_use` blurb up in `VMWARE_TYPED_WHEN_TO_USE_BY_GROUP`
  (required for a grouped typed op), and upserts each row into
  `endpoint_descriptor` with `source_kind="typed"` via
  `register_typed_operation`.
- **`VsphereTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the model satisfies the
  Protocol structurally without code edits here.
- **`VsphereSessionLoader`** (`session.py`) — async callable type
  resolving a `(target, operator)` pair to
  `{"username": ..., "password": ...}`. The `operator` parameter
  (threaded down the HTTP auth surface by G3.9-T1) carries the full
  frozen `Operator` so the live loader (G3.9-T3) can read the
  service-account secret under the operator's identity via
  `vault_client_for_operator(operator)`. Injectable on connector
  construction (`VmwareRestConnector(session_loader=…)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the
  default Vault loader.
- **`load_session_credentials_from_vault`** (`session.py`) — default
  loader, stubbed `NotImplementedError` until G3.9-T3 lands the
  operator-context Vault read path. Accepts `(target, operator)` but
  ignores `operator` while stubbed. Mirrors the
  `load_kubeconfig_from_vault` pattern in `connectors/kubernetes/`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage.
2. Importing `meho_backplane.connectors.vmware_rest` triggers the
   module-level `register_connector_v2(product="vmware", version="9.0",
   impl_id="vmware-rest", cls=VmwareRestConnector)` call.
3. The same import triggers the side-effect import of
   `meho_backplane.connectors.vmware_rest.composites`, whose
   `__init__` calls
   `register_typed_op_registrar(register_vmware_composite_operations)`
   to queue the composite-row upsert onto the lifespan's registrar
   list. The package `__init__` then queues
   `register_typed_op_registrar(register_vmware_typed_operations)`
   (`#2257`) for the typed-op rows.
4. The registry's v2 table now resolves `(vmware, 9.0, vmware-rest)`
   to `VmwareRestConnector`. The G0.7 auto-shim's idempotency check
   (in `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.
5. Lifespan calls `run_typed_op_registrars()`, which iterates every
   queued registrar and upserts: the 32 `vmware.composite.*` rows with
   `source_kind="composite"` (9 reads with `safety_level="safe"` +
   `requires_approval=False`; 23 writes with `safety_level="dangerous"`
   + `requires_approval=True`, and the destructive-tier `vm.destroy` with
   `safety_level="destructive"` + `requires_approval=True` / `#3198`),
   plus the `vmware.host.usage` row with
   `source_kind="typed"` (`safety_level="safe"` + `requires_approval=False`).
   The typed row resolves and dispatches with **zero catalog ingest** —
   it depends on no ingested descriptor.

### Per-target session

1. First call to `auth_headers(target, operator)` against a target whose
   name isn't in `self._session_tokens`:
   a. Acquires `self._session_lock` (asyncio.Lock).
   b. Calls `self._session_loader(target, operator)` for the
      service-account credentials (the operator is threaded so the live
      loader can read Vault under the operator's identity; the stub
      ignores it).
   c. POSTs `/api/session` with HTTP basic auth (creds["username"],
      creds["password"]).
   d. Parses the response body: a JSON-quoted string (vSphere 7.0+
      modern shape) or `{"value": "<token>"}` (pre-7.0 legacy shape;
      kept for vcsim cross-version compatibility).
   e. Caches the token under `target.name`.
2. Subsequent calls take the fast path: lock acquisition + cache hit +
   return.
3. The dispatcher's call path
   (`HttpConnector._request_json` / `_post_json`) reads
   `auth_headers()`, gets `{"vmware-api-session-id": "<token>"}`, sends
   it on every dispatched op against this target.

**Standalone-ESXi session branch (`#3363`) — hand-rolled SOAP over `/sdk`.**
A standalone ESXi host — the pre-vCenter case `#3332` exists to support —
does **not** serve the VI-JSON surface and its `/api/*` is a **JSON-RPC
2.0** handler, not the vSphere-Automation vAPI. Two things follow:

- *Disproven premise (kept for history).* `#3363` was first filed for a
  VI-JSON `SessionManager.Login` POST to `/sdk/vim25/{release}/…`. That
  premise is **disproven live against a standalone ESXi 9.1 host in a lab**
  (and against the vendor corpus): the VI-JSON surface `/sdk/vim25/{release}/…`
  is **vCenter-only** — every VI-JSON POST there returns **HTTP 500** with a
  SOAP expat fault because `/sdk` XML-parses the body. And `POST /api/session`
  answers **HTTP 400** ("Unsupported content type") — the JSON-RPC handler,
  never a token — so the modern→legacy 404 fallback dead-ends.
- *Chosen transport.* ESXi serves vmomi only as **hand-rolled SOAP 1.1 over
  `POST /sdk`** (proven live end-to-end: `RetrieveServiceContent` → 200
  `apiType=HostAgent`; `SessionManager.Login` → 200 sets a
  `vmware_soap_session` cookie; `RetrievePropertiesEx` → 200). The codec —
  `defusedxml` parser, per-method string-template envelope builders with a
  local XML escaper, parsers that return the **exact VI-JSON dict shapes**
  the unchanged downstream consumers already read — lives in
  `connectors/vmware_rest/soap.py`; the connector wires it onto the pooled
  `httpx` client (inheriting its TLS-pin → insecure → default precedence and
  `sni_hostname` extension for free — no separate `SSLContext`). **No new
  dependency, no `pyvmomi`, no resolver change, no separate impl class.**

*Establish.* Two ordered SOAP posts on `/sdk` (`_establish_esxi_session`):
(1) unauthenticated `ServiceInstance.RetrieveServiceContent` → the
HostAgent's `propertyCollector` + `sessionManager` MoRefs (which are **not**
the vCenter `propertyCollector` / `ha-sessionmgr` literals — they are read
from ServiceContent, cached in `_esxi_pc_moids` / `_esxi_session_manager_moids`)
and `about` (`version`, `apiType == "HostAgent"`); (2) `SessionManager.Login`
on the ServiceContent SessionManager moid → a 200 sets the
`vmware_soap_session` cookie, which `httpx` keeps in the pooled client's
cookie jar. That cookie is the auth for every subsequent `/sdk` POST, so
`auth_headers()` adds **no header** for an ESXi session (the cached sentinel
token is the opaque cookie value, never the password).

*Branch selection.* By the target's probe fingerprint (`product=esxi`, via
`classify_host_target` — the same `#3332`/`#3312` distinguisher every host
op uses) once probed; and for the **very first probe, before any fingerprint
exists**, by the `POST /api/session` **HTTP 400** signature (status alone is
diagnostic — vCenter answers 401 / a token there, never 400) confirmed by
the JSON-RPC discriminator or the `"Unsupported content type"` body text. A
vCenter target — fingerprint absent / vcenter / unreachable, or a genuine
non-400 / non-JSON-RPC response — never takes this branch, so the vAPI path
above stays **byte-for-byte** unchanged.

*moid remap.* The host reads/writes bake the vCenter `propertyCollector`
literal into their `RetrievePropertiesEx` path; `_post_soap` substitutes the
ServiceContent-provided PC moid **only** when the caller's moid equals that
literal (a guarded substitution — any other PC moid is left untouched).

*vim method map (all on `POST /sdk`, SOAP 1.1, `urn:vim25`).* Eight methods
total — three session/bootstrap + five op methods, each with a builder +
parser in `soap.py`; `_post_vmomi_json`'s esxi guard routes the op methods
through `_post_soap`, whose parsers return the same VI-JSON dict shapes the
unchanged consumers read:

| Consumer | vim method | `_this` | Response shape |
|---|---|---|---|
| establish + fingerprint | `RetrieveServiceContent` | `ServiceInstance` | ServiceContent (PC/SessionManager moids, `about`) |
| establish | `SessionManager.Login` | SessionManager moid | cookie set (success) |
| teardown | `SessionManager.Logout` | SessionManager moid | empty |
| storage_devices, config-mgr reads, task polls | `PropertyCollector.RetrievePropertiesEx` | PC moid **from ServiceContent** | `{objects:[{obj,propSet:[{name,val}]}]}` |
| storage_devices (best-effort) | `HostBootDeviceSystem.QueryBootDevices` | boot-device-system moid | `HostBootDeviceInfo` |
| datastore_mount_nfs | `HostDatastoreSystem.CreateNasDatastore` | `configManager.datastoreSystem` moid | Datastore MoRef (synchronous) |
| disk_mark_flash | `HostStorageSystem.MarkAsSsd_Task` / `MarkAsNonSsd_Task` | `configManager.storageSystem` moid | Task MoRef → poll `Task.info` |

*Native primitive typing (the codec crux).* The deserialiser coerces leaf
primitives by expected type — a **bare** `<ssd>true</ssd>` / `<local>true</local>`
(no `xsi:type`, the real `scsiLun` shape) becomes a Python `bool`, not the
string `"true"` a schema-less walker would emit and the downstream
`_bool_or_none` would silently null (the exact `#3332` corruption class,
moved but not reintroduced — proven `ssd is True` through `_map_scsi_lun`).

*Credential posture.* Credentials never appear in logs, errors, results, or
the flight-recorder vendor-call span (`#3214`): the Login envelope is the
only place the password lives (XML-escaped) and it is never logged nor handed
to the span — `_soap_post` records the `#3214` span with **no request body**,
so the `/sdk` envelope cannot leak into a captured span. A rejected credential
(`InvalidLogin` / `NoPermission` fault) → `ConnectorAuthError` (the SOAP
analogue of a vCenter 401/403, same restage remediation + the dispatcher's
one cold re-login); any other fault → `RuntimeError` naming only the target.

*Readiness.* **State 1 (unit-proven against SOAP envelopes)** until the
documented **State-2** live run against a standalone ESXi 9.1 host in a lab
lands the captured-envelope respx fixtures + the verification note; the
"works on real hardware" claim is gated on that run, not on green units
(mocks are exactly what let the disproven VI-JSON premise ship green).

### fingerprint() / probe()

`fingerprint(target)` runs the #2765 probe chain. `GET /api/about`
first (auth headers injected lazily via the cached session token) —
the richest source, but it is the **ESXi host REST surface**: vCenter's
Automation API has no `/api/about` and answers HTTP 404, which
pre-#2765 left every vCenter target permanently `reachable=False`.

1. `GET /api/about` answers 200 → the payload populates the canonical
   `FingerprintResult` exactly as before:
   - `vendor="vmware"`
   - `product` — via `product_from_line_id(payload.product_line_id)`
     (`vpx` → `vcenter`; `embeddedEsx`/`esx` → `esxi`; fall-through for
     unknown values; `""`/`None` → `"unknown"`)
   - `version`, `build`, `edition` — straight from the payload
   - `extras` — `uuid`, `full_name`, `product_line_id`, `api_type`,
     `os_type`
2. `GET /api/about` answers **404 specifically** (not transport/auth
   failures) → fall back to `GET /sdk/vimServiceVersions.xml`, the
   unauthenticated version-discovery document both vCenter and ESXi
   have served since the SOAP era (`service_versions_api_version`
   parses the `urn:vim25` entry's current version; the request is a
   bare client GET — no auth headers, no session). A parsed vim25 API
   version → `reachable=True`, `version=<api version>`,
   `probe_method="GET /sdk/vimServiceVersions.xml"`, `product` from
   the target's registered product (else `"unknown"` — the document
   alone cannot distinguish vCenter from a pre-REST ESXi).
3. Best-effort enrichment: `GET /api/appliance/system/version`
   (authenticated; the session from step 1's attempt is reused). The
   appliance surface is VCSA-only, so an answer is observed evidence of
   `product="vcenter"` and supplies the authoritative `version` +
   `build`; `probe_method` then names both fallback endpoints. Any
   failure here is swallowed (`_appliance_version` → `None`) — absence
   must not turn a reachable fingerprint red.
4. **Standalone ESXi (`#3363`):** on a JSON-RPC ESXi host `GET /api/about`
   answers **HTTP 400** (empty), not 404, so the step-2 fallback (gated on
   exactly 404) never fires. But establishing the session for that GET
   already recognised the host as ESXi and minted a SOAP session (see the
   standalone-ESXi session branch above); when it did — detected via
   `_session_flavors[...] == "esxi"` — `fingerprint` returns
   `reachable=True`, `product="esxi"`, `version` = the `about.version` the
   unauthenticated `RetrieveServiceContent` already returned (cached in
   `_about_versions`, exact e.g. `9.1.0`), `extras.api_type == "HostAgent"`,
   and `probe_method` names the `GET /api/about` 400 → SOAP
   `RetrieveServiceContent` chain. So a standalone ESXi target now
   fingerprints reachable instead of dead-ending on the vAPI-only
   `/api/about`.

`probe(target)` delegates to `fingerprint()` and folds the boolean
reachable flag into a `ProbeResult`. Failure modes (TCP `ConnectError`,
TLS error, 401 from `/api/session`, 5xx from `/api/about`, 404 on both
`/api/about` and the discovery document) surface as `reachable=False`
with the exception class + message in `extras["error"]` /
`ProbeResult.reason`, and `probe_method` names every endpoint the chain
attempted. The unreachable arm reports the target's **registered**
product (else `"unknown"`) rather than asserting `"vcenter"` — a failed
probe observed nothing (#2765).

### aclose()

1. Snapshot the cached session tokens (+ paths, flavors, SessionManager
   moids, extensions), clear the dicts.
2. For each `(cache_key, token)` pair: a **vCenter** session revokes with
   `DELETE /api/session` (or the legacy `/rest/...` path it was minted at)
   carrying the `vmware-api-session-id` header; an **ESXi**-flavored
   session (`#3363`) has no `DELETE /api/session` on its JSON-RPC vAPI, so
   it revokes with a SOAP `SessionManager.Logout` (`POST /sdk`) on the
   ServiceContent-provided SessionManager moid — cookie-carried, no header
   — instead (`_esxi_logout_quiet`). Either way a failure (5xx, transport
   error, an expired session) is logged via structlog
   `vsphere_session_revoke_failed` / `vsphere_session_revoke_non_2xx`
   but doesn't block shutdown — Kubernetes' 30 s
   `terminationGracePeriod` would otherwise be at risk.
3. Delegate to `super().aclose()` to close the per-target httpx
   clients.

`invalidate_session(target)` — the duck-typed 401-recovery hook — drops
the cached token + flavor + version + SOAP moids so the next dispatch
cold-re-establishes (vCenter re-runs the `/api/session` fallback; ESXi
cold-re-runs the SOAP `RetrieveServiceContent` + `Login`). For an
ESXi-flavored session it additionally issues a best-effort SOAP
`SessionManager.Logout` after dropping the cache entry, so the server-side
session is released too; it is best-effort by design (a stale cookie — the
common 401 case — fails harmlessly), and the vCenter path stays
network-free here (its `DELETE` lives in `aclose`), so vCenter behaviour is
unchanged. An `InvalidLogin` fault surfacing mid-op on `_post_soap` maps to
`ConnectorAuthError`, which is exactly what drives the dispatcher into this
hook + one cold re-login.

### execute()

Legacy shim — synthesises a system-tenant `Operator` and calls
`meho_backplane.operations.dispatch(...)` against the
`connector_id="vmware-rest-9.0"` encoding. Post-G0.6 callers
(`/api/v1/operations/call`, MCP `call_operation`, CLI verbs from #511)
construct a real `Operator` and call `dispatch()` directly; they don't
reach this method.

### Composite dispatch

The 38 composites (9 reads + 29 writes) land as `source_kind="composite"`
rows in `endpoint_descriptor`. At dispatch time:

1. Dispatcher resolves `(vmware-rest-9.0, vmware.composite.<verb>)`
   to the row, sees `source_kind="composite"`, builds a
   `DispatchChild` callable via
   `get_dispatch_child(dispatch=dispatch, parent_operator=...,
   parent_target=..., parent_audit_id=..., parent_op_id=...)`.
2. Handler is resolved via `import_handler(descriptor.handler_ref)`
   to one of the module-level functions in `composites/_read.py` or
   `composites/_write.py`. The dispatcher also resolves the connector
   instance for the composite's target (`#2251`).
3. Dispatcher invokes the handler with the keyword args it declares:
   `connector` (the resolved instance — every read and write handler)
   and/or `dispatch_child` (only `host_evacuate_composite`, for its
   composite→composite recursion).
4. Handler issues its sub-ops **directly on the connector session**
   (`connector._get_json` / `connector._post_json` mounted via
   `connector.mount_op_path`), no `endpoint_descriptor` lookup. Write
   sub-calls pass through `enforce_subop_policy` first (see the
   write-composites bullet above). The lone exception is
   `host.evacuate`'s `await dispatch_child(op_id="vmware.composite.vm.migrate", …)`
   recursion, which re-enters the dispatcher's `source_kind="composite"`
   branch, inherits `parent_audit_id` via the contextvar, and increments
   `composite_depth_var` (bounded at `Settings.composite_max_depth=8`).
5. Handler aggregates the sub-op responses into a single dict and
   returns; the dispatcher wraps it as an `OperationResult` with
   `status="ok"` and `result=<aggregated dict>`. A write handler may
   instead return an `awaiting_approval` / `denied` `OperationResult`
   (from `enforce_subop_policy`); the dispatcher passes that through
   verbatim.

Direct-session sub-calls drop the per-sub-op audit rows (the top-level
composite row is the audit anchor) and the per-sub-op parameter-schema
validation; the per-sub-op policy gate is relocated onto
`enforce_subop_policy` for writes. Only the `host.evacuate` →
`vm.migrate` recursion still rides `dispatch_child` and keeps its
audit-tree linkage + bounded-recursion guard.

### Recursive composite dispatch (`host.evacuate` → `vm.migrate`)

`host_evacuate_composite` is the first production composite that
calls another composite via `dispatch_child`. Two-level nesting:

```text
host.evacuate                                            # depth 0 (top-level dispatch)
  ├─ GET:/vcenter/vm                                     # depth 0 (direct session read)
  └─ vmware.composite.vm.migrate (× N)                  # depth 1 (dispatch_child of a composite)
       ├─ vim RetrievePropertiesEx (drsRecommendation)  # depth 1 (direct vmomi read, #2970)
       └─ POST:/vcenter/vm/{vm}?action=relocate         # depth 1 (direct session write, gated)
  └─ vim POST:/HostSystem/{moId}/EnterMaintenanceMode_Task # depth 0 (gated vmomi write + Task poll, #2970)
```

`composite_depth_var` (default cap 8) handles the one nesting level
naturally. Post-`#2256` the **audit** tree is two-level, not three:
one `host.evacuate` parent row and N `vm.migrate` child rows (the
`dispatch_child` recursion). The raw-REST leaves run directly on the
session and write **no** audit row of their own — the top-level
composite's row is the audit anchor. `composite_depth_var` reads inside
those direct sub-calls ramp 0 (host.evacuate's own reads/writes) → 1
(the vm.migrate frame's reads/writes). The substrate guard's coverage in
`tests/test_operations_composite.py` proves the depth-cap behaviour
holds; this connector's recursive composite is the first production
caller.

### L1/L2 dispatch — direct-session (two-world migration, Goal #2247)

The 38 composites are hand-authored aggregators the connector ships as
`source_kind='composite'` descriptors. Each composite's body issues its
raw-REST sub-ops (`GET:/vcenter/datastore`,
`POST:/vcenter/vm/{vm}/power?action=start`, etc.) **directly on the
connector's own authenticated session** — the connector instance is
injected into the handler (Task #2251), so the sub-calls never resolve an
`endpoint_descriptor` row and work on a fresh boot with zero catalog
ingest (reads migrated in `#2253`, writes in `#2256`).

Because no composite dispatches through an ingested row, the entire old
failure-coping apparatus is gone (Task #2259): the dispatch-time
`preflight_l2_dependencies` pre-flight, the `CompositeL2Dependency*`
exceptions, and the `composite_l2_missing` / `composite_l2_disabled`
structured errors are deleted, not guarded. The `_SUB_OPS_*` tuples in
`_read.py` / `_write.py` are retained purely as the canonical
sub-op-path manifest the spec-reconcile lanes
(`tests/test_connectors_vmware_rest_composites_read_reconcile.py` — the
exhaustive read lane, #2986 —
`tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`,
and `tests/acceptance/test_portgroup_audit_op_id_reconcile.py`) check
against the pinned vCenter specs.

The sole remaining safety net is the platform-wide registration-time
invariant (`operations.composite_invariant`, `#2252`): if any future
code-shipped op declared a `dispatch_child` sub-op that resolved to an
`ingested` row, the boot would fail closed. See
`docs/codebase/composite-ingested-dispatch-invariant.md`.

Composite-to-composite recursion (`vmware.composite.*`, today only
`host.evacuate` -> `vm.migrate`) keeps its `dispatch_child` path: those
sub-ops resolve to registrar-guaranteed `source_kind='composite'` rows,
not ingested primitives, and the invariant skips `*.composite.*` sub-ops
for that reason.

### Request-body envelope: top-level `*Spec`, not `{"spec": {...}}` (#2973)

The write composites build their `/api` request bodies as the vSphere
Automation `*Spec` **at the top level** of the JSON body —
`{"placement": {…}}` for relocate, `{"count": …, "cores_per_socket": …}`
for the CPU PATCH, and so on. `_split_sub_op` substitutes the
`{vm}` / `{nic}` / `{cdrom}` path variables into the URL and posts every
remaining key verbatim as the body, so a handler passes the `*Spec` fields
alongside the path vars (e.g. `{"vm": vm_moid, "placement": {"host": …}}`).

This is a behavioural contract of the modern `/api` surface, not a style
choice. The legacy `/rest` surface wrapped each operation's structured
input under a `{"spec": {...}}` envelope; the `/api` surface flattened it
and **rejects the envelope** with `400 INVALID_ARGUMENT`
(`vapi.invoke.invalid.input`). `vmware.composite.vm.migrate`'s first
real-world dispatch hit exactly this (#2973) — the relocate body still
carried the `/rest` `{"spec": {"placement": …}}` shape — which also wedged
`host.evacuate` (it recurses into `vm.migrate` per VM). The same divergent
envelope was swept off `vm.create`, the NIC create/repoint bodies, the
CPU / memory / CD-ROM PATCH bodies, and the two guest-customization bodies
(`guest.customization_spec.create` / `vm.customize`). The pinned
`vcenter.yaml` declares each of these request bodies as the `*Spec`
directly (`VM.RelocateSpec` / `VM.CreateSpec` / `Cpu.UpdateSpec` / …), with
no `spec` property.

Two `spec` shapes are **kept** and must not be confused with the legacy
envelope:

- The **vmomi (VI-JSON) write bodies** (`ReconfigVM_Task`, `CloneVM_Task`,
  `ReconfigureDvs_Task`, `ReconfigureComputeResource_Task`) dispatch through
  `_write_vmomi_sub_op` onto the `/sdk/vim25` mount, where the vim request
  types genuinely take a `spec` parameter — a real field, not the `/rest`
  envelope.
- The **GOSC `CreateSpec`'s own `spec` field** (the inline
  `CustomizationSpec`): the flattened create body is
  `{name, description, spec: <CustomizationSpec>}` at the top level, so the
  only `spec` key is the legitimate `CreateSpec.spec` property, not an outer
  wrapper.

A body-shape reconcile lane
(`tests/test_connectors_vmware_rest_composites_write_body_reconcile.py`)
grounds the flat-body contract against the pinned spec: for every REST write
sub-op the pinned `vcenter.yaml` serves with a body, the `requestBody` must
be a flat `*Spec`, never a single-`spec` wrapper. It follows the
spec-reconcile standard (skips without the shelf, runs where it is wired).
The byte-for-byte connector-side proof — that the composites emit the flat
bodies with no envelope — lives in
`tests/test_connectors_vmware_rest_composites_write.py`, which runs
everywhere. The path-only reconcile lanes assert *which* endpoints exist;
this pair asserts *what shape* their bodies take.

**Not-yet-swept (adjacent findings).** The same envelope still rides two
non-VM write bodies that the pinned spec likewise declares flat, left for a
follow-up because each carries a wrinkle beyond the mechanical flatten:
`vm.clone`'s library-item deploy (`DeploySpec`) and `cluster.patch`'s vLCM
software apply (`ApplySpec` — an all-optional spec whose empty body raises
the `json=body or None` "send `{}` vs no body" question). The content-library
`find` reads (`FindSpec`, a distinct `_post_json` helper on
`vm.deploy_from_library`'s resolution path) were the third such body; #3071
swept them to the flat `/api` shape (see the OVF/OVA deploy section).
Separately, the create/memory bodies still use vSphere's
documented mixed-case field names (`guest_OS`, `size_MiB`) while the pinned
spec keys them lowercase (`guest_os`, `size_mib`); that field-casing gap is
independent of the envelope and untouched here.

### Write-composite partial-failure conventions

Write composites return a structured `{"status": ...}` envelope so
callers can branch on `status` without parsing free-form prose. The
status alphabets per composite (from each handler's `response_schema`
enum) are:

| Composite | Status values |
| --- | --- |
| `vm.create` | `created`, `rolled_back` (the optional `nested_hv` VHV leg (#3093) is a vim `ReconfigVM_Task` through the governed vmomi seam, task-polled, applied after NIC attach and before any power-on; a leg failure — transport fault, task fault, or poll timeout — rolls back like the other post-create steps. The `created` envelope echoes the applied `nested_hv` state only when the param was supplied, so a param-absent call keeps the pre-#3093 envelope byte-identical. Optional placement pins — `resource_pool` / `datastore` / `host` moids, #3096 — thread into the CreateSpec `placement` alongside the resolved folder moid; absent pins keep the create body byte-identical and never echo into the envelope. **Data disks (#3117):** the optional `disks` param (`[{capacity_gb}]`) lands data disks in the one create — REST arm threads each into the CreateSpec `disks` as a SCSI `new_vmdk` (vCenter fabricates the controller), pre-9.0 vim arm ALWAYS folds a `VirtualLsiLogicSASController` (so a fresh VM is disk-add-ready even with no `disks`, closing the `500 UNABLE_TO_ALLOCATE_RESOURCE` on the documented governed disk-add) plus one `VirtualDisk` (`fileOperation: create`) per entry bound to it; `disk_attach` rides `steps_succeeded` when disks were requested, an invalid `capacity_gb` fails closed with `disk_spec`, and empty `disks` keeps the REST create byte-identical. **Folder resolution (#3115, both arms):** an explicit `folder` moid pin skips the display-name lookup entirely (`folder_lookup` omitted from the ledger; one of `folder`/`folder_name` required via `anyOf`); a `folder_name` matching more than one folder — every datacenter ships a default VM folder named `vm`, so multi-DC collisions are the norm — reverse-maps a placement pin to its datacenter (datacenter listing + one identity∩`datacenters` intersection probe per DC, host → resource-pool → datastore priority) and re-issues the lookup scoped via `filter.datacenters`; residual ambiguity refuses with a `rolled_back` carrying `candidate_folders` instead of silently taking the first row (which created VMs in the wrong datacenter, proven live). Unique-name lookups stay byte-identical (zero extra reads). **Version-conditional create transport (#3099):** on a live pre-9.0 `about.version` major the whole create rides vim `Folder.CreateVM_Task` through the governed vmomi seam, task-polled — bare REST `POST /api/vcenter/vm` is vendor-defective on vCenter 8.0.x (opaque `500 UNABLE_TO_ALLOCATE_RESOURCE` for every spec shape/placement, proven live) — with the always-folded SCSI controller + disks (#3117), NICs (vmxnet3; DVPG backing resolved via portgroup-key + switch-uuid vmomi reads, standard-portgroup backing via the network display name) and `nested_hv` folded into the one ConfigSpec; `resource_pool`/`datastore` are required there (`placement_params` fail-closed), the `guest_os` enum maps through a curated spec-grounded guestId table (`guest_id_mapping` fail-closed), and 9.0+/unresolved keep the REST path byte-identical) |
| `vm.clone` | `completed` (the pinned deploy operation is synchronous — its 200 body is the new VM id, #2970; deploy failures raise `connector_error`) |
| `vm.deploy_from_library` | `deployed`, `deploy_failed`, `deploy_error`, `invalid_reference`, `library_not_found`, `ambiguous_library`, `item_not_found`, `ambiguous_item`, `resolve_error` (OVF/OVA deploy, #2909; the synchronous deploy's 200 body is a `DeploymentResult` — `succeeded=false` → `deploy_failed` with the report's per-issue messages, an HTTP 400/404 for an invalid/missing placement resource → `deploy_error`, and a faulted content-library `find` during name resolution → `resolve_error` (#3071), both with a structured message carrying the vCenter status — so a placement/mapping/resolution error is a structured status, never a raw vendor fault) |
| `vm.import_from_library` | `deployed`, `deploy_failed`, `deploy_error`, plus the same resolution statuses as `vm.deploy_from_library` (typed HttpNfcLease OVF import, #3229; the deploy-envelope family is reused — `CreateImportSpec` descriptor rejection → `deploy_failed`, a vim control-plane / lease / disk-upload fault → `deploy_error` — plus a per-disk `transfer` manifest; see the dedicated section) |
| `vm.snapshot.revert` | `reverted`, `ambiguous`, `not_found`, `timeout` (vim `RevertToSnapshot_Task` polled; a task fault raises `connector_error`, #2970) |
| `vm.migrate` | `migrated`, `no_recommendation` |
| `vm.power` | `ok`, `error`, `tools_unavailable` (single VM; `tools_unavailable` when a soft `guest_shutdown`/`guest_reboot` finds Tools down) |
| `vm.power.bulk` | (per-VM `results` + aggregate `summary` + `aborted_on_failure`) |
| `vm.disk.grow` | `grown`, `invalid_shrink`, `disk_not_found`, `timeout` (grow-only; `invalid_shrink` refuses a request ≤ current capacity before any write; `timeout` when the `ReconfigVM_Task` poll gives up) |
| `vm.disk.attach` | `attached`, `invalid_vmdk_path`, `invalid_unit`, `controller_not_found`, `unit_in_use`, `timeout` (#3256; the first four are pre-write fail-closed refusals; `timeout` when the `ReconfigVM_Task` add poll gives up; a task *fault* raises `connector_error`) |
| `vm.clone_from_template` | `cloned`, `template_not_found`, `ambiguous_template`, `not_a_template`, `timeout` (name-resolution refusals + the template assert are pre-write; `timeout` when the `CloneVM_Task` poll gives up; a task *fault* raises `connector_error`) |
| `host.evacuate` | `evacuated`, `partial`, `aborted` (the maintenance-enter is the vim `EnterMaintenanceMode_Task`, polled; fault/timeout raises `connector_error`, #2970) |
| `host.detach_from_vds` | `detached`, `incomplete`, `timeout` (the detach is the vim `ReconfigureDvs_Task` host-member remove, polled, #2970) |
| `cluster.patch` | `completed`, `stopped` (per-host vim maintenance `*_Task`s + the vLCM `software?action=apply&vmw-task=true` cis task, every task polled before the next step, #2970) |
| `cluster.drs_rule.create` | `created`, `rule_exists`, `insufficient_vms`, `timeout` (idempotent on rule name — a duplicate `rule_exists` is refused before any write; `insufficient_vms` when fewer than two named VMs resolve to the cluster; `timeout` when the `ReconfigureComputeResource_Task` poll gives up) |
| `folder.create` | `created`, `parent_not_found`, `ambiguous_parent` (synchronous `CreateFolder` — the resolution refusals are structured, not raw vim faults) |
| `network.portgroup.create` | `created`, `invalid_vlan_spec`, `timeout` (vim `CreateDVPortgroup_Task` polled, #3091; `invalid_vlan_spec` refuses a trunk+access clash before any write; `timeout` when the poll gives up; a task *fault* — e.g. `DuplicateName` — raises `connector_error`. The `created` envelope carries a read-back `observed` = `{name, vlan}` off the new portgroup's `config`. The trunk / access VLAN specs are `InheritablePolicy` subtypes, so each wire body carries `inherited: false` — without it vCenter defaults `inherited: true` and drops the `vlanId`, silently creating an untagged (VLAN 0) portgroup, #3356) |
| `network.portgroup.security.set` | `updated`, `no_change_requested`, `timeout` (vim `ReconfigureDVPortgroup_Task` polled, #3091; `no_change_requested` refuses when none of the three booleans is supplied, before any read/write; `timeout` when the poll gives up; a task *fault* raises `connector_error`. Carries `previous` (pre-write security triple) + `observed` (post-write triple) read-backs) |
| `vm.resize` | `resized`, `requires_power_off`, `no_change`, `partial` |
| `vm.nic.repoint` | `repointed`, `not_found`, `ambiguous` |
| `vm.device.cdrom` | `removed`, `updated`, `disconnected`, `invalid_request` |

`vm.create` is the only composite that issues a compensating
mutation (`DELETE:/vcenter/vm/{vm}`) on partial failure. The other
write composites prefer "stop and report" semantics over silent
rollback -- the operator decides whether to manually finish or
revert. `vm.resize`'s `partial` follows the same rule: a CPU PATCH
that lands followed by a failing memory PATCH is reported (CPU stays
applied), not rolled back.

### Hardware write composites (`vm.resize` / `vm.nic.repoint` / `vm.device.cdrom`, #2891)

The post-clone reconfigure trio are pure vSphere Automation REST
writes (no `pyvmomi`). A freshly-cloned VM is stuck at the template's
sizing, on the template's portgroup, and with the template's CD-ROM
backing — these three composites rightsize it, move its NIC, and clear
a host-pinning ISO:

- **`vm.resize`** reads current sizing + hot-add flags via
  `GET:/vcenter/vm/{vm}` (one read serves `name`, `power_state`,
  `cpu.{count,cores_per_socket,hot_add_enabled}`,
  `memory.{size_MiB,hot_add_enabled}`), then PATCHes
  `PATCH:/vcenter/vm/{vm}/hardware/cpu` and/or
  `PATCH:/vcenter/vm/{vm}/hardware/memory`. When the VM is powered on
  and the requested change cannot be made live — no hot-add for the
  changed dimension, a decrease, or any `cores_per_socket` change — the
  handler returns `requires_power_off` **before** issuing the PATCH, so
  the operator gets a typed status instead of a raw vCenter 400.
- **`vm.nic.repoint`** reads the NIC's current backing + MAC via
  `GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}`, resolves the target
  distributed portgroup by display name via
  `GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP`, then PATCHes
  `PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}` with
  `{backing: {type: DISTRIBUTED_PORTGROUP, network: <moid>}}`. A name
  that matches zero / many portgroups refuses the repoint
  (`not_found` / `ambiguous`) with no PATCH issued.
- **`vm.device.cdrom`** reads the device's current backing + state via
  `GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}` (surfacing a host-local
  ISO path the approver needs to see), then dispatches the `action`:
  `remove` (`DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}`), `update`
  (`PATCH` the backing — requires a `backing` param, e.g.
  `{type: CLIENT_DEVICE}` to un-pin a host-local ISO), or `disconnect`
  (`POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect`).

All three register a live-read **from->to** preview builder in
`_write_preview.py`, so the parked approval row names the current state
alongside the requested change (the delta is the decision the four-eyes
reviewer signs off). Update PATCH bodies send the `*Spec`'s fields at the
**top level** of the request body — the modern `/api` surface takes the
`Cpu.UpdateSpec` / `Ethernet.UpdateSpec` / `Cdrom.UpdateSpec` directly, not
the legacy `/rest` `{"spec": {...}}` envelope (#2973; see the request-body
envelope note under Control flow).

**Portgroup resolution — the #1602 lesson.** There is no dedicated
`distributed-portgroup` list resource in the REST Automation API;
distributed portgroups are enumerated via the generic
`GET:/vcenter/network` resource filtered to `DISTRIBUTED_PORTGROUP`
(each summary row is `{network (id), name, type}`). The
`_OP_LIST_PORTGROUPS = "GET:/vcenter/network/distributed-portgroup"`
constant `_write.py` carried (declared by `#509`, singular spelling,
absent from the pinned spec) was corrected to `_OP_LIST_NETWORK =
"GET:/vcenter/network"` as part of `#2891` — the same shape
`_read.py`'s `network.portgroup.audit` composite already used. The
`host.detach_from_vds` composite's pre-flight portgroup read moved to
the corrected path too.

### Mutating VI-JSON write substrate (`vm.disk.grow`, #2893)

Some vim operations have **no REST path** — growing a virtual disk is the
first: the pinned 9.0 spec's `Disk.UpdateSpec` carries only `backing` (no
capacity field, spec-verified), so the capacity change is only reachable
through vim `VirtualMachine.ReconfigVM_Task`. This is the connector's first
*mutating* VI-JSON call, and it establishes two reusable seams that Tasks D
(`vm.clone_from_template`, `CloneVM_Task`, #2894) and E (`cluster.drs_rule.create`,
`ReconfigureComputeResource_Task`, #2895) ride. `vm.create`'s optional
`nested_hv` leg (#3093) rides the same two seams:
`VirtualMachineConfigSpec.nestedHVEnabled` has no REST expression (pinned by
the #3087 recipe lanes) and *raw* VI-JSON dispatch mounts on `/api` — a
9.x-fleet accommodation that 404s on vCenter 8.0.x (#2466) — so the
substrate's `/sdk/vim25/{release}` mount is the only cross-version governed
path to the flag. `vm.create`'s pre-9.0 create arm (#3099) rides them too:
`Folder.CreateVM_Task` replaces the vendor-defective 8.0.x REST create,
gated under its canonical vi-json op_id and polled through
`poll_vim_task` like the sibling vim writes.

**1. Governed mutating vmomi sub-op — `_write_vmomi_sub_op` (in `_write.py`).**
A mutating vmomi POST is a *write* sub-op, not a transport detail, so it
flows through the **same** `enforce_subop_policy` gate the REST write
sub-ops do — the VI-JSON counterpart of `_write_sub_op`. It runs the gate
with the vim method's canonical governance op_id (the `METHOD:/path` key
the ingest parser emits from `vi-json.yaml`, e.g.
`POST:/VirtualMachine/{moId}/ReconfigVM_Task`) + the sub-op's full logical
params (so the durable `ApprovalRequest` names the entity the write
touches) under the shared `dangerous` / `requires_approval=False` posture.
On an `awaiting_approval` / `denied` verdict the `_post_vmomi_json` write
is **never issued** — a policy-denied vmomi write never reaches the wire.
On `AUTO_EXECUTE` it POSTs the method through `_post_vmomi_json` (the same
`/sdk/vim25/{release}` mount the reads use) and returns the parsed payload
— for a `*_Task` method, the returned `Task` `ManagedObjectReference`.
`_post_vmomi_json` itself stays a pure transport method (reads use it
un-gated); the governance lives in the composite-layer seam, symmetric
with the REST `_write_sub_op`.

**2. Reusable vim Task-poll helper — `poll_vim_task` (in `vim_task.py`).**
Every `*_Task` method returns a `Task` MoRef rather than the finished
result; the caller must poll `Task.info` to a terminal state before
declaring the write done. `poll_vim_task(connector, target, operator, *,
task, timeout_seconds=600, poll_interval=2)` re-reads `Task.info` (via
`_post_vmomi_json` + the shared `build_task_info_retrieve_params` request
builder generalized from `tasks.recent`) on a fixed cadence until the vim
state is `success` / `error` or the wall-clock deadline elapses, returning
a `VimTaskResult{task, state, result, error_message, progress}`. On a
fault, `error_message` carries the best available description via a
fallback chain (#3116): `error.localizedMessage` (optional on the wire) →
the joined `fault.faultMessage[*].message` texts → the concrete fault's
`_typeName` (e.g. `InvalidArgument`) — so every vim-arm composite's
`rollback_reason` degrades to `<no fault reported>` only when the
`TaskInfo` truly carries no fault content. It does
**not** raise on a task *fault* — the outcome is a legible value each
caller maps to its own status envelope (a clone surfaces `result` as the
new VM MoRef; disk-grow raises on a fault so the dispatcher wraps it
`connector_error`, mirroring `vm.clone`'s task-poll; a timeout returns
`status='timeout'`). A transport `httpx.HTTPError` on the `Task.info` read
*does* propagate — it is a load-bearing failure, not "still running". The
600 s bound mirrors the `vm.clone` `GET:/cis/tasks/{task}` convention.

**3. `_typeName` annotations on every vim request body (#3103).** VI-JSON's
wire format requires the `_typeName` discriminator on **every DataObject**
in a request body — the pinned `vi-json.yaml` derives all data objects from
`Any`, whose `required` list names `_typeName`, and a live vCenter 8.0.3
**rejects** un-annotated bodies outright: a controlled differential against
`POST /sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx`
returned `500 InvalidArgument` (vim fault `Invalid MoRef field: pathSet`)
for the bare body and `200 RetrieveResult` for the same body annotated. The
pre-#3103 substrate tagged only base-typed polymorphic fields (device
backings, `ClusterConfigSpecEx`); the differential disproved that premise —
the 8.0.x deserialiser demands the tag even where declared type == runtime
type. 9.x accepts annotated bodies (it is the spec'd format), so annotation
is **unconditional** — no version gate. Mechanically: the shared
`vim_body.py` module carries `vim_moref()` (the annotated MoRef) and
`retrieve_properties_body()` (the annotated
`PropertyFilterSpec`/`PropertySpec`/`ObjectSpec` trio + `RetrieveOptions` —
the shape every property read and task poll sends); every other body keeps
an **explicit per-builder annotation** at its call site (spec-groundable
and reviewable — deliberately no recursive type-inference magic). Server
responses have always carried `_typeName` tags and the extraction helpers
read fields by name; the unit-test canned
payloads mirror the tagged live shapes to pin that tolerance. The
annotation *vocabulary* is grounded by a reconcile lane in
`tests/test_connectors_vmware_rest_composites_write_body_reconcile.py`:
every `_typeName` literal the substrate emits must name a component schema
in the pinned `vi-json.yaml` (shelf-gated), and the exact annotated
single-prop retrieve body is pinned byte-for-byte in an always-on lane.

**4. Boxed response values un-boxed at every vim property consumer
(#3106).** The response-side twin of #3103: `DynamicProperty.val` (what
every `RetrievePropertiesEx` propSet entry carries) is an **`Any`
placeholder**, and VI-JSON *boxes* what lands in one — a primitive
arrives as `{"_typeName": "string", "_value": "dvportgroup-1766"}`
(live-observed on vCenter 8.0.3; the `PrimitiveBoolean` / `PrimitiveInt`
/ enum-box components of the pinned `vi-json.yaml`, every one keying its
payload `_value`) and an array as `{"_typeName": "ArrayOfString",
"_value": [...]}` (`ArrayOfVirtualDevice`,
`ArrayOfManagedObjectReference`, ... — same `_value` key), while MoRefs
and DataObjects arrive as plain `_typeName`-annotated dicts, no box. The
pre-#3106 extractors read `val` as the bare value, so every
primitive-typed property read failed its type guard against live 8.0.x —
observed as `vm.create`'s DVPG lookup failing `network_lookup` on a 200
that carried both properties (the boxed `key` next to the plain MoRef).
Mechanically: `vim_body.py` carries `unwrap_vim_value()`, a tolerant
recursive un-boxer (strips `_value` boxes and `ArrayOf*` wrappers —
including the SOAP-flavoured element-keyed variant — passes plain values
through unchanged, keeps DataObject `_typeName` tags, and normalises
nested `Any` positions like `TaskInfo.result` in the same pass), and
**every** propSet consumer funnels `val` through it: the `_write.py`
extractors (single-prop, `config.hardware.device`, `config.template`,
`configurationEx.rule`), `_read.py`'s `_extract_object_props`,
`vim_task.py`'s `Task.info` extraction (a boxed `info.state` /
`error.localizedMessage` / `progress` still reaches terminal states), and
the typed reads (`host.usage`, `vm.info`, `host.network_uplinks`,
`object.collect`, `tasks.recent` — whose `recentTask` is a boxed MoRef
array on live 8.0.x). Un-boxing happens at **response consumption only**
— never on payloads that round-trip into request bodies (the resolved
`CustomizationSpec` a clone embeds must keep its wire shape).

**`vm.disk.grow` flow** (the proving op): read the VM's
`config.hardware.device` (a *read* `RetrievePropertiesEx`, un-gated) to
find the full `VirtualDisk` device (the `VirtualDeviceConfigSpec` requires
the edited device "fully specified") + its current `capacityInBytes`;
**refuse a shrink** (`status='invalid_shrink'`) for any request ≤ current,
before any write (vSphere rejects a shrink — fail early and legibly);
issue a single-device `ReconfigVM_Task` edit (`deviceChange=[{operation:
"edit", device: {…VirtualDisk…, capacityInBytes: <raised>}}]`) through
`_write_vmomi_sub_op`; poll the returned Task to terminal via
`poll_vim_task`. The `disk` param is the REST disk id, which is the string
form of the vim `VirtualDevice.key` — so the handler matches the device by
`str(key)` and the REST `GET hardware/disk/{disk}` supplies label +
capacity for the park-time preview. The preview is a live-read from→to
capacity diff (`{vm, name, disk, disk_label, current_capacity_bytes,
requested_capacity_bytes, delta_bytes}`) — the delta is the decision the
approver makes; a failing disk read parks with the #1628
`preview_unavailable` marker (the delta is unknowable).

### Shared disks for WSFC/FCI — bus-sharing, eagerzeroedthick, shared-attach (#3256)

A Windows Server Failover Cluster (WSFC) / SQL FCI needs shared disks that
two nodes open at identical SCSI addresses. Three knobs express the build,
all on the **vim seam**. Two of the three are genuinely REST-inexpressible:
the pinned `Disk.VmdkCreateSpec` carries only `name` / `capacity` /
`storage_policy` (spec-verified — no provisioning field, so `eagerzeroedthick`
cannot be asked for over REST), and there is no per-disk multi-writer field.
The third — controller bus-sharing — *is* REST-expressible in principle
(`Vm.CreateSpec.scsi_adapters[].sharing` accepts `NONE`/`VIRTUAL`/`PHYSICAL`),
but the composite folds a single fixed SCSI controller and exposes no
`scsi_adapters` knob, so it has no bus-sharing spelling on the REST arm
either. Rather than support each knob on a different arm, any non-default
knob routes `vm.create` uniformly through the vim `CreateVM_Task` arm
regardless of vCenter version (`_shared_disk_knobs_requested` generalises the
pre-9.0 `_vim_create_required` gate; that arm requires `resource_pool` +
`datastore` pins), and the shared-attach op is vim-only — both mirroring
`vm.disk.grow`. All knobs at default keep the 9.0+ REST create body
byte-identical.

- **`vm.create` disk/adapter knobs.** Top-level `scsi_bus_sharing`
  (`none`→`noSharing` | `virtual`→`virtualSharing` | `physical`→`physicalSharing`)
  sets the folded `VirtualLsiLogicSASController`'s `sharedBus`; each `disks[]`
  entry's `provisioning` maps to the backing's allocation fields
  (`thin`→`thinProvisioned:true`; `thick`→`thinProvisioned:false,eagerlyScrub:false`;
  `eagerzeroedthick`→`thinProvisioned:false,eagerlyScrub:true`) and `sharing`
  (`multi_writer`→`sharingMultiWriter`) to the backing `sharing`. The knobs
  apply to the **data disks** the create folds; the boot/OS disk is not part
  of that fold, so it never lands on a shared bus.
- **`vm.disk.attach` (the shared-attach leg).** Attaches an **existing** VMDK
  to a VM at an explicit `controller_key` + `unit_number`. A `RetrievePropertiesEx`
  read (un-gated) locates the SCSI controller by key and confirms the unit is
  free; a single `ReconfigVM_Task` carries a `VirtualDeviceConfigSpec` add with
  **no `fileOperation`** — the seam that attaches an existing backing instead of
  creating one (`fileOperation:"create"` would make a new VMDK). Fail-closed
  before any write: `vmdk_path` must match `[datastore] path.vmdk`
  (`invalid_vmdk_path` — no injection reaches the backing `fileName`),
  `unit_number` 0-15 and not the reserved 7 (`invalid_unit`), the controller
  must exist and be SCSI (`controller_not_found`), and the unit must be free
  (`unit_in_use`). Reuses the already-pinned #2893 vim methods
  (`ReconfigVM_Task` + `RetrievePropertiesEx`) — no new reconcile pins.

**Bus-sharing vs multi-writer — pick correctly per workload.** These are two
different vim fields for two different clustering models:

- **SCSI bus-sharing** (`scsi_bus_sharing`, the controller's `sharedBus`) is
  the **WSFC / SQL FCI** mechanism. `physicalSharing` shares the SCSI bus
  across VMs on different hosts and turns on SCSI-3 persistent reservations,
  which the Windows cluster uses to arbitrate exclusive ownership of each
  clustered disk. WSFC disks are **not** multi-writer — only one node owns a
  disk at a time.
- **Multi-writer** (`sharing`, the disk backing's `sharingMultiWriter`) lets
  several VMs open the **same VMDK concurrently** with the vendor lock
  disabled, for **application-managed** clustering where the guest coordinates
  its own locking (e.g. Oracle RAC, or a clustered filesystem). It is a disk
  property, independent of the controller's bus-sharing.

Both require `eagerzeroedthick` disks. For a WSFC/FCI node: create the OS
separately (or clone from a template), give each node a dedicated
`physical`-bus-sharing controller with the EZT shared disks (`vm.create`
`scsi_bus_sharing="physical"` + `provisioning="eagerzeroedthick"` on the first
node), then `vm.disk.attach` the same VMDKs onto the second node at the
identical `controller_key`/`unit_number`. Leave `sharing="none"` for WSFC —
reach for `multi_writer` only when the guest application (not SCSI-3 PR) owns
the locking. (`vm.create` currently folds shared disks at create time; adding
a newly-created EZT shared disk to an *already-provisioned* VM is a follow-up,
not in this task's scope.)

The `vm.disk.attach` park-time preview is a param-echo (`{vm, vmdk_path,
controller_key, unit_number, sharing}`) — the params fully name the blast
radius (which disk attaches to which VM at which address), so no live read is
needed, unlike disk-grow's from→to delta.

### Governed destructive delete (`vm.destroy`, #3198)

`vmware.composite.vm.destroy` is the connector's — and MEHO's — **first
`safety_level="destructive"`** op: the first delete family modeled into the
governed-delete tier (decision
[`governed-delete-operations.md`](../decisions/governed-delete-operations.md)).
Op id: `vmware.composite.vm.destroy` (the reconcile string the teardown
tooling matches; group `vm`, tags `composite / write / vm / lifecycle /
destroy / destructive`). Params: one required `vm` moid — no `force` flag,
because the mandatory human approval *is* the confirmation.

The destructive tier is enforced generically (see
[`approvals.md`](approvals.md#first-governed-delete-vmwarecompositevmdestroy-3198)):
mandatory human approval always (agent verdict `DENY`, no standing grant,
no self-approval even under break-glass), a mandatory preview-hash binding,
and a mandatory blast-radius block. Two things are connector-specific:

- **Fail-closed on a running VM.** The handler live-re-reads `Vm.Info`
  (`GET:/vcenter/vm/{vm}`) at dispatch time (post-approval, so a VM powered
  on between park and approval is still caught) and refuses with
  `status="not_powered_off"` unless `power_state == "POWERED_OFF"`. It
  issues **no implicit power-off** — vSphere faults a destroy on a running
  VM, and powering it down is a separate deliberate decision through
  `vmware.composite.vm.power`.
- **Dual arm** (mirrors `vm.create`'s `_vim_create_required` gate). A
  resolvable pre-9.0 `about.version` routes through the vim
  `VirtualMachine.Destroy_Task` (task-polled via the governed
  `_write_vmomi_sub_op` seam, #2893 substrate); 9.0+ (and an unresolved
  version) issues the synchronous REST `DELETE:/vcenter/vm/{vm}`. The vim
  arm's `Destroy_Task` + the preview's snapshot `RetrievePropertiesEx` are
  declared in `_write._VIM_SUB_OPS_VM_DESTROY` and reconciled against the
  pinned `vi-json.yaml` (the same lane as disk-grow).

The blast-radius preview builder (`_write_preview._vm_destroy_preview`)
live-reads `Vm.Info` for object identity (moid / name / power state) plus
the enumerated disks (with capacities) and NICs, and best-effort enumerates
snapshots via the vim snapshot read (`_read_vm_snapshots_best_effort` — a
fault yields "no snapshots enumerated", never sinks the park). It declines
(`None` → the park is refused `blast_radius_required`, fail-closed) when the
VM cannot be read.

### Two clone ops: content-library vs folder-template (`vm.clone_from_template`, #2894)

The connector ships **two** clone composites, and they deploy from two
different kinds of source — an operator picks by where the golden image
lives:

| Op | Source | Path | When |
| --- | --- | --- | --- |
| `vm.clone` | a **content-library** template item | REST `POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy` (synchronous — the 200 body is the new VM id; #2970) | the golden image is published to a content library |
| `vm.clone_from_template` | a **folder VM template** (a marked-as-template VM in a VM folder) | vim `POST:/VirtualMachine/{moId}/CloneVM_Task`, poll via `poll_vim_task` | the golden image is a plain marked-as-template VM (govc/terraform's `CloneVM_Task` path) |

A third content-library deploy path, `vm.deploy_from_library` (#2909,
retiring `govc library.deploy`), covers a different **item type**: an
**OVF/OVA** library item (content-library item `type=ovf`) rather than a
VM-template item. It deploys via the synchronous REST
`POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy` whose
200 body is a `DeploymentResult` (not a bare VM id), so it can surface
OVF-descriptor / network-mapping / placement validation failures as a
structured `deploy_failed` / `deploy_error` status. It also resolves the
item by name (`POST:/content/library/item?action=find`, filtered to
`type=ovf`, optionally scoped by a library name resolved via
`POST:/content/library?action=find`), refusing ambiguity before any
deploy. See the OVF/OVA deploy section below.

`vm.clone`'s content-library deploy path **cannot** clone a folder
template — a marked-as-template VM has no content-library item to deploy
— and the REST `POST:/vcenter/vm?action=clone` template-source acceptance
is undocumented (MEDIUM confidence). The vim `VirtualMachine.CloneVM_Task`
path is unambiguous (it is what govc/terraform use for a folder-template
deploy) and it **uniquely** supports inline guest customization at clone
time. `vm.clone_from_template` (#2894) rides the #2893 substrate: the
mutating `CloneVM_Task` flows through the governed vmomi write seam
(`_write_vmomi_sub_op` → the #2254 gate) and the returned `*_Task` MoRef
is driven to a terminal state via `poll_vim_task`.

**Flow** (spec-verified against the pinned `vi-json.yaml`): resolve
`source_template` — a display **name** — to a unique VM moid via
`GET:/vcenter/vm?filter.names=` (refuse `template_not_found` /
`ambiguous_template`); assert `config.template` is true via a vmomi
`RetrievePropertiesEx` *read* (refuse `not_a_template` before any clone —
a regular VM named by mistake never gets cloned); when
`customization_spec_name` is set, resolve the stored GOSC spec to its full
`CustomizationSpec` via `CustomizationSpecManager.GetCustomizationSpec`
(a *read*) and embed it inline in `CloneSpec.customization`; build the
`CloneVMRequestType` body `{folder: Folder-MoRef, name,
spec: CloneSpec{location: RelocateSpec(pool + datastore, optional host),
template: false, powerOn, customization?}}`; issue `CloneVM_Task` through
the governed seam; poll the returned Task — success reports `cloned` with
the new VM moid from `TaskInfo.result`, a fault raises (dispatcher wraps
`connector_error`), a poll timeout returns `timeout` with the task id.

**Composes with the GOSC keystone (#2892).** vim `CloneSpec.customization`
takes a full `CustomizationSpec` inline, not a by-name reference, so a
stored GOSC spec (e.g. one created by `guest.customization_spec.create`)
is resolved to its object form and embedded — the clone yields a
customized VM in **one** dispatch, no separate `vm.customize` call. The
`customization_spec_manager_moid` param defaults to the standard
`ServiceContent.customizationSpecManager` singleton moid and is
overridable (mirrors the performance composite's `perf_manager_moid`).

The preview is a **param echo** (no I/O — the params name the full blast
radius): `{source_template, new_vm_name, folder, resource_pool,
datastore, host, power_on, customization_spec_name}`. Only the
customization spec **name** is echoed — never the resolved spec's
secret-bearing sysprep/password contents (#1503); the identity-only gate
params carry the same, so the durable `ApprovalRequest` names the blast
radius without leaking credential material. New vim sub-op paths
(`CloneVM_Task`, `RetrievePropertiesEx`, `GetCustomizationSpec`) are
declared in `_VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE` and reconciled against
the pinned `vi-json.yaml`.

### OVF/OVA content-library deploy (`vm.deploy_from_library`, #2909)

Retires `govc library.deploy` for OVF/OVA appliances (the HoloRouter OVA
and friends). Pure vSphere Automation REST — no `pyvmomi`. All four
sub-ops are `vcenter.yaml`-served paths, so the composite reconciles
through the generic `_SUB_OPS_VM_DEPLOY_FROM_LIBRARY` sweep (not a
`_VIM_SUB_OPS_*` lane).

**Item resolution.** `library_item` (an id) is a passthrough. Otherwise
`library_item_name` is resolved to an id via
`POST:/content/library/item?action=find` (a *read* — returns a bare id
array — issued un-gated through the `_find_content_library_ids` helper,
which rides `_post_json` since find is POST-shaped). The `FindSpec` is sent
at the **top level** of the `/api` body (`{name, type, library_id}`), not
the legacy `/rest` `{"spec": {...}}` envelope vCenter 8.x `400`s (#3071); the
item find is filtered to `type=ovf` so a colliding non-OVF item name never
matches. An optional `library_name` first resolves to a library id via
`POST:/content/library?action=find` to scope the item lookup. Zero
matches → `item_not_found` / `library_not_found`; more than one →
`ambiguous_item` / `ambiguous_library` with the candidate ids — every
refusal is **pre-deploy**, so the operator re-dispatches by explicit id. A
find call that itself faults (HTTP 4xx/5xx) is caught and mapped to
`resolve_error` with the vCenter status + message, never a raw
`connector_error: HTTPStatusError` (#3071).

**Deploy.** The body is `{deployment_spec, target}` — two top-level named
params, the flat `/api` shape. This deploy op takes two parameters, so its
body never carried the single-`spec` envelope the single-parameter `find` /
`CreateSpec` bodies did before #2973/#3071 flattened them.
`target.resource_pool_id` is the one **required** placement
(`host_id` / `folder_id` refine it); `deployment_spec` carries
`accept_all_eula` (defaults true), `name`, the OVF-network-key →
portgroup-moid **map** `network_mappings` (a map in the pinned 9.0 spec,
not an array), `storage_provisioning` / `storage_profile_id` /
`default_datastore_id`, and any `ovf_properties` folded into a single
`PropertyParams` entry in `additional_parameters`. The EULA-accept wire
key is **version-aware** (#3074): the pinned 9.0 spec keys it
`accept_all_eula` (lowercase), but vCenter 8.0.x — and every pre-9.0
release — rejects that key (`HTTP 400 UNEXPECTED_INPUT`) and expects the
legacy Automation name `accept_all_EULA` (`govmomi`'s
`DeploymentSpec.AcceptAllEULA`; a live `govc library.deploy` onto 8.0.3
succeeded with it). `_deploy_eula_field_name` gates off the live
`about.version` major component — pre-9.0 targets get the caps form, 9.0+
(and an unresolved version, which falls back to the pinned-spec form)
keep the lowercase form — so a single field-level conditional in the
composite covers the divergence without a separate `vmware-rest-8.0`
connector.

**Result mapping — structured statuses, never a raw vendor error.** The
deploy is synchronous, but unlike `vm.clone` its 200 body is a
`DeploymentResult` structure. `succeeded=true` → `deployed` with the VM
moid from `resource_id.id` (+ `resource_type`); `succeeded=false` →
`deploy_failed` with the report's per-issue messages
(`error.errors/warnings/information`, each projected to
`{category, severity, message}`) — this is how a bad network-mapping key
or OVF-descriptor validation surfaces. An HTTP 400/404 (invalid args, or
a placement moid that does not exist) is **caught** and mapped to
`deploy_error` with a parsed message rather than re-raised — so a
placement/mapping error is always a structured status. With `power_on`,
a best-effort `POST:/vcenter/vm/{vm}/power?action=start` follows a
successful deploy; a power-on fault leaves `status='deployed'` with
`powered_on=false` and a `power_on` warning issue (a deployed appliance
is never rolled back over a power-on hiccup).

**Synchronous-deploy long timeout (#3076).** Both library-item deploys —
the OVF `POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy`
and `vm.clone`'s VMTX
`POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy`
— are **synchronous** with no `vmw-task=true` variant to poll (#2970): the
POST connection is held open for the entire multi-GB disk copy. The
connector's pooled client bakes a 30s read/write default
(`httpx.Timeout(read=30.0, write=30.0)` in `HttpConnector._http_client`),
which any real appliance copy blows past — so the POST used to raise
`httpx.ReadTimeout` at ~30s and surface a **false** `deploy_error`
(`vm.clone`: a raw `connector_error`) while vCenter finished the copy
server-side. The two deploy sub-ops now pass a per-request `timeout`
override, `_LIBRARY_ITEM_DEPLOY_TIMEOUT`
(`connect=5 / read=10800 / write=10800 / pool=5` — a 3-hour read/write
ceiling, widened from the original 30 min after a 4.7 GB installer OVA to an
NFS datastore on the Hetzner fabric outran that window live, #3176), threaded
`composite → _write_sub_op → HttpConnector._post_json →
client.request(timeout=)`. The override defaults to
`httpx.USE_CLIENT_DEFAULT`, so **every other call keeps the unchanged 30s
client default** — the global client timeout is deliberately *not* raised,
so ordinary reads/gets still fail fast on a dead target; connect/pool stay
fast on the deploy too, so only the body transfer gets the long window.
When a deploy genuinely faults at the transport layer, the structured
detail names the exception *type* — `transport fault (ReadTimeout)` — since
`str(httpx.ReadTimeout())` is the empty string (the `_vsphere_fault_detail`
helper, #3076).

**Async dispatch is the supported mode for multi-GB deploys (#3176 / #3079).**
Widening the read ceiling is a *bounded* mitigation: a still-larger item or a
slower fabric can outrun any fixed window, and on the synchronous path a caller
that disconnects mid-transfer aborts the deploy session — vCenter rolls the
half-created VM back (the second live failure mode on the 4.7 GB installer
OVA). Both are cured by dispatching the deploy in **async governed mode**
(`POST /api/v1/operations/call` with `async: true`, or the `call_operation`
meta-tool's async flag; #3079 / PR #3201): the dispatch runs on a durable
background task that owns the vCenter POST, the route returns `202` + a run
handle (`GET /api/v1/operations/runs/{handle}` polls it, and the completed
`DeploymentResult` envelope is persisted on the run row), and because that task
is spawned via `asyncio.create_task` — independent of the request handler
task — a dropped caller no longer propagates cancellation into the in-flight
copy. The synchronous path stays the default for small items but is bounded by
the ceiling above; an item large enough to outrun even 3 h uses the durable
typed `HttpNfcLease` import `vm.import_from_library` (#3229; shipped, see the
next section) — MEHO drives the disk transfer itself, so completion is bounded
only by the transfer's own duration and there is no single blocking read to
outrun at all.

**204 / empty-body write acks (#3082).** Several vCenter write endpoints
acknowledge success with **204 No Content** and no body — the power actions
(`POST:/vcenter/vm/{vm}/power?action=<verb>`), the Tools-mediated guest
power actions (`POST:/vcenter/vm/{vm}/guest/power?action=<verb>`),
`DELETE:/vcenter/vm/{vm}` (the `vm.delete` write and the `vm.create`
rollback), and the session-revoke `DELETE`. The shared transport helpers
(`HttpConnector._post_json` / `_request_json`) used to end in an
unconditional `resp.json()`, so a 204 raised `json.JSONDecodeError` —
wrapped by the dispatcher as `connector_error` — **after the write had
already taken effect at the vendor** (observed live:
`vmware.composite.vm.power` powered the VM on, then reported
`connector_error: JSONDecodeError`; a false error on a succeeded write
makes envelope-driven automation retry an executed operation). The guard
lives at the adapter (`json_payload_or_empty` in
`connectors/adapters/http.py`): a `204` — or any empty body — returns a
benign `{}`, which the composites' success arms already accept
(`vm_power_composite` / `vm_power_bulk_composite` / the delete legs discard
the payload and build their own envelopes); a non-empty body parses exactly
as before, so 200-with-JSON payload flows (`create` vm-id unwrap, deploy
payloads) are byte-identical. Every `_write_sub_op` call site inherits the
guard because `vmware_rest` does not override the transport helpers; the
`vcf_automation` connector *does* override them and applies the same guard
for contract parity.

### Typed HttpNfcLease OVF import (`vm.import_from_library`, #3229)

The durable, transfer-window-decoupled sibling of `vm.deploy_from_library`.
The deploy composite's REST deploy is *synchronous* — one POST is held open
for the whole server-side copy, so completion is bounded by the client
read-timeout, not the operation's real duration; a 4.7 GB installer OVA to an
NFS datastore outran even the 3 h mitigation ceiling live (#3176). Rather than
widen a fixed window further, `vm.import_from_library` **drives the disk
transfer itself** over a typed vim `HttpNfcLease` import, so completion is
bounded only by the transfer's own duration and there is no single blocking
read to outrun. Per postulate 1 a typed path is the sanctioned durable fix
when the REST spec is inadequate — the pinned `vcenter.yaml` serves no `$Task`
variant of the library-item deploy (#2970), and the only async REST OVF deploy
(`Vcenter.Ovfs_deploy$Task`) is URL-sourced + 9.0-only.

**Mechanism** (vim25 over the VI-JSON `_post_vmomi_json` seam / #2466, split
across `ovf_import.py` / `ovf_import_control.py` / `ovf_transfer.py`, with the
content-library byte source in `library_download.py`):

1. `ServiceInstance.RetrieveServiceContent` → the `OvfManager` + `rootFolder`
   MoRefs (resolved off ServiceContent, never a guessed singleton moid).
2. `OvfManager.CreateImportSpec` (a read: `System.View`) validates the OVF
   descriptor against the target and returns an `importSpec` + the `fileItem`
   disk list. A descriptor error short-circuits to `deploy_failed` before any
   mutation.
3. `ResourcePool.ImportVApp` — the one governed *write* (gated through the same
   `enforce_subop_policy` seam as every composite write) — creates the
   inventory objects and returns an `HttpNfcLease`. The `importSpec` is
   round-tripped **verbatim** into `ImportVApp` (never funnelled through
   `unwrap_vim_value`, which would strip the `_typeName` tags an `Any` request
   field needs — the response-consumption-only contract of that helper).
4. Poll `HttpNfcLease.state` to `ready`; `ready` carries the per-device upload
   URLs in `HttpNfcLease.info.deviceUrl`.
5. Stream each disk from the content library (client-side download-session:
   create → prepare → poll `PREPARED` → GET the `download_endpoint.uri`)
   straight to its device URL, matched by `deviceUrl.importKey ==
   fileItem.deviceId`, with a background `HttpNfcLeaseProgress` heartbeat that
   both keeps the lease + download session alive and surfaces percent. Nothing
   buffers a whole disk — the download stream feeds the lease PUT directly, and
   the PUT's `Content-Length` is the download response's own `Content-Length`
   (the `File.Info.size` is not guaranteed set before the download completes).
   A `*` device-URL host is substituted with the host the client connected to
   (the `HttpNfcLeaseDeviceUrl.url` spec contract). **Before a byte streams,
   the device host's certificate is pinned to the lease's `sslThumbprint`**
   (see the hardening note below).
6. `HttpNfcLeaseComplete` on success; `HttpNfcLeaseAbort` on any failure after
   the lease exists — vCenter then removes the half-created inventory objects,
   so a failed import never leaves a partial VM behind.

**Version-agnostic (AC5).** Every schema + method is core vim25 (`OvfManager`
/ `HttpNfcLease` predate 9.0), and the engine reads only the pre-9.0
`HttpNfcLeaseDeviceUrl` fields (`key` / `importKey` / `url` / `sslThumbprint`)
— never the 9.0-only `sslCertificate` — so it works on the VCF 5.x
migration-source fleet (#3056) as well as 9.0+.

**Device-host thumbprint pinning (#3284).** Each disk PUT streams to an
*absolute* per-device ESXi upload URL, not to `target.host`, so the pooled
client's per-target SSRF host re-screen (keyed to `target.host`) does not run
for the device host — a deliberate bypass, since screening would wrongly
block a legitimate private ESXi host. The replacement control is certificate
thumbprint pinning: `plan_transfers` captures each device URL's
`HttpNfcLeaseDeviceUrl.sslThumbprint` (a SHA-1 hash of the DER certificate in
colon-separated hex — the vim thumbprint convention `HostConnectSpec.sslThumbprint`
documents), and `ovf_transfer.verify_device_thumbprint` opens a pre-flight
`VERIFY_NONE` handshake to the device host and **refuses to stream a byte**
unless the presented certificate hashes to that attestation. A mismatch — or
a handshake that cannot obtain a certificate — raises `DeviceThumbprintError`,
which `_run_transfer` catches before `httpx.HTTPError`, aborts the lease, and
maps to a distinct `import_error` with a `security`-category issue (no disk
bytes flowed). **Missing-thumbprint policy: fail-open.** An empty / absent
`sslThumbprint` skips pinning and falls back to the pooled client's existing
TLS trust, because the vim spec sanctions an empty value ("Empty if no SSL
thumbprint is available or needed") — a conformant lease that omits the field
must still import, and pinning is a strict improvement layered on top of the
pre-existing "device URLs are trusted vendor data" posture, never a new gate
that fails closed on legitimate traffic. The SSRF host screen stays off for
device-URL PUTs; pinning is the replacement, comparison is
colon/case-insensitive, and the 64-hex-char case is accepted as SHA-256 for
forward-compatibility.

**Envelope.** Item resolution (id passthrough or name find, ambiguity-refusing)
reuses `_resolve_deploy_library_item`, and the outcome maps onto the **same**
`deployed` / `deploy_failed` / `deploy_error` family (#3071) as the deploy
composite, plus a per-disk `transfer` manifest (`{path, device_id,
size_bytes}`). `datastore` is **required** (the vim `CreateImportSpec` request
takes an explicit placement datastore, unlike the REST deploy which derives
it). As with the deploy composite, multi-GB imports should ride **async
governed dispatch** (#3079) so a dropped caller cannot cancel the in-flight
transfer.

**Live-appliance verification is deferred** (no lab access at implementation
time): the deliverable is the implementation + full conformance/unit coverage,
consistent with how every recent connector task shipped. The vim methods,
content-library download-session paths, and every emitted `_typeName` are
grounded against the pinned `vcenter.yaml` / `vi-json.yaml` by the reconcile
lanes (`..._l2_ingest_reconcile` + `..._write_body_reconcile`); the engine +
source mechanics are proven by mock-transport unit tests
(`test_connectors_vmware_rest_ovf_import.py` /
`test_connectors_vmware_rest_library_download.py`).

### vim cluster / inventory writes (`cluster.drs_rule.create` + `folder.create`, #2895)

Two more vim-only writes ride the #2893 substrate (Task E). Neither has a
usable REST write path (`/vcenter/cluster` is list/get/evc-mode only, and
the tag-based compute-policies surface is tag-scoped rather than an explicit
VM list; `/vcenter/folder` is GET-only — both spec-verified against the
pinned `vi-json.yaml`).

**`cluster.drs_rule.create`** adds a DRS affinity / anti-affinity rule by
*explicit VM list* through vim
`ClusterComputeResource.ReconfigureComputeResource_Task`. Flow: resolve the
rule's VM names to MoRefs **scoped to the cluster** (a *read*
`GET:/vcenter/vm?filter.names&filter.clusters`, un-gated) — fewer than two
resolve → `status='insufficient_vms'` before any write; read the cluster's
existing rules (`configurationEx.rule` via `RetrievePropertiesEx`, un-gated)
for the idempotence / name-collision check — a duplicate name →
`status='rule_exists'`, a **structured status rather than a raw vim
`DuplicateName` fault**; then issue a single-rule `ClusterConfigSpecEx`
delta with `modify=true` — `rulesSpec=[{operation: "add", info:
{_typeName: "ClusterA(nti)AffinityRuleSpec", name, enabled, vm:
[MoRefs]}}]` — through `_write_vmomi_sub_op`, and poll the returned Task via
`poll_vim_task`. The `_typeName` discriminator is required on the `spec`
(the request type declares the base `ComputeResourceConfigSpec`) and on the
`info` (the base `ClusterRuleInfo`); the `ClusterRuleSpec` array item and
the VM MoRefs need none (declared type == runtime type), mirroring the
disk-grow `VirtualDeviceConfigSpec` precedent. The park-time preview is the
fan-out blast-radius pattern (like `vm.power.bulk`): `{cluster,
cluster_name, rule_type, rule_name, enabled, resolved, total_resolved}`,
`resolved` capped at 20.

**`folder.create`** creates a VM folder under a named parent through vim
`Folder.CreateFolder` — which is **synchronous**: it returns the new Folder
`ManagedObjectReference` directly, **not** a `*_Task`, so (unlike
disk-grow / drs_rule) the returned MoRef is unwrapped into the result and
**never polled** (`poll_vim_task` is not called). Flow: resolve the parent
folder *name* to a moid (`GET:/vcenter/folder?filter.names&filter.type=VIRTUAL_MACHINE`,
un-gated; no match → `status='parent_not_found'`, >1 → `ambiguous_parent`,
both before any write), then issue the single `CreateFolder` through
`_write_vmomi_sub_op`. The preview is a param echo `{parent_folder,
new_folder_name}` (no I/O — the params fully name the blast radius). Both
mutating writes flow through the same `enforce_subop_policy` gate the REST
and disk-grow writes do, so an agent principal without a grant is denied and
a policy-parked write never reaches the wire.

### vim distributed-portgroup writes (`network.portgroup.create` + `network.portgroup.security.set`, #3091)

Two vim-only distributed-portgroup writes for standing up an L2 substrate —
the nested-hypervisor lab recipe. Neither surface has a REST write path
(there is no portgroup-create Automation path, and the security policy lives
in `DVPortgroupConfigSpec.defaultPortConfig.securityPolicy` with no REST
expression — both spec-verified against the pinned `vi-json.yaml`), so both
ride the #2893 governed vmomi substrate (`_write_vmomi_sub_op` →
`poll_vim_task`) like `host.detach_from_vds` / `cluster.drs_rule.create`.
Both live in the `networking` operation group beside the `portgroup.audit`
read (which surfaces the DVS / portgroup moids these writes take — there is
no portgroup/switch REST list to resolve a display name against, so the moid
is passed directly, the `host.detach_from_vds` `dvs`-moid convention).

**`network.portgroup.create`** creates a portgroup on an existing DVS through
vim `DistributedVirtualSwitch.CreateDVPortgroup_Task` with one
`DVPortgroupConfigSpec` — `name`, binding `type` (default `earlyBinding`, a
static portgroup), optional `numPorts`, and a `VMwareDVSPortSetting.vlan`
that is **either** a VLAN trunk
(`VmwareDistributedVirtualSwitchTrunkVlanSpec` with a `NumericRange[]`
`vlanId`, e.g. `0..4094` at bootstrap — the nested-ESXi case) **or** a single
access VLAN (`VmwareDistributedVirtualSwitchVlanIdSpec`). Passing both
refuses with `status='invalid_vlan_spec'` before any write; passing neither
inherits the switch default. The returned Task's `TaskInfo.result` MoRef is
the new portgroup; its `config` (`name` + `defaultPortConfig.vlan`) is read
back into the response's `observed` verification rows.

**`network.portgroup.security.set`** sets any of the security-policy triple —
`allowPromiscuous` / `forgedTransmits` / `macChanges` (the knobs a nested-ESXi
trunk portgroup needs at Accept) — through vim
`DistributedVirtualPortgroup.ReconfigureDVPortgroup_Task` with a
`DVPortgroupConfigSpec.defaultPortConfig.securityPolicy` delta. The spec's
**required `configVersion`** (optimistic-concurrency echo, the
`host.detach_from_vds` pattern) and the current policy are read off `config`
first via `RetrievePropertiesEx` (the `previous` verification rows); **only
the booleans supplied are written**; the applied policy is read back after
(`observed`). No boolean supplied → `status='no_change_requested'` before any
read/write. `DVSSecurityPolicy` and each `BoolPolicy` derive from
`InheritablePolicy`, whose `inherited` field the spec marks required, so both
carry `inherited: false` ("explicitly set", not inherit). Governance note:
promiscuous mode makes the portgroup see all traffic on its VLANs, so
`security.set` is a governance-sensitive write — it rides the same
`dangerous` / `requires_approval=True` tier and the same `enforce_subop_policy`
gate as every other write composite (there is no `caution` tier in this
connector; only `vm.destroy` is `destructive`). Neither composite registers a
park-time preview builder (matching `vm.import_from_library`); the read-back
`previous` / `observed` rows in the response are the verification surface.

### Read-composite best-effort enrichment (`datastore.usage`, #1908)

Read composites distinguish **load-bearing** sub-ops from **optional
enrichment** sub-ops. A load-bearing failure routes through
`_require_ok` and raises `CompositeSubOpError`, which the dispatcher
wraps into a `connector_error` envelope -- the whole composite fails.
An optional enrichment failure must **not** sink the composite: the
leg degrades and the rows the core use case needs are still returned.

`datastore.usage` is the canonical example. Its layout is
`GET:/vcenter/datastore` (listing, load-bearing) → per row
`GET:/vcenter/datastore/{datastore}` (capacity/free/type, load-bearing)
→ a **single batched VM-placement read** (`_read_vm_placement`,
**best-effort**) resolved once for the whole result. The "which
datastores are filling up?" use case is satisfied by the
capacity/free/type reads, which have already succeeded by the time the
placement read runs. So when that read errors, every row is still
returned with `capacity`/`free_space`/`type` intact, `vm_count`/`vm_names`
set to `null`, and an `enrichment_note` string recording the failure.
The response schema marks `vm_count`/`vm_names` as nullable and adds the
optional `enrichment_note` key (present only when enrichment was skipped
or discarded).

**Placement is vim-authoritative, not a server-side filter (#2975).**
The original layout scoped each row with
`GET:/vcenter/vm?filter.datastores=<ds>` — one filtered call per
datastore — and trusted the target to honour it. Some builds silently
ignore that filter and return the whole-inventory VM list on every call,
so every datastore row was enriched with the identical global list
(`vm_count` = cluster-wide total, identical `vm_names` on every row),
poisoning the classic "which VMs live on this filling-up datastore?"
triage question with the same wrong answer everywhere. The seam that
strips the `filter.` prefix on modern mounts (#2298) is not the cause;
the code cannot emit `/api` + `filter.datastores`, and the real cause is
undetermined from code (legacy `/rest` mount vs the build ignoring the
bare `datastores=` param). The fix stops depending on the filter
altogether: `_read_vm_placement` reads `GET:/vcenter/vm` **unfiltered**
(every VM's moid + name), then one `RetrievePropertiesEx` over every VM
reading the vim-authoritative `VirtualMachine.datastore` property (the
union of datastores a VM's config/disks/snapshots/swap sit on), and
joins the two client-side into per-datastore `vm_count`/`vm_names`. Each
row therefore carries only the VMs vim reports on that datastore
regardless of whether any REST filter is honoured; a VM whose disks span
two datastores legitimately appears under both. This also replaces N
per-datastore calls with two batched reads.

The **cross-row identical-sets guard** (#3049) is retained as
belt-and-braces: an identical **non-empty** VM set across *every*
enriched row is nulled + noted rather than emitted as per-datastore
placement. With authoritative placement that shape cannot arise in
practice (it would require every VM to sit on every datastore), so the
guard's firing condition is effectively impossible — but it stays to
refuse any populated-but-identical anomaly. The non-empty condition
excludes the legitimate all-empty case (every datastore really has zero
VMs), and the guard needs at least two enriched rows to compare, so a
single-datastore result is never touched.

### Per-entity capacity sensing (`filter_names`, #2758)

`datastore.usage` takes an exact-match `filter_names` param (array of
datastore names, forwarded to the listing as `filter.names`). Passing
**exactly one** name is the per-datastore Sensor pattern: the one-row
result returns inline (unsampled), so a Sensor can select
`$.datastores[0].free_space` (bytes) under a `ThresholdCompare` and
threshold a single vSAN/VMFS store's free space — the capacity-tier
monitoring the v0.26.0 ops report needed. No new param, no fuzzy
matching, no top-N; the filter is exact-match only.

Why this is not automatic: the dispatcher's JSONFlux reducer collapses
any op result serialising past `byte_threshold` (4096 bytes, the default
`JsonFluxReducer()` installed at `main.py`) into a sampled envelope
(`{row_count, …, source_key}`). A single row is one row (well under the
50-row bound), but the best-effort `vm_names` list is unbounded — a
VM-dense datastore (a vSAN with a few hundred VMs) pushes one row past
4096 bytes, the whole `{"datastores": [row]}` collapses to the envelope,
and `$.datastores[0].free_space` is no longer selectable. So `vm_names`
is **bounded to a sample** (`DATASTORE_USAGE_MAX_VM_NAMES`, 20; the row's
`maxItems`), while `vm_count` stays the exact total — `vm_count >
len(vm_names)` signals truncation. Bounding the enrichment payload (not
raising the global JSONFlux threshold) is what keeps a one-name filtered
row reliably inline and assertable. `free_space`/`capacity` are bytes;
band them directly (`op: lt`, `degraded`/`critical` in bytes) — no
server-side `free_gb`/percent derivation.

`capacity`/`free_space` are read from the per-datastore detail payload
with a **list-row fallback** (#2078): some vCenter builds (observed on an
8.0.3 vCenter against the 9.0 spec) return a detail `Datastore.Info` that
populates `free_space` but omits `capacity`, while the
`GET:/vcenter/datastore` listing row carries both. The row-builder takes
the detail value when present and falls back to `entry.get(...)` on the
already-fetched listing row otherwise, so `capacity`/`free_space` are
`null` only when neither source carries them — the composite no longer
discards a capacity it already had, which is what made `%`-full
uncomputable off the composite alone.

### Bubbling a sub-op's structured error (#1908)

`CompositeSubOpError` folds the failed sub-op's most diagnostic line
into its message via `_describe_sub_op_failure`, rather than stopping at
the terse `error` summary (`connector_error: HTTPStatusError`). The
helper prefers the structured `http_status` + `upstream_message` the
dispatcher's 403/422/auth builders extract; for every other status
(400/404/5xx, routed through the generic `connector_error` builder) it
falls back to `extras["exception_message"]` -- the stringified
`httpx.HTTPStatusError`, which already carries the status code **and**
the offending URL. The same helper feeds `datastore.usage`'s
`enrichment_note`. Net effect: the 400 + URL that previously only showed
on a manual sub-op replay now ride the composite's error envelope (or
the per-row note). The `returned status='<status>'` clause is preserved
so existing string-matching consumers keep working.

### Park-time approval previews (#1608)

All 19 write composites ship `requires_approval=True`, so a human/agent
dispatch parks as a durable `ApprovalRequest` row. Pre-#1608 that row's
`proposed_effect` was the identifier-only default `{op_id, connector_id,
target_id}` — and since the dispatch `params` are deliberately never
serialised onto a reviewer-facing surface (#1503), the four-eyes
approver could not tell a one-VM power cycle from a 1000-VM outage.

`composites/_write_preview.py` registers one preview builder per write
composite on the generic per-op hook (`register_preview_builder`,
`operations/_preview.py`, #1437). The builder result lands under
`proposed_effect["preview"]`, wrapped with the op's sensitivity
`op_class` (see [`approvals.md`](approvals.md)). Three depths:

| Composite | Preview | Depth |
| --- | --- | --- |
| `vm.power.bulk` | `{action, filter, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `host.evacuate` | `{host, tolerate_partial_failure, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `host.detach_from_vds` | `{host, dvs, fallback_network, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `cluster.patch` | `{cluster, resolved, total_resolved}` | live read (`GET:/vcenter/host?clusters=...`) |
| `vm.resize` | `{vm, name, power_state, current, requested}` sizing from->to | live read (`GET:/vcenter/vm/{vm}`) |
| `vm.nic.repoint` | `{vm, name, nic, mac_address, current_backing, requested_backing}` network from->to | live read (`ethernet/{nic}` + `GET:/vcenter/network`) |
| `vm.device.cdrom` | `{vm, name, cdrom, action, current_backing, state}` (the host-local ISO path) | live read (`cdrom/{cdrom}`) |
| `vm.create` | creation-spec echo (name, guest_os, placement pins — folder_name, folder (#3115), resource_pool, datastore, host (#3096) — sizing, networks, disks_gb (#3117), nested_hv, power-on) | param echo, no I/O |
| `vm.clone` | clone-coordinates echo | param echo, no I/O |
| `vm.deploy_from_library` | deploy-coordinates echo (item ref, placement, network mappings, provisioning, `ovf_property_keys` — **ids only**, never values #1503, power-on) | param echo, no I/O |
| `vm.snapshot.revert` | `{vm, snapshot_name}` echo | param echo, no I/O |
| `vm.migrate` | `{vm, cluster, target_host, target_host_source}` | param echo, no I/O |
| `vm.power` | `{vm, verb, power_kind}` echo (`power_kind` = `hard` vs Tools-soft `guest`) | param echo, no I/O |
| `cluster.drs_rule.create` | `{cluster, cluster_name, rule_type, rule_name, enabled, resolved, total_resolved}` | live read (`GET:/vcenter/vm` + `GET:/vcenter/cluster/{cluster}`) |
| `folder.create` | `{parent_folder, new_folder_name}` echo | param echo, no I/O |

The `#2891` hardware previews are a live-read **from->to diff**: the
delta between the VM's current sizing / NIC backing / CD-ROM backing
and the requested change is exactly what the approver signs off, so
the builder reads current state via the shared
`_write._read_vm_info` / `_write._read_ethernet_nic` /
`_write._read_cdrom` / `_write._resolve_distributed_portgroup` helpers
(the same ones the handlers use) and pairs it with the request. The
fan-out live-read previews resolve the same entity set the approved
dispatch would act on, through the **same shared helpers** the handlers
use at dispatch time (`_write._resolve_vm_list` /
`_write._resolve_cluster_hosts`) — one resolution code path, two call
sites. The `resolved` list is capped at 20 entries
(`_PREVIEW_RESOLVED_CAP`), identity-only per row (`vm`/`host`, `name`,
`power_state`); `total_resolved` always carries the uncapped count. The
five param-echo composites name their full blast radius in params, so
no read can change what the preview says; `vm.migrate` deliberately
does **not** pre-resolve a DRS recommendation (point-in-time output
would mislead the reviewer — the preview says
`target_host_source="drs_at_execution"` instead).

At park time the composite handler never runs, but the shared
`_write._resolve_vm_list` / `_write._resolve_cluster_hosts` helpers are
now direct-session (`#2256`): the live-read builders call them with the
connector instance the dispatcher resolved into the `PreviewContext`
(`ctx.connector_instance`), so the one listing `GET` runs straight on the
session. Because it is a direct read, the three properties the old
park-time `dispatch_child` adapter enforced hold intrinsically — no
policy-gate re-entry (a direct call cannot re-enter the dispatcher), no
unparented audit rows (a direct read writes none), reads-only (the
helpers only ever issue the listing `GET`). This also fixes the
fresh-boot gap the pre-`#2256` preview had: the retired adapter resolved
the sub-op against an **ingested** descriptor row, so on a
zero-catalog-ingest deploy the live-read preview always degraded to
`preview_unavailable`; the direct-session read now resolves the entity
set on a fresh boot. This mirrors how the k8s.apply dry-run (#1437), the
argocd snapshot reads (#1452), and the vault capability probe (#1504) run
their preview I/O — connector-level, un-dispatched.

Everything is fail-soft — the park always proceeds — but a decline and
a failure degrade differently (#1628): a builder that *declines*
(malformed params, or no resolved connector instance) parks with the
identifier-only default, while a builder that *raises* (vCenter
unreachable, the listing read errors on this deploy) parks with the
identifier fields **plus** `preview_unavailable: true` and a
`preview_error` reason naming the failed read. The marker rides through
every reviewer surface that renders `proposed_effect` verbatim (REST
`GET /api/v1/approvals`, `meho_approvals_list` / `.get`, `meho
approvals show`), so "blast-radius unknown" is distinguishable from a
genuinely small action. The 7 read composites register no builder —
they never park.

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` (G0.2-T3
  #242) — transport plumbing (retry, timeout, per-target pool,
  `_request_json` / `_post_json`).
- `meho_backplane.connectors.registry.register_connector_v2` (G0.6-T2
  #393).
- `meho_backplane.connectors.schemas` — `AuthModel`,
  `FingerprintResult`, `OperationResult`, `ProbeResult`.
- `meho_backplane.operations.dispatch` (G0.6-T5 #396) — invoked by
  `execute()`'s legacy shim.
- `httpx` (transitively via `HttpConnector`).
- `structlog` for structured log events.
- Test-only: `respx` for HTTP mocking in unit tests, `testcontainers`
  for the vcsim-backed integration test.

## Known issues / gaps

- **Default loader stubbed** — `load_session_credentials_from_vault`
  raises `NotImplementedError` until G0.3 (#224) lands. Production
  deploys must inject a custom `session_loader` at connector
  construction. Same pattern as `KubernetesConnector(kubeconfig_loader=…)`.
- **`auth_model` enum gating** — only `shared_service_account` (and
  `None` for pre-G0.3 targets) is accepted. `per_user` and
  `impersonation` raise `NotImplementedError`; both are deferred to
  v0.2.next.
- **Reactive 401 recovery at the dispatch path (#2067)** — vSphere's
  ~5-minute idle timeout means a long-idle cached session may see a 401 on
  the next dispatch. The connector caches one token per `(tenant_id,
  target.id)` with no TTL, and `_request_json`/`_post_json` do not retry 401
  themselves. Recovery instead lives at the generic-ingested dispatch arm:
  on an auth-class status it calls the connector's public
  `invalidate_session(target)` hook (which pops the cached token + login
  path under `_session_lock`, keyed on `target_cache_key`) and re-dispatches
  the op once, so the next transport call misses the cache and re-runs
  `_establish_and_cache_session` — re-authenticating and re-running the
  modern→legacy `/api/session` 404 fallback. A second 401 (re-login also
  failed) surfaces as `connector_auth_failed`. **Proactive** token TTL
  (re-mint before the doomed call) remains v0.2.next polish.
- **`vi-json.yaml` ingestion live** — T3 (#503) shipped the ingestion
  pipeline (depends on T2 / #501's `$ref` resolver). The same
  connector dispatches the ~2,195 vi-json ops alongside the ~1,275
  vCenter REST ops; both share the `vmware-api-session-id` session
  header per `docs/vcenter-9.0/MANIFEST.md`. Two of the read
  composites (`event.tail`, `performance.summary`) call vi-json
  sub-ops; the other three call vCenter REST sub-ops only.
- **VI-JSON mount for vmomi reads *and writes* — `/sdk/vim25/{release}`
  (#2466, mutating writes #2893)** —
  vmomi (VI-JSON) methods (`RetrievePropertiesEx`,
  `VsanQueryVcClusterHealthSummary`, `QueryEvents`,
  `QueryAvailablePerfMetric`, `QueryPerf`, and now the mutating
  `ReconfigVM_Task`) are served under the
  documented release-versioned base
  `/sdk/vim25/{release}/{MoType}/{moId}/{method}` (Broadcom Web Services
  SDK guide, "Building JSON Request URLs"; available since vCenter 8.0U1,
  same scheme on 9.x) — **not** the vSphere Automation `/api` mount that
  `mount_op_path` resolves for `/vcenter/*` paths. Mounting a vmomi
  method on `/api` 404s on vCenter 8.0.x (observed on a live 8.0.3 host).
  `VmwareRestConnector._post_vmomi_json` owns this seam: for a
  modern-session target it derives `{release}` via `_about_version` —
  `GET /api/about`'s `version` (`8.0.3` → `8.0.3.0`), falling back on a
  404 to the vim25 API version from `/sdk/vimServiceVersions.xml`
  (vCenter serves no `/api/about`, #2765; the discovery document's
  four-part value is exactly the VI-JSON release), resolved once and
  cached in `_about_versions` — POSTs `/sdk/vim25/{release}{path}`, and on a 404
  falls back **once** to the `/api`-mounted form (the undocumented
  accommodation the 9.0.2 fleet serves); when both 404 the raised
  `RuntimeError` names both attempted URLs + the vCenter version so a
  best-effort caller's `read_note` is self-explanatory. Legacy/vcsim
  targets (session on `/rest`) skip VI-JSON entirely and mount the vmomi
  method on `/rest` (the pre-#2466 behaviour, so the vcsim integration
  lane is unchanged). Every typed vmomi read, the read-composite vmomi
  POST sub-ops, and the mutating `vm.disk.grow` write route through
  `_post_vmomi_json`; only the vSphere Automation `GET /vcenter/*` legs
  still use `mount_op_path`.
- **All hand-authored composites shipped** — T5 (#508) ships 5 read;
  #2080 + #2135 add two more reads (`host.network_uplinks` /
  `host.vsan_health`, later re-shipped as typed ops in #2258); T6
  (#509) ships 8 write composites, #2301 adds a 9th (single-VM
  `vm.power`, incl. Tools soft shutdown), #2893 adds a 10th (the
  mutating VI-JSON `vm.disk.grow`), #2894 an 11th (the folder-template
  `vm.clone_from_template`), #2895 a 12th + 13th (the vim cluster /
  inventory writes `cluster.drs_rule.create` + `folder.create`), and
  #2891 a 14th / 15th / 16th (the post-clone hardware reconfigure trio
  `vm.resize` / `vm.nic.repoint` / `vm.device.cdrom`) — 21
  composites today. The
  "All hand-authored composites land as endpoint_descriptor rows with
  source_kind='composite'" Definition-of-done line in [#227](https://github.com/evoila/meho/issues/227)
  is fully ticked.
- **`vm.clone` task polling is wall-clock bounded** — the composite
  blocks up to `timeout_seconds` (default 600s) before returning
  `status='timeout'`. The vSphere task may still complete in the
  background; callers should poll `GET:/cis/tasks/{task}` if
  long-running deploys are normal. An async-task-tracking substrate
  is v0.2.next.
- **Per-VM rollback for `vm.power.bulk` is by design absent** — bulk
  power operations are intentionally non-transactional. Partial-
  failure tolerance is the documented contract; a transactional
  bulk-power would require vSphere-side support that doesn't exist.
- **`cluster.patch` sequential, not concurrent** — concurrent host
  patches would overwhelm DRS by forcing every VM in the cluster to
  vMotion at once. The composite serialises hosts and lets DRS
  rebalance between iterations.
- **`network.portgroup.audit` op_id reconciliation (#1602)** — the
  read composite originally dispatched
  `GET:/vcenter/network/distributed-switch` and
  `GET:/vcenter/network/distributed-portgroup` (both **singular**),
  neither of which resolves against the canonical `vmware/9.0` ingest.
  The vSphere Automation REST distributed-switch resource is **plural**
  (`GET:/vcenter/network/distributed-switches`, a preview feature), and
  there is **no** dedicated distributed-portgroup list resource at all —
  distributed portgroups are enumerated via the generic
  `GET:/vcenter/network` resource filtered to the
  `DISTRIBUTED_PORTGROUP` type. **#2970 update:** the first run against
  the *canonical pinned* `vcenter.yaml` showed the plural
  `distributed-switches` path exists only under the NSX-scoped
  `/vcenter/namespace-management/` tree — there is **no** generic DVS
  list resource in the pinned REST spec at all — so the audit's DVS-list
  step was dropped entirely. `dvs_name` is now always `null` (the
  generic `Network` summary carries no parent-DVS field to join on, so
  the name index was never consulted with a hit anyway), `dvs` stays
  best-effort from the portgroup row itself, and `filter_dvs` is
  accepted but inert.
  A build-time guard
  (`tests/acceptance/test_portgroup_audit_op_id_reconcile.py`) parses
  the pinned `vcenter.yaml` and asserts every audit sub-op_id is emitted
  by the ingest. The vendor-licensed spec-shelf is not provisioned in
  public-repo CI, so that guard **skips** there (the #1602 convention);
  it runs wherever the spec-shelf is wired — an operator deploy or a
  spec-shelf-backed run — which is where a future drift surfaces.
- **`host.detach_from_vds` write-side portgroup fix: done (#2891);
  real-spec reconcile guard: done (#2944)** — `_write.py` once carried
  `_OP_LIST_PORTGROUPS = "GET:/vcenter/network/distributed-portgroup"`
  (singular, absent from the pinned spec — the same class of defect
  #1602 fixed on the read side). #2891 corrected it to `_OP_LIST_NETWORK
  = "GET:/vcenter/network"`, so `_SUB_OPS_HOST_DETACH_FROM_VDS` now
  dispatches the resolvable path (see "Portgroup resolution — the #1602
  lesson" above). #2944 then closed the guard gap: the always-on
  reconcile in `test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`
  synthesises its fixture *from* the constants (proves op_id **shape**,
  cannot catch a wrong key), so #2944 added an env-gated assertion that
  parses the real pinned `vcenter.yaml` and asserts every `_SUB_OPS_*`
  REST path actually **exists** — skipping in CI where the spec-shelf is
  unprovisioned (the #1602 convention), running wherever it is wired.
- **#2970 real-spec repoint** — running the #2944 guard against the real
  shelf (the #2949 verification) found 11 op_ids the pinned
  `vcenter.yaml` does not serve. The composites were repointed:
  `vm.clone`'s deploy moved to the per-item
  `POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy`
  (synchronous — the cis-task poll went away), cluster-host listing to
  `GET:/vcenter/host` + `clusters` filter, `vm.create`'s NIC attach to
  `POST:/vcenter/vm/{vm}/hardware/ethernet`, `host.detach_from_vds`'s
  NIC migration to per-adapter
  `PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}`, and `cluster.patch`'s
  patch step to the vLCM
  `POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true`.
  Surfaces with **no** REST path in the pinned spec switched to vim
  (`_VIM_SUB_OPS_*`, reconciled against `vi-json.yaml`): snapshot
  list/revert (`VirtualMachine.snapshot` +
  `VirtualMachineSnapshot.RevertToSnapshot_Task`), host maintenance
  (`HostSystem.EnterMaintenanceMode_Task` / `ExitMaintenanceMode_Task`),
  DRS recommendations (`ClusterComputeResource.drsRecommendation`), and
  DVS host removal (`DistributedVirtualSwitch.ReconfigureDvs_Task` with
  the `config.configVersion` read). The audit's DVS listing was dropped
  (degradation note above).
- **#2986 read-composite residual + exhaustive read lane** — #2970's
  sweep covered the write composites; the read side's
  `_OP_GET_CLUSTER_DRS = "GET:/vcenter/cluster/{cluster}/drs"` was
  recorded as an adjacent finding (unserved by the pinned
  `vcenter.yaml`, which has **no** cluster DRS REST resource — the
  `/vcenter/cluster` family is list/get/evc-mode only). #2986 switched
  the `cluster.drs_recommendations` DRS leg to vim: one
  `RetrievePropertiesEx` on the `propertyCollector` singleton reading
  `ClusterComputeResource.configurationEx.drsConfig`
  (`ClusterDrsConfigInfo`) and — when
  `include_recommendations_history=True` — `drsRecommendation` in the
  same call (the surface `vm.migrate`'s DRS lookup already uses; the
  response envelope keys are unchanged, `drs` now carries the vim
  config object). The same task added the exhaustive read lane
  (`tests/test_connectors_vmware_rest_composites_read_reconcile.py`,
  #2980 harness): every `_OP_*` constant in `_read.py` is introspected
  live, cross-checked against the `_SUB_OPS_*` manifests, and asserted
  against the spec serving its dispatch leg — GET legs vs
  `vcenter-9.0/vcenter.yaml`, vmomi POST legs vs
  `vcenter-9.0/vi-json.yaml` — skipping uniformly without the shelf,
  running for real in CI.

## References

- Governed content-library ISO import-from-URL + `iso.image`
  mount/unmount recipe (raw ingested ops, no composite):
  [vmware-rest-governed-iso-path.md](vmware-rest-governed-iso-path.md) (#3086).
- Governed nested-hypervisor VM recipe (composite create with `VMKERNEL_*`
  pass-through + raw disk/cdrom adds + VI-JSON VHV via `nestedHVEnabled`):
  [connectors-vmware-rest-nested-esxi-recipe.md](connectors-vmware-rest-nested-esxi-recipe.md) (#3087).
- Parent Initiative: [#227 G3.1 vmware-rest-9.0](https://github.com/evoila/meho/issues/227)
- Parent Task: [#498 G3.1-T1 VmwareRestConnector](https://github.com/evoila/meho/issues/498)
- Composite-helper Task: [#504 G3.1-T4 register_composite_operation()](https://github.com/evoila/meho/issues/504)
- Read-composite Task: [#508 G3.1-T5 vmware-rest read composites](https://github.com/evoila/meho/issues/508)
- Write-composite Task: [#509 G3.1-T6 vmware-rest write composites](https://github.com/evoila/meho/issues/509)
- Composite recursion substrate: [#398 G0.6-T7 composite recursion infrastructure](https://github.com/evoila/meho/issues/398)
- G0.7 canary that ingested the rows this connector dispatches:
  [#408 G0.7-T8 vSphere canary](https://github.com/evoila/meho/issues/408)
  (closed via PR #493 on 2026-05-15).
- vSphere REST session contract:
  [vSphere Automation API security schema](https://developer.broadcom.com/xapis/vsphere-automation-api/latest/api-security-schema/).
- vcsim simulator: <https://github.com/vmware/govmomi/tree/main/vcsim>.
- Closest in-repo precedents:
  - Package layout + v2 registration pattern:
    `backend/src/meho_backplane/connectors/vault/__init__.py`.
  - Injectable-loader Protocol pattern:
    `backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py`.
  - `auth_headers` + `_request_json` HTTP plumbing:
    `backend/src/meho_backplane/connectors/adapters/http.py`.

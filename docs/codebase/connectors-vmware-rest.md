# Connector: vmware-rest (vSphere 8.5+ / 9.0)

## Overview

The `vmware-rest` connector is the hand-rolled `HttpConnector` subclass
that dispatches ingested vCenter REST operations under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple. It pairs with the G0.7 ingestion pipeline's auto-shim (which
makes ~1,275 + ~2,195 `endpoint_descriptor` rows resolvable but not
dispatchable) to deliver real session-authenticated calls against
vSphere 8.5+ / ESXi 8.5+ targets, plus 73 hand-authored composites
that orchestrate cross-spec workflows: 16 read composites
(G3.1-T5 / `#508`; the `host.network_uplinks` / `#2080` and
`host.vsan_health` / `#2135` reads were later re-shipped as typed ops
in `#2258`; plus the four guest-operations reads `#3100` and the
Supervisor status read `#3281` + the vSphere Namespace status read
`namespace.status` / `#3502` + the storage-policy list read `#3494` +
the two SUBSCRIBED content-library reads `content_library.subscribed.status` +
`content_library.subscribed.items.list` / `#3495` + the datastore
cache-refresh read `datastore.refresh` / `#3789` + the VM limit and
reservation read `vm.resource_allocation.show` / `#3880`) and 57 write
composites (G3.1-T6 / `#509`, incl. the destructive-tier `vm.destroy` / `#3198`, the
governed vSphere Namespace `namespace.create` (caution) +
`namespace.delete` (destructive) / `#3502` (see the **Governed vSphere
Namespace composites** subsection under Control flow), the
governed NFS tag-based SPBM `storage_policy.create` (caution) +
`storage_policy.delete` (destructive) / `#3494`, the
single-VM `vm.power` verb incl. Tools soft shutdown / `#2301`, the
mutating VI-JSON `vm.disk.grow` / `#2893` + the WSFC/FCI shared-attach
`vm.disk.attach` / `#3256`, the folder-template
`vm.clone_from_template` / `#2894`, the vim cluster / inventory writes
`cluster.drs_rule.create` + `folder.create` / `#2895`, the governed
resource-pool allocation writes `resource_pool.create` (`caution`) +
`resource_pool.delete` (`dangerous`) and the VM-Host affinity
`cluster.drs_vm_host_rule.create` (`caution`) / `#3505`, the `#2891`
post-clone hardware reconfigure trio `vm.resize` / `vm.nic.repoint` /
`vm.device.cdrom`, the VM CPU / memory limit + reservation write
`vm.resource_allocation.set` (`caution`, with its safe read
`vm.resource_allocation.show`) / `#3880`, the two guest-customization (GOSC) composites
`guest.customization_spec.create` + `vm.customize` / `#2892`, the
OVF/OVA content-library deploy `vm.deploy_from_library` / `#2909`, and
the three host-domain writes `host.datastore_mount_nfs` /
`host.disk_mark_flash` / `host.service_control` / `#3182`, the three vim
distributed-portgroup writes `network.portgroup.create` +
`network.portgroup.security.set` / `#3091` +
`network.portgroup.vlan.set` (VLAN reconfigure of an existing portgroup),
the content-library import
`vm.import_from_library` / `#3229`, the two governed
guest-operations writes `vm.guest.file.write` / `#3100` +
`vm.guest.program.run` / `#3255` (see
`connectors-vmware-rest-guest-ops.md`), plus the two Supervisor (WCP)
namespace-management writes `supervisor.enable` + `supervisor.disable`
/ `#3281` (see the **Supervisor (WCP) namespace-management composites**
subsection under Control flow), and the two SUBSCRIBED content-library
caution writes `content_library.subscribed.create` +
`content_library.subscribed.sync` / `#3495` (see the **Content-library
SUBSCRIBED composites** subsection), and the seven teardown composites /
`#3339` -- six destructive deletes `network.portgroup.delete`,
`host.standard_portgroup.delete`, `folder.delete`, `datastore.file.delete`,
`content_library.delete`, `content_library.item.delete`, plus the caution
`datastore.dir.create` (see **Governed teardown deletes**), the destructive
DRS rule delete `cluster.drs_rule.delete` (see **Governed DRS rule delete**), the
CD drive add `vm.cdrom.add` (`dangerous`) and the VMware Tools installer unmount
`vm.tools.installer.unmount` (`caution`) (see **VM hardware read, CD drive add and
Tools installer unmount**), the VM snapshot writes `vm.snapshot.create` (caution)
+ `vm.snapshot.delete` (destructive) (see **VM snapshot operations**), the licence
assign `license.assign` (see **vSphere licences**), and the two caution alarm changes
`alarm.acknowledge` + `alarm.reset` / `#4383` (see **Alarms**)). The
write composites cover every state-mutating operator workflow named
in [#214](https://github.com/evoila/meho/issues/214) as required for
govc-wrapper retirement.

Source: `backend/src/meho_backplane/connectors/vmware_rest/`.

## Key types

- **`VmwareRestConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vmware"`, `version="9.0"`,
  `impl_id="vmware-rest"`, `supported_version_range=">=8.5,<10.0"`,
  `priority=1`.
- **Missing managed objects on typed PropertyCollector reads (`#3481`, `#4776`)** — vCenter reports
  an addressed, deleted vim object as a `ManagedObjectNotFound` fault inside
  an HTTP 500. On the vCenter VI-JSON transport the fault body is JSON
  (`{"_typeName": "ManagedObjectNotFound", "obj": {...}}`, the shape the
  pinned `vi-json.yaml` documents and vCenter 9.1 sends); a standalone ESXi
  host sends the SOAP-shaped XML fault. The addressed read operations `vmware.vm.info`,
  `vmware.host.usage`, `vmware.host.storage_devices`,
  `vmware.host.network_uplinks`, and `vmware.object.collect` explicitly opt
  into promotion on their `PropertyCollector.RetrievePropertiesEx` calls, so the connector
  reads the fault in either form (JSON first, then XML) and raises
  `ConnectorResourceNotFoundError` with the requested MoID(s). A JSON fault
  names the missing object in `obj`; it maps to `not_found` only when that
  object is one the read asked for (a missing property collector is not a
  missing VM), and a JSON fault without `obj` reports every addressed
  object, as the XML form does. The dispatcher
  returns `status="not_found"` with `extras.error_code="not_found"`,
  `extras.resource_ids`, and the singular `extras.resource_id` when one object
  was addressed. A different fault, a non-PropertyCollector vmomi method, or
  a transport-only 5xx remains `connector_error`; the mapping never infers
  absence from the status code. Promotion defaults off on the shared seam, so
  task polls and destructive-composite preflight/execute reads retain their
  existing failure semantics; #3479 owns those paths.
- **Read composites** (`composites/_read.py`) — 6 module-level
  `async def` handlers (`cluster_drs_recommendations_composite`,
  `event_tail_composite`, `datastore_refresh_composite`,
  `performance_summary_composite`, `datastore_usage_composite`,
  `network_portgroup_audit_composite`). The other read composites live
  next to their feature area (for example the guest reads in
  `_guest.py`); `test_connectors_vmware_rest_doc_composite_counts.py`
  keeps this list and the counts below in step with the registry.
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
  Two former read composites were re-shipped as typed ops in `#2258`
  and keep their behaviour: `vmware.host.network_uplinks` (`#2080`) lists hosts via
  `GET:/vcenter/host`, then per host reads `config.network.pnic` +
  `config.network.proxySwitch` through the vi-json PropertyCollector
  `RetrievePropertiesEx` method — the pnic link-state / uplink mapping
  the plain REST surface cannot reproduce (drives physical
  switch-port-occupancy reasoning); the per-host property read is
  best-effort (a failed read nulls the network detail with a
  `read_note` rather than sinking the op).
  `vmware.host.vsan_health` (`#2135`) queries the vSAN
  health-service method `VsanQueryVcClusterHealthSummary` on the
  `vsan-cluster-health-system` object, scoped to the target
  cluster's MoRef — the `govc vsan.health.*` equivalent, returning the
  cluster-wide `overall_health` colour plus the health-test `groups`
  list. It is **not** best-effort (`#4441`): when the vSAN call fails,
  the operation returns an error with vCenter's fault type and text, and
  a cluster id that does not exist gives `not_found`. It never returns
  empty health fields that look like a healthy cluster. How the call is
  sent: see "vSAN calls" below.
- **Write composites** (`composites/_write.py`) — the module-level
  `async def` handlers (`vm_create_composite`, `vm_clone_composite`,
  `vm_deploy_from_library_composite`, `vm_import_from_library_composite`,
  `vm_snapshot_revert_composite`, `vm_migrate_composite`,
  `vm_power_composite`, `vm_power_bulk_composite`,
  `vm_resize_composite`, `vm_nic_repoint_composite`,
  `vm_device_cdrom_composite`,
  `host_evacuate_composite`, `host_detach_from_vds_composite`,
  `network_portgroup_create_composite`,
  `network_portgroup_security_set_composite`,
  `network_portgroup_vlan_set_composite`,
  `cluster_patch_composite`, `guest_customization_spec_create_composite`,
  `vm_customize_composite`, and the `#3505` governed-allocation writes
  `resource_pool_create_composite` / `resource_pool_delete_composite` /
  `cluster_drs_vm_host_rule_create_composite`). The `resource_pool.create`
  handler issues the REST `POST:/vcenter/resource-pool` through the same
  governed `_write_sub_op` seam the other REST writes use, resolving the
  parent either from an explicit `parent` moid or (the estate convenience)
  from a `cluster` moid whose root resource pool it reads via one vim
  `RetrievePropertiesEx` of `ClusterComputeResource.resourcePool`;
  `resource_pool.delete` counts the pool's child pools + VMs first and
  refuses (`not_empty`) a populated pool unless `force=true` (the REST
  `DELETE` reparents children up to the parent). The VM-Host affinity
  `cluster.drs_vm_host_rule.create` is a **sibling** of
  `cluster.drs_rule.create` (which stays VM-VM only, contract unchanged):
  one `ReconfigureComputeResource_Task` carries a `ClusterConfigSpecEx`
  `groupSpec` delta (adds a `ClusterVmGroup` + `ClusterHostGroup`) plus a
  `rulesSpec` delta (adds a `ClusterVmHostRuleInfo` referencing those groups
  by name, with a `mandatory` must/should flag), riding the governed
  `_write_vmomi_sub_op` seam + `poll_vim_task` like the VM-VM rule. The
  `vm_resize` / `vm_nic_repoint` /
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

  **Terminal-error envelopes dispatch as errors (`#3809`).** A composite
  that hits a load-bearing sub-op failure *returns* a structured
  envelope carrying an `issues[]` entry with `severity == "error"` (e.g.
  `content_library.subscribed.create`'s `create_error` /
  `datastore_not_found`, the OVF `deploy_failed` / `deploy_error`,
  `folder.create`'s `parent_not_found` / `ambiguous_parent`, #3588) rather
  than raising. The dispatch/audit boundary
  (`operations/dispatcher.py::_composite_terminal_error_code`, scoped to
  `source_kind == "composite"`) maps any such envelope to a first-class
  dispatch error: `OperationResult.status="error"`, `extras.error_code` =
  the envelope's `status` sentinel, a synthetic 500, and a
  `result_status="error"` audit row (the redacted envelope is preserved
  in `result`, so a parked-then-approved run's decision result keeps the
  `issues` detail). The envelope goes through the same result reducer as
  a success payload (`_reduce_and_audit_composite_error`, #4223): a small
  one passes through unchanged, an oversized one (a refusal naming
  hundreds of candidate ids) comes back as the bounded summary with the
  full envelope behind a result handle, while `error` and
  `extras.error_code` are read from the full envelope and the audit row
  still reads `error`. **The signal is the `error`-severity issue, not the
  `status` sentinel** — the sentinel space is unbounded and mixes hard
  failures with legitimate soft refusals / idempotency guards
  (`rule_exists`, `not_powered_off`, `no_change_requested`, `ambiguous`,
  …) that must stay `ok`, so a name-based partition would misfire;
  success / warning envelopes therefore carry only `warning` / `info`
  issues (a power-on that fails after a successful deploy is a `warning`
  on the `deployed` envelope). The corollary is a **convention every
  composite follows**: a hard failure (the change did not complete,
  rolled back, timed out, or only partly applied) must attach an
  error-severity issue, or it would still audit `ok`. Since #3813 the
  write composites listed there do this, using the one shared builder
  `connectors/vmware_rest/composite_issues.py::issue`: `vm.create`
  `rolled_back`; the vim task `timeout` of `vm.destroy`, `vm.disk.grow`,
  `vm.snapshot.revert`, `vm.snapshot.create`, `host.detach_from_vds`, the three
  `network.portgroup.*` writes and `cluster.drs_rule.create`;
  `vm.resize` / `host.disk_mark_flash` / `host.evacuate` `partial`;
  `host.evacuate` `aborted`; `host.detach_from_vds` `incomplete`;
  `vm.power` `error` / `tools_unavailable`; `vm.power.bulk` when any VM
  failed (no `status` key, so `error_code` is `composite_error`);
  `vm.guest.program.run` `timeout` / `exit_unknown`;
  `storage_policy.delete` `delete_failed`; and `config_manager_unreadable`
  for the three host config-manager composites. Their response schemas
  document the `issues` field. Soft refusals and idempotency guards keep
  no error issue and stay `ok`. The regression tests are
  `tests/test_connectors_vmware_rest_composites_hard_failure_e2e.py`.
  The error path runs the same JSONFlux reducer as the success path
  (`dispatcher._reduce_and_audit_composite_error`, #4223). An envelope within the
  inline budget comes back whole. A larger one (a failed `vm.power.bulk`
  over many VMs, or a failed deploy with many or long issues) returns a
  result handle plus the failure fields inline: `status` and `summary`
  as they are, and a bounded view of `issues` (at most 5 entries, the
  `error` entries first, every string value cut at 256 bytes of JSON text
  and the list kept under 2 KB of JSON text, measured the way the
  transport sends it, so non-ASCII text cannot grow it) with
  `issues_total`. So the caller sees why the call failed without a
  drill-in, and the inline result stays small. The full issues list is in
  the stored result (the handle's rows when `issues` is the envelope's
  only list, or the whole document otherwise). The audit row is
  unchanged: `error`, with the error code and the full raw payload.

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
  (returns the new Datastore MoRef directly, no poll) — and is **idempotent**:
  it reads the host's mounted datastores first
  (`composites/_host_nfs_mount.py`), so a re-mount of an already-mounted export
  (matched on `remoteHost` + `remotePath`) returns `already_mounted` with the
  existing moid and no write, while a `datastore_name` taken by a different
  export returns the structured `name_conflict` (never a raw vim
  `DuplicateName`); `disk_mark_flash`
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
- **Supervisor (WCP) namespace-management composites** (`#3281`, group
  `namespace_management`, `composites/_supervisor.py`) — the governed
  replacement for out-of-band `govc` / `kubectl-vsphere` Supervisor
  enablement. Two `dangerous` / approval-gated writes,
  `supervisor_enable_composite`
  (`POST:/vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster`,
  the current 9.x path — the `clusters/{cluster}?action=enable` form is
  deprecated as of vSphere 9.0) and `supervisor_disable_composite`
  (`POST:/vcenter/namespace-management/clusters/{cluster}?action=disable`),
  plus one `safe` read, `supervisor_status_composite`
  (`GET:/vcenter/namespace-management/clusters/{cluster}`). See the
  **Supervisor (WCP) namespace-management composites** subsection under
  Control flow.
- **Content-library SUBSCRIBED composites** (`composites/_library.py`,
  `#3495`, group `content_library`) — four
  `vmware.composite.content_library.subscribed.*` composites that create and
  drive a **SUBSCRIBED** content library through the backplane: the governed
  source for a vSphere Supervisor's Tanzu Kubernetes release (TKr / VKr)
  images. On Supervisor enable, `wcpsvc` otherwise auto-creates a subscribed
  library pointed at the fleet offline-depot content-gateway, which cannot
  serve the `VKR` component on a VCF-Installer fleet → activation hangs
  `CONFIGURING`; the governed path is to **pre-create** a subscribed library
  subscribed straight to the upstream VMware repo
  (`https://wp-content.vmware.com/v2/latest/lib.json`) and assign it as the
  Supervisor's TKr library via `#3281`'s enable spec
  `default_kubernetes_service_content_library` field. Two writes —
  `subscribed.create` (`POST:/content/subscribed-library`, resolves the
  `datastore` name to a `DATASTORE` storage backing, returns the new library
  id) and `subscribed.sync`
  (`POST:/content/subscribed-library/{libraryId}?action=sync`, respects
  `on_demand`, asynchronous) — registered **`caution` + `requires_approval=True`**
  (the issue's "write" tier: a write that always needs an approval decision,
  but not the `dangerous` intrinsic-risk of a VM/host mutation, and the first
  `caution` composites on this connector). Two reads — `subscribed.status`
  (`GET:/content/subscribed-library/{libraryId}`, surfaces `last_sync_time`,
  the readiness signal) and `subscribed.items.list`
  (item ids via `POST:/content/library/item?action=find`, then per-item
  `GET:/content/library/item/{libraryItemId}`, JSONFlux-reduced) — registered
  `safe`. Both writes ride the same `_write._write_sub_op` governance seam the
  other write composites use (`dangerous` / `requires_approval=False` sub-op
  posture) and both reads use `_write._read_sub_op` /
  `_find_content_library_ids` (un-gated). **Delivery shape — thin typed
  composites, not the generic-ingested row:** the subscribe body carries a
  `subscription_info.password` (BASIC auth) needing credential hygiene the raw
  ingested row cannot provide (broadcast aggregate-only via the
  `_CREDENTIAL_WRITE_OPS` pin on `subscribed.create`, a park-time preview that
  echoes identity fields only — never the password / username, `params_hash`-only
  audit), and the connector's governed ops must dispatch on a fresh boot with
  zero catalog ingest (a generic row requires a runtime spec-ingest +
  `edit_op` enable). The naming nests under the sibling `#3331` LOCAL-library
  family (`content_library.create`) via the `subscribed` infix, so the two do
  not collide. `_SUB_OPS_*` manifests are reconciled against the pinned
  `vcenter.yaml` by `test_connectors_vmware_rest_library_reconcile.py`.
- **`register_vmware_composite_operations`** (`composites/_register.py`)
  — async registrar function called from `run_typed_op_registrars` at
  lifespan startup. Iterates a single `_COMPOSITES` tuple of 73
  `_CompositeSpec` rows (16 read — incl. the `#3789` datastore
  cache-refresh read `datastore.refresh` — + 45 dangerous/destructive
  writes + 12 caution writes); each row carries its
  own `safety_level` + `requires_approval` so the policy posture is
  implied by the spec, not by global defaults. The AI guidance
  (`llm_instructions`) of the VM and folder writes and the VM allocation
  reads lives in `composites/_vm_llm_instructions.py`, one entry per op id;
  their example calls sit under `examples` in each parameter schema. The derived registration-coverage guard uses explicit, closed exceptions only for write operations whose existing semantics intentionally omit a preview or governed-suboperation discovery entry; every other registry id must be represented. `test_reference_docs_drift.py` remains the single total-set drift gate for generated `docs-site/reference/connectors.md`; regenerate it with `cd backend && uv run python scripts/generate_reference_docs.py` when the registry changes. Idempotent on re-run via the body-hash skip path.
- **Typed ops** (`typed_ops.py`, `#2257`) — the first vmware
  `source_kind="typed"` op, `vmware.host.usage`. Unlike a composite, a
  typed op is a **bound method** on `VmwareRestConnector`
  (`host_usage(self, operator, target, params)`) that the dispatcher
  binds to the resolved connector instance and calls directly — no
  `dispatch_child`, no ingested-descriptor sub-ops, no L2 pre-flight. It
  therefore works on a **fresh boot with zero catalog ingest** — the same
  direct-session property the read composites (`#2253`) and, since
  `#2256`, the write composites now share. The only `dispatch_child`
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
  `vmware.host.network_uplinks`); the host listing is load-bearing.
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
    `GET /vcenter/vm?filter.names=`). An unknown name — the listing
    answers `200 []` — raises `ConnectorResourceNotFoundError` with the
    name as the resource id, so the dispatcher returns
    `status="not_found"` with `extras.error_code="not_found"` and
    `extras.resource_id=<name>` (#3653). Like every dispatcher result,
    `POST /api/v1/operations/call` delivers this envelope on HTTP 200;
    callers check `status`, not the HTTP code, to learn that the VM does
    not exist. (The 404 from `status_code_for_result` is only the
    synthetic code written to the audit row.) An ambiguous name still
    raises a plain error. One PropertyCollector read of the
    `VirtualMachine`'s
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
    [path]}`, surfacing the `ObjectContent.missingSet`. It additionally
    surfaces the per-property `missingSet` fault **type names** —
    `missing_properties: [{path, fault_type?}]` plus a
    `missing_fault_summary: {fault_type: count}` roll-up (concrete vim
    fault class names only, e.g. `NoPermission` / `InvalidProperty`,
    **never** fault text, which can echo a property value) — so an
    operator can tell a genuinely-unset property from a per-property
    permission/auth/invalid fault (#3708). The op passes paths through
    as given. Its description and `properties` hint tell callers that
    vCenter 8.0.x rejects a path into a cluster's `configurationEx`
    (`configurationEx.rule` / `.group` / `.drsConfig`), so they request
    `configurationEx` and pick the field from the result (#3617).
    **Use it for distributed switch and distributed portgroup config
    reads** (type `DistributedVirtualPortgroup` /
    `VmwareDistributedVirtualSwitch`, for example the VLAN and security
    policy under `config.defaultPortConfig`). Read polymorphic fields
    whole: `config.defaultPortConfig` is declared as the base
    `DVPortSetting`, so a path into a subclass-only field
    (`config.defaultPortConfig.vlan`) fails with `InvalidProperty`. The
    hint in `parameter_hints.properties` says this (#3534).
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
  - **`vmware.tasks.history`** (`typed_ops_tasks_history.py`, handler
    `tasks_history`, `#3701`) — vCenter tasks in a past time window.
    Use it when a task has already left the recent list.
    - **Recent vs history.** `tasks.recent` reads
      `TaskManager.recentTask`, which keeps only the last few minutes.
      Classic vim tasks (an OVF deploy from a content library, a clone, a
      relocate) are not CIS task providers, so `/cis/tasks` cannot see
      them. `tasks.history` is the read for those older tasks.
    - **One dispatch, no lasting server state.** The op calls
      `TaskManager.CreateCollectorForTasks` with a `TaskFilterSpec`, pages
      the new `TaskHistoryCollector` with `ReadNextTasks` (100 rows per
      page, oldest first), and calls `DestroyCollector` on the success
      path and on the error path. The op returns only when the collector
      is confirmed gone.
    - **Every call on the collector goes to the session that created
      it.** A collector lives in one vCenter session. The create call
      records the session token it was sent with in a `SessionTokenUsed`
      (`session.py`): the connector pins that token on the request, so it
      is the token on the wire, not the cached token before or after the
      call. The reads and the destroys are then sent with that token
      (`_post_vmomi_json(..., session_token=...)`), even when the
      connector has logged in again in between (the session max-age ran
      out, a `NotAuthenticated` fault on another call forced a re-login,
      or another request replaced the session while the create was in
      flight). A `NotAuthenticated` fault on such a pinned call is not
      retried on a new login: that session is gone, and a new one would
      not hold the collector.
    - **One cleanup rule.** The collector counts as gone only when the
      owning session accepts `DestroyCollector`, or answers
      `ManagedObjectNotFound` (already gone) or `NotAuthenticated` (the
      session is gone, and the collector with it). A failed destroy is
      retried (3 tries in all, logged as
      `vmware_tasks_history_destroy_failed`). In every other case the op
      closes the owning session (`VmwareRestConnector.close_session` with
      the recorded token: `DELETE` at the session path with that token),
      because a collector dies with its session
      (`vmware_tasks_history_session_closed`). That covers a destroy that
      keeps failing, a timeout or a lost answer, and a create request
      that was sent but returned no collector reference (a timeout, a
      lost answer, a fault, an empty answer;
      `vmware_tasks_history_create_unconfirmed`): vCenter may have
      created the collector before the answer was lost. When the closed
      session is still the cached one, the cache entry is evicted too, so
      the next call on the target logs in again on a clean session and
      leaked collectors cannot pile up. When the connector has already
      logged in again, only the old session is closed and the new one
      stays. Only a 2xx answer to that `DELETE` counts as closed; an
      error answer, a transport error, or a redirect the shared client
      did not follow counts as a failed close. When the session close
      fails too, the call fails with
      `CollectorCleanupError` (`vmware_tasks_history_collector_leaked`);
      vCenter then drops the collector when that session times out. A
      read error (or a create error) always wins over a cleanup error, so
      cleanup never hides why the call failed.
    - **Cancel-safe.** The cleanup also runs when the call is cancelled
      (for example by a timeout) while a page is being read. A cancel
      while the create request is pending lets that request finish,
      because vCenter may have created the collector already; the op then
      removes it (`vmware_tasks_history_cancelled_during_create`), or
      closes the session when the request failed. A cancel that arrives
      during the cleanup waits for the cleanup to finish and is raised
      again afterwards. So no collector survives the call.
    - **Params.** `since` (required) and `until` filter on the task's
      queued time (ISO-8601; a time without a zone is UTC). `entity` +
      `entity_type` filter on the object the task ran on;
      `include_children` widens that to child objects. `description_ids`
      keeps only some operations (for example `ResourcePool.ImportVApp`).
      `TaskFilterSpec` has no operation filter, so MEHO applies this one
      after the read. `max_tasks` caps the rows (default 200, at most
      1,000).
    - **Output.** `{tasks, truncated, scanned, collector_cleanup}`.
      `collector_cleanup` is `destroyed` (the owning session confirmed
      the collector gone) or `session_closed` (the session close above
      ran). Rows have the same
      shape as `tasks.recent` (one shared parser).
      A long list goes through the normal JSONFlux result handle.
    - **Limits.** vCenter caps how many collectors one session may hold
      (create fails with `InvalidState` above the cap) and keeps task
      history only as long as its own settings allow. A wide window can
      hit `max_tasks` or the 50-page scan budget; then `truncated` is
      `true` and the caller should narrow the window. vCenter only: a
      standalone ESXi host does not serve the collector.
    - **Safety level `safe`.** The collector is a temporary,
      session-scoped read cursor that the op removes before it returns. It
      changes no inventory or configuration, and vCenter needs only the
      `System.View` privilege to create it.
- **`vmware.vm.question.get`** and **`vmware.vm.question.answer`**
  (`typed_ops_vm_question.py`, handlers `vm_question_get` /
  `vm_question_answer`; the shared read is in `vm_wait_state.py`) — read
  and answer the question a VM waits on.
  vCenter can stop a VM change and wait until someone answers a question
  in the VM; until then the change runs on, and a second try fails with
  "Another task is already in progress".
  - **Read** (`safe`). By `vm` moid or `name`. One PropertyCollector read
    of `name`, `runtime.question` and `recentTask` on the VM, then one
    `Task.info` read of those tasks (only when there are any). Returns
    `{vm, name, pending, question: {id, text, choices: [{key, label,
    summary}], default_choice, messages} | null, running_tasks: [{task,
    operation, state, progress, start_time}], tasks_error}`;
    `running_tasks` keeps only `queued` and `running` tasks.
    - The question read must succeed. When vCenter could not read
      `runtime.question` (a `missingSet` fault, for example no permission),
      the read raises and names the fault type, so a read failure never
      looks like "no question".
    - The task read is best effort. When `recentTask` was unreadable or the
      `Task.info` read fails, `running_tasks` is empty and `tasks_error`
      says why; the question is still returned.
    - When a long `running_tasks` list makes JSONFlux reduce the result to
      a handle, the `result_objects` hint keeps the `question` (`id`,
      `text`, `choices`, `default_choice`, `messages`) inline, so the caller
      still has what it needs to answer. The inline copy is bounded: a
      question with many long choices is cut, `result_object_truncations`
      names the cut field, and `result_object_note` says how to get the
      whole question. The reducer stores it complete with the rows, so
      `result_query` with `path="/question"` returns every choice.
    - When vCenter answers the `Task.info` read but could not read the
      `info` of one task (a `missingSet` fault), or left a task out, that
      task may still run: `running_tasks` is empty and `tasks_error` names
      the fault, so an unreadable task never looks like "no running task".
  - **Answer** (`dangerous`, waits for approval). Params `vm` (moid only,
    checked against `[A-Za-z0-9-]+` because it goes into the URL path, and
    percent-encoded), `question_id` and `answer` (a choice key). It reads
    the question again first: a different question id or no question
    returns `question_gone`, a key that is not a choice returns
    `invalid_answer`, and nothing is sent in both cases. Then it POSTs
    `VirtualMachine.AnswerVM` (`{questionId, answerChoice}`) on the VI-JSON
    base `/sdk/vim25/{release}` through `_post_vmomi_json` (the ingested
    `/api/VirtualMachine/...` form 404s on vCenter 9.x). A
    `ConcurrentAccess` or `InvalidArgument` fault means someone closed the
    question in the meantime and also returns `question_gone`. A question
    vCenter could not read raises before anything is sent; a failed task
    read does not stop the answer.
  - **Approval preview.** A preview builder shows the approver the
    question text, the label of the chosen answer, whether the question
    is still open, the running tasks and `tasks_error`.
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
- **`vmware.snapshot.list`** (`typed_ops_snapshot_list.py`, handler
  `snapshot_list`) — the snapshots of one VM (by `vm` moid) as a flat list in
  tree order. `safe`, in its own group `vmware-snapshot-list`, on both
  catalogs. See **VM snapshot operations** under Control flow.
- **`vcenter.sso_admin.api_file.get`** (`typed_ops_sso_admin_api_file.py`,
  handler `sso_admin_api_file_get`) — reads one SSO Admin API description
  file (the WSDL or one of its XSD files) that a vCenter serves. No vendor
  download has the SSO Admin WSDL, so a running vCenter is the only place to
  get it. `safe`, read-only, in its own group `vcenter-sso-admin-api-files`.
  Registered through `VMWARE_TYPED_OPS`, so it is on both catalogs.

  | Versions | Catalog |
  |---|---|
  | vCenter 8.0 U1, 8.0 U2, 8.0 U3 | `vmware-rest-8.0` |
  | vCenter 9.0, 9.1, 9.1.1 | `vmware-rest-9.0` |

  - **Which paths it reads.** Only a path under `/sso-adminserver/` (the SSO
    Admin service folder; the documented service address is
    `/sso-adminserver/sdk/{domain}`). Allowed characters are letters,
    digits and `. _ ~ / -`; no `..` or `//`; the query is empty or one of
    `wsdl`, `xsd`, `xsd=<number>`. So a backslash, a `%` (an encoded dot or
    slash), a scheme or a host is refused. The same rule is the `pattern` of
    the `path` parameter, so the dispatcher refuses a bad path as
    `invalid_params`, and the handler checks it again. A refused path sends
    no request. Every SSO Admin call is a SOAP `POST`, so a `GET` in that
    folder changes nothing. The exact file path of the WSDL is not documented
    by the vendor yet; the read does not need it, and the lab pins it with
    this read.
  - **How it reads.** One `GET` on a new client from
    `_ephemeral_http_client` (same SSRF and TLS settings, its own empty
    cookie jar). No login header and no session cookie go out: the SSO Admin
    service has its own login (a SAML token), and a WSDL holds no data. The
    request goes through `httpx.AsyncClient.send(..., follow_redirects=False)`,
    so a redirect is not followed (the pooled client class would follow a
    same-origin redirect by itself). A `3xx` returns `redirect_path`; a
    `401`, `403`, `404` or `5xx` returns its `status` with no text.
  - **What it returns.** `path`, `status`, `content_type`, `redirect_path`,
    `size` (bytes of the checked copy), `sha256` (of the checked copy),
    `secrets_replaced`, `imports`, `parts`, `text_parts`, `version`,
    `build`, `version_error`. `imports` lists the
    `location` / `schemaLocation` values of `wsdl:import`, `xsd:import` and
    `xsd:include`, as written, parsed with `defusedxml`; a file that is not
    XML gives `[]`. Only UTF-8 text comes back; other content is refused and
    the error names its content type.
  - **Version and build** come from `GET /api/appliance/system/version` on
    the connector's normal login. If that read fails, the file still comes
    back, `version` and `build` are `null`, and `version_error` says why.
    This covers an HTTP error, a vCenter login error, and the credential
    failures that happen before any request goes out: no credentials for
    the target, access denied, or a credential store that cannot be reached
    (`CredentialsReadError`, `VaultClientError`).
  - **The file is checked before it is cut.** The read runs the shared
    API file secret check (`connectors/_shared/api_file_secrets.py`, see
    `connectors-shared-api-file-secrets.md`) on the whole file: the file
    is parsed (XML for a WSDL or XSD file), every value under a secret
    name is replaced with `[REDACTED:param_name]`, and the file is
    written back from the parsed copy with its namespace prefixes as
    declared. A file that does not parse is refused with the reason.
    `secrets_replaced` counts the replaced values; `size` and `sha256`
    describe the checked copy. A redirect path gets the same check on its
    query string.
  - **How to rebuild the file.** `text_parts` is a list of
    `{index, text}`, each at most 64 KiB of UTF-8, cut only between
    characters. Join the `text` of all parts in `index` order and encode as
    UTF-8; the SHA-256 of the result equals `sha256` and the result is the
    checked copy. A part is a fixed byte
    size, not a line, because a served file can be one long line. The answer
    has two lists (`imports` and `text_parts`), so JSONFlux keeps a large
    answer whole as a document record behind a result handle; the inline
    summary keeps the scalar fields (`sha256`, `size`, `parts`, `build`, ...).
    Read the parts with `result_query` and `query={"path": "/text_parts"}`,
    paging with a small `limit` (one answer is at most 256 KiB), and the
    imports with `path="/imports"`. A file that does not fit in one stored
    result (`result_handle_max_record_bytes`, 16 MiB by default, measured as
    stored JSON) stops with an error that names its size; the read never
    returns a cut file.
  - **Redaction keeps the copy whole.** MEHO redacts secret-shaped text in
    every stored result (the dispatcher runs
    `apply_connector_boundary_redaction` before the JSONFlux reducer). A rule
    that fires on the text would rewrite a part, and the joined parts
    would no longer match `sha256`. So the read runs the same middleware, with
    the same connector, tenant and operation labels, on the text parts and on
    the whole copy before it returns. The whole-file pass matters: a
    secret that the part cut splits in two matches in no single part, so the
    dispatcher's own redaction would store it in clear text. When the text
    would change, the read stops with an error that names the rule and the
    place (a part, or `file_text` for the whole file), and returns no file.
    Redaction itself stays on; its `api_key` rule skips the placeholder the
    file check writes. The default policy's `strip-api-key` rule fires
    on text such as
    `type="token:SecurityToken"`; a redaction policy registered for this
    operation changes what the check accepts, because both use the same
    policy lookup.
  - **Tests.** `tests/test_connectors_vmware_rest_typed_sso_admin_api_file.py`
    (respx: accepted and refused paths, no login on the `GET`, status and
    redirect cases, large files, version failure including missing
    credentials and a failed credential-store login, a file that a redaction
    rule would change, a secret split across the part cut, and the
    result-handle round trip through redaction,
    storage and read-back) and the reconcile lane
    `tests/test_connectors_vmware_rest_sso_admin_api_file_reconcile.py`
    (the SSO Admin folder is a documentation path that the pinned 8.0 and
    9.0 `vcenter.yaml` do not serve; both serve
    `GET /appliance/system/version`).
- **vSphere licences** (`license_common.py`, `typed_ops_license.py`,
  `composites/_license.py`) — two typed reads and one write, on both
  catalogs. See **vSphere licences** under Control flow.
  - **`vmware.license.list`** (handler `license_list`, `safe`) — the
    licences in the vCenter's licence inventory.
  - **`vmware.license.assigned.list`** (handler `license_assigned_list`,
    `safe`) — which licence each asset uses.
  - **`vmware.composite.license.assign`** (`dangerous`,
    `requires_approval=True`) — assign a licence to a host or to the
    vCenter.

  All three share the group `vmware-licensing`. Its text is one constant,
  `license_common.LICENSE_WHEN_TO_USE`, that both registrars pass, so the
  group row reads the same whichever registrar creates it first.
- **`vmware.appliance.local_account.*`** (metadata in
  `typed_ops_appliance_local_accounts.py`, handlers in
  `appliance_local_accounts.py`, password handling in
  `appliance_local_account_password.py`) — the local accounts of the vCenter
  appliance: its own operating-system accounts, such as `root`. `root` is the
  login for the appliance console, SSH and the management page on port 5480.
  When its password expires, these logins stop working until someone sets a
  new one. Four typed operations in the group
  `vmware-appliance-local-accounts`, registered through `VMWARE_TYPED_OPS`,
  so they are on both catalogs.

  | Operation | What it does | Vendor call | Risk |
  |---|---|---|---|
  | `vmware.appliance.local_account.list` | Every local account, one row each. | `GET /api/appliance/local-accounts`, then `GET /api/appliance/local-accounts/{username}` per account | `safe` |
  | `vmware.appliance.local_account.get` | One account. A missing account answers `not_found`. | `GET /api/appliance/local-accounts/{username}` | `safe` |
  | `vmware.appliance.local_account.password.set` | Sets a new password that MEHO reads from a Vault path. | `PATCH /api/appliance/local-accounts/{username}` with `password` | `dangerous`, `requires_approval=True` |
  | `vmware.appliance.local_account.policy.set` | Changes when the password expires and when the account locks. Only the fields the call passes change. | the same `PATCH` with `password_expires`, `max_days_between_password_change`, `min_days_between_password_change`, `warn_days_before_password_expiration`, `inactive_after_password_expiration`, `days_after_password_expiration` | `dangerous`, `requires_approval=True` |

  | Versions | Catalog |
  |---|---|
  | vCenter 8.0 U1, 8.0 U2, 8.0 U3 | `vmware-rest-8.0` |
  | vCenter 9.0, 9.1, 9.1.1 | `vmware-rest-9.0` |

  The vendor API exists since vCenter 6.7. A vCenter 7.0 target resolves to
  `vmware-rest-9.0`, and typed operations run there, but 7.0 is not proven.
  The `PATCH` body is the flat `UpdateConfig` that `/api` takes; a vCenter
  that serves only the old `/rest` API (before 7.0 U2) refuses it.

  - **One account row** (list and get): `username`, `roles`, `enabled`,
    `has_password`, `last_password_change`, `password_expires_at`,
    `password_expired`, `days_until_password_expires`,
    `max_days_between_password_change`, `min_days_between_password_change`,
    `warn_days_before_password_expiration`, `inactive_at`. Times are ISO 8601,
    as vCenter sends them. No expiry date means the password never expires
    (`password_expired: false`). MEHO works out `password_expired` and the
    whole days left from the expiry date. vCenter reports when the account
    locks (`inactive_at`), not a number of days. A list row whose account read
    failed has null fields and a `read_note` (vCenter can answer HTTP 500
    instead of 403 for an account the caller may not read). No field holds a
    password: vCenter sends only `has_password`.
  - **The policy change** reads the account first (a missing account stops the
    call before any change), sends only the given fields, and reads the
    account again. It returns `requested`, `before` and `after`. The day
    fields take -1 to 99999, the range vCenter documents for its password
    policy. To stop the password from expiring, set `password_expires: false`,
    or set a large `max_days_between_password_change` such as 9999.
  - **How the new password stays secret.**
    - The call never carries the password. It names a Vault path
      (`password_secret_ref`, with `password_secret_mount`, default `secret`,
      and `password_secret_key`, default `password`). The handler reads the
      password with `load_param_secret_data` under the identity of the person
      whose call runs. The Keycloak and VCF Automation user writes use the
      same seam. A refused or missing path answers
      `vault_credentials_read_error`; a secret with no text under the key stops
      the call. In both cases nothing goes to vCenter.
    - After an approval, MEHO runs the change under the approver's login, so
      the approver needs read access to the Vault path.
    - The op id is pinned as `credential_write` in `broadcast/events.py`. So
      the flight recorder keeps no request or response body for the call (its
      `*password*` op-id pattern and the `password` tag do the same), the
      broadcast shows no params, and a waiting approval keeps the params in
      the encrypted handoff store instead of `approval_request.params`. That
      store needs `APPROVAL_HANDOFF_ENCRYPTION_KEY` (chart: `approvalHandoff`).
      Without it, MEHO refuses the call instead of letting it wait. `GET
      /ready` shows the state under `features.approval_custody`.
    - Because of the pin, the generic params echo does not reach the approver.
      A preview builder shows the account, `password_source: vault` and the
      Vault path, never the password.
    - Every vCenter error answer to the two changes (also to the first read
      of the policy change) goes through one rule, `change_error`, before it
      leaves the handler. A 404 answers `not_found`. A 401 stays an
      `httpx.HTTPStatusError` with status 401, so the dispatcher still logs in
      again and retries once; but its response holds only the vAPI error type
      and messages, and its request has neither the session header nor the
      body. Any other status becomes `ConnectorUpstreamError` with the HTTP
      status and the same safe text (a 403 also names the SSO group the
      service account needs). When any vendor text holds the password (as is,
      JSON-escaped, URL-encoded or HTML-escaped), all vendor text is dropped.
      The new error is raised after the `except` block, so it does not keep
      the original error (raw response and request) as its context. A failed
      read after a change gives a `read_note` with the same safe text.
    - The result is `{username, password_set, password_expires_at,
      read_note}`.
  - **The service account of the target needs an appliance role.** The vendor
    maps three levels to SSO groups: `operator` and `admin` to
    `SystemConfiguration.Administrators`, and `superAdmin` to
    `SystemConfiguration.BashShellAdministrators`
    ([Authorization Model Mapping to the vCenter Single Sign-On Domain](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/introduction-to-the-vcf-programming-guide/introduction-to-the-vsphere-automation-rest-apis/vcenter-server-management/authorization-model-for-administration-of-vcenter-server/authorization-model-mapping-to-the-vcenter-single-sign-on-domain.html)).
    The vendor says the super administrator level "is required for adding or
    altering user accounts"
    ([Using the SuperAdmin Role](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/8-0/vmware-vsphere-automation-rest-programming-guide-8-0/vcenter-server-management-1/authorization-model-for-administration-of-vcenter-server-1/using-the-superadmin-role-1.html)).
    So both changes need the service account in
    `SystemConfiguration.BashShellAdministrators`. The vendor names no level
    for the reads; a service account in neither group gets HTTP 403 on the
    list. Members of `SystemConfiguration.BashShellAdministrators` can also
    open the appliance Bash shell with near-root rights, so keep the group
    small.
  - **Tests.** `tests/test_connectors_vmware_rest_typed_appliance_local_accounts.py`
    (rows and expiry, refused names and values, `not_found`, the Vault read,
    the error rule for every status, the approval preview, the
    flight-recorder span); `tests/test_connectors_vmware_rest_appliance_password_e2e.py`
    (one password change through the dispatcher, the approval queue and the
    real connector over respx: the password is in the `PATCH` body and not in
    the params, the approval request, the audit rows, the result, the
    flight-recorder spans or the logs; and two 401 answers that echo the
    password: the login retry runs once and nothing leaks); and the reconcile lane
    `tests/test_connectors_vmware_rest_appliance_local_accounts_reconcile.py`
    (the three paths in the pinned 8.0 and 9.0 `vcenter.yaml`, and the flat
    `UpdateConfig` body in the 9.0 file).
- **Alarms** (`alarm_state.py`, `typed_ops_alarm_triggered.py`,
  `composites/_alarm.py`, `#4383` part 1) — one read and two changes in the
  group `alarms`. The read `vmware.alarm.triggered.list` is a typed op
  (`safe`, handler `alarm_triggered_list`). The changes
  `vmware.composite.alarm.acknowledge` and `vmware.composite.alarm.reset` are
  composites (`caution`, they wait for approval). All three are on both
  catalogs. See **Alarms: list, acknowledge, reset to green** under Control
  flow.
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
2b. The same import also registers the **second versioned catalog**
   `register_connector_v2(product="vmware", version="8.0",
   impl_id="vmware-rest", cls=VmwareRest80Connector)` (#3569, dual-impl
   #3038) plus the shared product wildcard `(vmware, "", "")` owned by
   `VmwareRestConnector`. `VmwareRest80Connector` is a thin subclass of
   `VmwareRestConnector` (`version="8.0"`,
   `supported_version_range=">=8.0,<8.1"`, and the ingested-catalog guard
   boundary narrowed to the 8.0.x line via the
   `_catalog_version_floor`/`_catalog_version_ceiling`/`_catalog_version_band_label`
   attributes). The bands are disjoint (8.0 catalog `>=8.0,<8.1`; 9.0
   catalog `>=8.5,<10.0`), so a fingerprinted 8.0.x target resolves to
   `vmware-rest-8.0` (versioned beats the wildcard) and a 9.x target to
   `vmware-rest-9.0`. Ingested rows for the 8.0 U3 catalog land under
   `connector_id="vmware-rest-8.0"`; the shared `(product, impl_id)` safety
   floor covers them identically. See
   [vcf-api-compatibility.md](vcf-api-compatibility.md). Both shipped minimal
   specs (`vmware_rest_8_0_minimal.yaml` and `vmware_rest_minimal.yaml`) carry
   the `/api` mount on `servers[0].url` and key their inventory paths bare, so
   the ingested op_ids read `GET:/vcenter/vm` — byte-identical to the vendor
   `vcenter.yaml` catalog and to the spec-relative form `_mount.py` re-mounts
   onto `/api` (modern) or `/rest` (legacy) per target; only the `/api/about`
   fingerprint-probe path is keyed literally (`GET:/api/about`). The reconcile
   lane `test_connectors_vmware_rest_8_0_spec_reconcile.py` pins the two specs
   to an identical served-op_id set, so they are re-keyed in lock-step (#3614).
   **8.0 tasks-list body (#3700).** `POST /cis/tasks?action=list` (list
   vCenter tasks) is not in the shipped minimal; it comes with a full-surface
   re-ingest. The 8.0 spec gives it a flat body (`exclude_result` /
   `return_all` at the top), which vCenter 8.0.3 rejects with HTTP 400.
   `ingest_corrections.py` replaces the body at ingest time with the
   two-level shape the 9.0 spec already has:
   `{filter_spec: {services | tasks | operations | status | targets | users},
   result_spec: {return_all, exclude_result}}`. The corrected schema refuses
   the flat body and an empty filter before any request is sent: vCenter 8.0.3
   needs a non-empty `filter_spec.services` or `filter_spec.tasks` list. The
   row's `llm_instructions` says the same. The 9.0 catalog is left as
   ingested. The reconcile lane above pins the corrected body shape, against
   a fixture always and against the pinned vendor 8.0 U3 spec when the shelf
   has it.
   **8.0 VI-JSON calls (#4333).** The same lane collects every VI-JSON
   method the connector calls (from the source) and checks it against the
   vendor VI/JSON files of 8.0 U1, U2 and U3. A method a file lacks needs a
   version gate or an entry with the reason in the lane's exclusion list.
   The list is empty today. No 8.0 VI/JSON file has vSAN paths, so on
   vCenter 8.0 the vSAN methods go as SOAP (`#4441`, see "vSAN calls"
   below); the same lane checks them against the vendor's vSAN SDK of
   8.0 U1 and U2 instead. See
   [vcf-api-compatibility.md](vcf-api-compatibility.md).
   **Operation summaries (#4329).** Search and the operation-group step
   read each operation's one-line `summary`. The full vendor specs fall
   short in two ways, and `ingest_corrections.py` fixes both at ingest
   time, on every `vmware-rest` catalog version:
   - The vSphere REST spec (`vcenter.yaml`) has no `summary` on any of its
     operations (1,275 on 9.0). An operation with no summary gets the first
     sentence of its description, at most 120 characters. A longer first
     sentence is cut at a word and ends with `…`. Paragraphs that only hold
     vendor notes (`***Required privileges:*** ...`) are skipped; when
     nothing else is left the summary stays empty.
   - The VI-JSON spec (`vi-json.yaml`) repeats one summary for the same
     method on many managed object types (844 operations on 9.0). When two
     or more VI-JSON operations of one spec share a summary, each gets its
     object type in front, for example
     `VirtualMachine: Destroys this object, ...` and
     `Folder: Destroys this object, ...`. A VI-JSON path is
     `/<Type>/{moId}/...`, with an optional service segment in front
     (`/vsan/...`). A summary that no other operation shares is kept as it
     is, and so is any summary the spec already gives.

   The shared-summary fix compares operations with each other, so it is a
   batch correction (`register_ingest_batch_correction` in
   `ingest/spec_corrections.py`); the description fix is part of the
   per-operation correction. The shipped minimal specs already carry
   summaries, so a catalog ingest of them is unchanged.

   **Running installs** get the new summaries on their next ingest of the
   full vendor specs: run the same `--spec` ingest again. The summary is
   part of the row's body hash, so the changed rows are updated and
   embedded again for search; rows that did not change are skipped. The
   operator's own texts (`custom_description`, `custom_notes`) are not
   touched.
3. The same import triggers the side-effect import of
   `meho_backplane.connectors.vmware_rest.composites`, whose
   `__init__` calls
   `register_typed_op_registrar(register_vmware_composite_operations)`
   to queue the composite-row upsert onto the lifespan's registrar
   list. The package `__init__` then queues
   `register_typed_op_registrar(register_vmware_typed_operations)`
   (`#2257`) for the typed-op rows.
3b. **Composites and typed ops on the 8.0 catalog (#3789).** Two more
   registrars, `register_vmware_composite_operations_8_0` and
   `register_vmware_typed_operations_8_0`, write the same rows under
   `connector_id="vmware-rest-8.0"`. The resolver sends a fingerprinted
   8.0.x target to that catalog, so an agent that picks it finds
   `vm.disk.grow`, the guest ops, `object.collect` and the rest there. The
   rows use the same handlers and the same safety level and approval rule
   as on 9.0; the handlers already choose the right path per vCenter
   release. Notes:
   - The Supervisor and namespace composites are not on the 8.0 catalog.
     `COMPOSITES_NOT_ON_8_0` in `composites/_register.py` lists each
     excluded composite with its reason. The test
     `test_connectors_vmware_rest_catalog_parity.py` fails when a composite
     or typed op on 9.0 is neither on 8.0 nor in that list.
   - The governed child ops inside a composite keep their
     `vmware-rest-9.0` keys on both catalogs. One grant set covers the
     composite on either catalog, and approving a parked child op re-runs
     the composite on the 9.0 catalog, where it is also registered.
   - VI-JSON needs vCenter 8.0 U1 or later, so the VI-JSON composites do
     not work on an 8.0 GA target.
   - When the 9.0 catalog refuses an ingested op for an 8.0.x target
     (`unqualified_target_version`), the reason now names the catalog to
     use: `...; use connector vmware-rest-8.0 for this target`. The 8.0
     catalog does the same for a 9.x target.
4. The registry's v2 table now resolves `(vmware, 9.0, vmware-rest)`
   to `VmwareRestConnector`. The G0.7 auto-shim's idempotency check
   (in `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.
5. Lifespan calls `run_typed_op_registrars()`, which iterates every
   queued registrar and upserts: the 73 `vmware.composite.*` rows with
   `source_kind="composite"` — 16 reads (15 with `safety_level="safe"` +
   `requires_approval=False`, plus `vm.guest.file.read` at
   `safety_level="caution"` since `#3720`, which auto-parks for agent /
   service principals), and 57 writes, all `requires_approval=True`
   (34 `safety_level="dangerous"`; 12 `safety_level="caution"` —
   `alarm.acknowledge` + `alarm.reset` / `#4383`, `resource_pool.create` + `cluster.drs_vm_host_rule.create` / `#3505`,
   `namespace.create`, `storage_policy.create`,
   `content_library.subscribed.create` + `content_library.subscribed.sync`,
   `vm.resource_allocation.set` / `#3880`, `datastore.dir.create` / `#3339`,
   `vm.tools.installer.unmount`, `vm.snapshot.create`;
   and 11 `safety_level="destructive"` — `vm.destroy` / `#3198`,
   `namespace.delete`, `storage_policy.delete`, `cluster.drs_rule.delete`,
   `vm.snapshot.delete`, and the six `#3339` teardown deletes) —
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
and `about` (`version`, `apiType == "HostAgent"`, and `apiVersion` — cached
in `_esxi_api_versions`); (2) `SessionManager.Login`
on the ServiceContent SessionManager moid → a 200 sets the
`vmware_soap_session` cookie, which `httpx` keeps in the pooled client's
cookie jar. That cookie is the auth for every subsequent `/sdk` POST, so
`auth_headers()` adds **no header** for an ESXi session (the cached sentinel
token is the opaque cookie value, never the password).

*SOAPAction / vim API version (`#3363` State-2 fix).* An **empty**
`SOAPAction` header resolves a `/sdk` method against the host's *baseline*
schema — live-observed on a standalone ESXi 9.1 host as `vim.version.version3`
(`vim25/2.5u2`), which predates `RetrievePropertiesEx` (vim API 4.1),
`MarkAs*_Task` (5.x), and the datastore-mount write, so those posts fault
`InvalidRequest: Unable to resolve WSDL method name … in vim.version.version3`.
The bootstrap `RetrieveServiceContent` + `Login` resolve on the baseline
(they exist there, and the version is not yet known), so they post an empty
action; every op past the bootstrap carries
`SOAPAction: urn:vim25/<about.apiVersion>` (`soap.soap_action_for_version`),
binding it to the host's own schema (`9.1.0.0`).

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

*vim method map (all on `POST /sdk`, SOAP 1.1, `urn:vim25`).* Ten methods
total — three session/bootstrap + seven op methods, each with a builder +
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
| datastore.refresh (`#3789`) | `Datastore.RefreshDatastore` / `RefreshDatastoreStorageInfo` | Datastore moid (`server:/export` on ESXi) | void (204 / empty) → summary read back via `RetrievePropertiesEx` |

*Native primitive typing (the codec crux).* An **explicit** `xsi:type` is
authoritative (`xsd:boolean` → `bool`, integer → `int`, float → `float`, any
other annotated leaf → its raw text). For a **bare** leaf — the ESXi norm,
which sends primitives with no `xsi:type` — only `"true"` / `"false"` is
coerced, to `bool`: the real `scsiLun` shape carries a bare
`<ssd>true</ssd>` / `<localDisk>true</localDisk>` that a schema-less walker
would leave as the string `"true"` and the downstream `_bool_or_none` would
silently null (the exact `#3332` corruption class — proven `ssd is True`
through `_map_scsi_lun`). A bare **numeric** leaf is deliberately left as
text: vim25 carries string-typed keys whose values read as integers
(`HostBootDeviceInfo.currentBootDeviceKey == "8"`), and value-based `int`
coercion broke the consumer's `isinstance(str)` guard live (`#3363`
State-2 — boot resolution misreported "no currentBootDeviceKey"); the
genuinely-integer consumers (`HostScsiDisk.capacity` `block` / `blockSize`)
normalise a numeric string through `_int_or_none` already, so `capacity_bytes`
lands as `int` either way and output parity holds.

*Credential posture.* Credentials never appear in logs, errors, results, or
the flight-recorder vendor-call span (`#3214`): the Login envelope is the
only place the password lives (XML-escaped) and it is never logged nor handed
to the span — `_soap_post` records the `#3214` span with **no request body**,
so the `/sdk` envelope cannot leak into a captured span. A rejected credential
(`InvalidLogin` / `NoPermission` fault) → `ConnectorAuthError` (the SOAP
analogue of a vCenter 401/403, same restage remediation + the dispatcher's
one cold re-login); any other fault → `RuntimeError` naming only the target.

*Readiness.* **State 2 (live-proven).** The full surface was exercised
against a standalone ESXi 9.1 host in a lab: probe → `product=esxi` /
`version 9.1.0`; `storage_devices` → the host's SCSI LUNs with `ssd`/`local`
as Python `bool` and integer capacities; `datastore_mount_nfs` →
`CreateNasDatastore` returned a usable Datastore MoRef, confirmed by a
follow-up `summary.name` read (then removed via `RemoveDatastore` to leave
host state unchanged); `disk_mark_flash` → `MarkAsSsd_Task` polled to
terminal and read back `ssd is True`, reverted via `MarkAsNonSsd_Task`. The
run surfaced and fixed two live-only defects (the `SOAPAction` baseline-schema
fault above; the bare-numeric `currentBootDeviceKey` mis-coercion), and its
captured, scrubbed envelopes are committed as respx fixtures
(`backend/tests/fixtures/vmware_esxi_soap/`) wired into the codec + session
suites — so the units now replay real hardware bytes, the trap that let the
disproven VI-JSON premise ship green under mocks.

**Expired-session recovery (`#3773`).** A vSphere/ESXi SOAP session that has
expired server-side but is still cached process-side keeps answering the
unauthenticated `RetrieveServiceContent` on the dead cookie, yet
`RetrievePropertiesEx` comes back **HTTP 200 with every requested property in
`missingSet`, each carrying a per-property `NotAuthenticated` fault** — no
top-level SOAP `<Fault>`, so the connector's existing re-login (which fires on a
top-level auth fault) never triggers and `object.collect` returns a silent
all-missing "void" for the rest of the pod lifetime. Two mechanisms recover it:

- *(b) All-missing auth health-reset (load-bearing).* When an object resolves
  but **every** requested property is missing under an auth-class fault
  (`NotAuthenticated`, or an all-object `NoPermission` — never a *partial*
  per-field `NoPermission`, which is a real permission outcome),
  `_collect_with_auth_health_reset` invalidates the cached session (the same
  `invalidate_session` recovery hook the dispatcher's `#2067` mid-session-401
  path uses → evict → the next read cold-re-logs-in) and re-reads **once** on
  the fresh session. At most one reset per dispatch — structurally, by
  re-reading exactly once; a still-all-missing retry returns the fault-annotated
  envelope normally, never a loop. Recovers on the *symptom*, independent of
  *why* the session died (idle expiry, `hostd` restart, …), and reads both the
  9.x nested `LocalizedMethodFault` and the 8.0.x flattened fault shapes via
  `fault_type_name` (a 9.x wrapper with no usable nested class name gives no
  `fault_type` at all — the wrapper name is never reported as the fault class).
  *Known tradeoff:* because an all-object `NoPermission` counts as auth-class, a
  target whose principal really lost all permissions pays one extra login per
  read, and that read evicts the shared per-target session. This is accepted on
  purpose: the reset still happens at most once per read (a unit test pins
  this), and the same symptom is how a stale session shows itself.
- *Recovery markers (`#3777`).* Each (b) reset logs a
  `vmware_object_collect_auth_reset` event (target name + fault class names,
  never fault text), and `object.collect` adds `session_health_reset: 1` to its
  result (not in `required`, absent on a normal read; the result also lands in
  the audit row). When the probe liveness read below had to log in again, the
  fingerprint `extras` carry `session_health_reset: 1` and a
  `vmware_esxi_probe_session_relogin` event logs the target, the fault class
  names and whether the session came back (`live`). This holds even when the
  retried read raises (re-login refused, transport error): the helper reports
  the reset to the probe before the retry, so the unreachable fingerprint still
  carries the marker and the event says `live=False`.
- *(a) Bounded session max-age (defense in depth).* A warm-cached session older
  than `VMWARE_SOAP_SESSION_MAX_AGE_SECONDS` (**default `1200` s / 20 min**) is
  proactively invalidated + re-established **before** its next read, so a
  server-side-expired-but-locally-cached session goes undetected for at most one
  max-age window even if (b) is somehow bypassed. The check lives in the
  **shared** `_session_token` path, so it applies to **every** target this
  connector caches a session for — vCenter as well as standalone ESXi — not
  only to standalone ESXi hosts, despite the `SOAP` in the variable name. A
  proactive cold re-login is safe on both. The default sits **safely
  below** a standalone ESXi host's default SOAP idle timeout
  (`Config.HostAgent.vmacore.soap.sessionTimeout` = `1800` s / 30 min) — the
  max-age is the belt to (b)'s suspenders, so it **must** stay below that idle
  timeout; never raise the default to ≥ `1800`. `0` disables the proactive
  re-login (leaving (b) the sole recovery); an unset / non-integer / negative
  value falls back to the default and **never raises on the read path**
  (`_resolve_session_max_age_seconds`, the
  `adapters.http._resolve_max_response_bytes` precedent). Resolved **once** at
  import into the module global `_SESSION_MAX_AGE_SECONDS` — an **env override,
  not a chassis `Settings` field**, deliberately: a `Settings`-model field would
  couple `get_settings()` to every vmware read on the hot path (and broke 30
  unit tests when tried). Age is tracked with `time.monotonic` (immune to
  wall-clock steps) in `_session_established_at`, dropped alongside the token on
  `invalidate_session` / `aclose`.

**VI-JSON 500 auth faults on vCenter (`#3566`).** On the vCenter VI-JSON
path, vCenter reports a vim method fault as **HTTP 500** with the fault object
as the JSON body (`{"_typeName": "NotAuthenticated", "faultstring": ...}`).
`_post_vmomi_json` now reads that body (`vim_body.method_fault_type`) and
treats the same auth set as the SOAP path (`_SOAP_AUTH_FAULT_TYPES`:
`InvalidLogin` / `NoPermission` / `NotAuthenticated`) as auth-class:

- `NotAuthenticated` → `invalidate_session(target)`, a new login, and **one**
  retry of the same call. This is the guest-operations case: vCenter passes
  guest calls on to the ESXi host, and the host-gateway token tied to the
  session can go stale while vCenter-local calls (`RetrievePropertiesEx`) on
  the same session still work (Broadcom KB 440879). A new session gets a new
  token. The rejected call never ran, so the retry is safe for a write too.
  The retry covers **only the failing call**, not the whole dispatch, so the
  earlier steps of a multi-step operation (for example `StartProgramInGuest`
  before the `ListProcessesInGuest` polls of `guest.program.run`) never run
  twice. A recovered call returns normally, so the dispatcher writes one
  success audit row and no error row. If the fault comes back on the fresh
  session it raises `ConnectorAuthError(stage="after_relogin",
  cause="vim_fault_NotAuthenticated_after_relogin")` with a "do not restage,
  check the host connection" remediation.
- `InvalidLogin` / `NoPermission` → `ConnectorAuthError(stage="call",
  cause="vim_fault_<type>")` at once, with no retry: a new session cannot fix
  a rejected credential or a missing privilege (the `#3810` PBM precedent).
  `NoPermission` carries a "grant the privilege, do not restage" remediation.
- A **401 on the retried call** (after the connector's own re-login) is
  final too: `ConnectorAuthError(stage="after_relogin",
  cause="session_dispatch_401_after_relogin")`, the same cause the
  dispatcher stamps on its own post-re-login rejection. It must not escape
  as a plain status error, because the dispatcher's `#2067` recovery would
  then log in a third time and re-run the **whole** operation, so a step
  that already ran before the fault (the `StartProgramInGuest` of
  `guest.program.run`) would run twice. A 401 on the **first** try is
  unchanged and still uses the dispatcher's recovery.
- Any other fault (`InvalidArgument`, `PlatformConfigFault`, ...) and any
  other status (on the first try or on the retried call) are unchanged: the
  plain `httpx.HTTPStatusError` reaches the dispatcher.

`ConnectorAuthError.status_code` is the real status (500). The dispatcher's
existing `except ConnectorAuthError` arm turns it into `connector_auth_failed`;
`ConnectorAuthError.stage` (`establish` by default) makes the summary say
which step was rejected (see `error-message-shape.md`). The ESXi SOAP path
(`_soap_fault_error`) still raises the establish-stage error for a mid-call
fault and recovers on the next dispatch, not within the call.

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
   and `probe_method` is `GET /api/about (400) -> soap-currentsession (live)`
   (see the liveness bullet below). So a standalone ESXi target now
   fingerprints reachable instead of dead-ending on the vAPI-only
   `/api/about`.
   - **Liveness read (`#3773d` / `#3710`).** Reachability is **confirmed with
     one lightweight authenticated read**, not asserted from the cached
     `about.version`: `_esxi_session_is_live` reads the single
     `SessionManager.currentSession` property off the cached SessionManager moid
     **through the (b)-protected `object.collect` path**. Pre-fix, `reachable`
     was stamped purely from cache, so a server-side-expired-but-locally-cached
     session (which still answers the unauthenticated bootstrap) probed green
     while every real read came back void. Now a stale session is either healed
     (the health-reset re-login succeeds and `currentSession` comes back →
     genuinely reachable) or, when the re-login cannot recover it, surfaces as
     `reachable=False` ("stale/void SOAP session") instead of masking the
     outage. A missing SessionManager moid or a read failure degrades to
     unreachable, never propagates.
   - **The ESXi probe is always live (`#3710`).** Every ESXi probe sends
     exactly one SOAP call to the host — the `currentSession` read — even
     when the session cache is warm (no new `RetrieveServiceContent` or
     `Login` is needed then). `probe_method` ends in `(live)` to say so:
     `reachable` comes from a call made during this probe. A `(cached)`
     ending would mean the answer came from earlier data without asking the
     host. The ESXi probe never does that, so it never reports `(cached)`.
     Note that `version` still comes from the `about.version` cached at login
     time; only reachability is checked live.

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

The 73 composites (16 reads + 57 writes) land as `source_kind="composite"`
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
  ├─ vim RetrievePropertiesEx (HostSystem.parent)        # depth 0: the host's cluster (#4895)
  ├─ vim RetrievePropertiesEx (configurationEx)          # depth 0: the cluster's DRS config
  ├─ GET:/vcenter/vm?hosts=<host>                        # depth 0 (direct session read)
  ├─ vmware.composite.vm.migrate (× running VMs)        # depth 1 (dispatch_child of a composite;
  │    ├─ vim RetrievePropertiesEx (drsRecommendation)  #   skipped when DRS is fully automated)
  │    └─ POST:/vcenter/vm/{vm}?action=relocate         # depth 1 (direct session write, gated)
  ├─ vim POST:/HostSystem/{moId}/EnterMaintenanceMode_Task # depth 0 (gated vmomi write + Task poll, #2970)
  └─ GET:/vcenter/vm?hosts=<host>                        # depth 0: which VMs left, which stayed
```

**What `host.evacuate` does (#4895).** The vCenter VM listing rows
(`Vcenter.VM.Summary`: `vm`, `name`, `power_state`, `cpu_count`,
`memory_size_mib`) carry no cluster field, so the handler reads the
cluster once from the host's `parent` property. A host outside a cluster
is refused before any change (`not_in_cluster`). It then reads the
cluster's `configurationEx` and picks `drsConfig` in code (vCenter 8.0
refuses the nested path, #3617). When DRS is on and fully automated, it
sends no per-VM move: vCenter moves the running VMs itself as part of the
maintenance-mode task. Otherwise it moves every running VM (`power_state`
`POWERED_ON`) through the recursive `vm.migrate` with that cluster; a VM
DRS has no suggestion for is `no_recommendation` and stops the call before
maintenance mode unless `tolerate_partial_failure` is set. Powered-off VMs
are never moved (MEHO does not send `evacuatePoweredOffVms`) and may stay
on the host. The maintenance body is `{"timeout": 0, "maintenanceSpec":
{"vsanMode": {"objectAction": <vsan_mode>}}}` — `vsan_mode` is
`ensureObjectAccessibility` (vCenter's own default), `evacuateAllData` or
`noAction`; `cluster.patch` keeps the plain `{"timeout": 0}` body. After
the task the handler lists the host's VMs again, and that listing alone
decides: `migrated_vms` are the VMs that are gone (moved by MEHO or by
vCenter, see `moved_by`), `remaining_vms` the VMs still listed with the
reason (`powered_off`, `no_recommendation`, `still_listed_after_move` when
`vm.migrate` reported a move the host does not show, `not_moved`,
`not_attempted`), `failed_vms` only the VMs MEHO tried to move and that are
still there. When the 600 s poll ends
while the task still runs, the answer is `status: timeout` with
`maintenance_task` and a `warning` issue — the call did not fail.

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

The 73 composites are hand-authored aggregators the connector ships as
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
| `vm.deploy_from_library` | `deployed`, `deploy_failed`, `deploy_error`, `invalid_reference`, `library_not_found`, `ambiguous_library`, `item_not_found`, `ambiguous_item`, `resolve_error` (OVF/OVA deploy, #2909; the synchronous deploy's 200 body is a `DeploymentResult` — `succeeded=false` → `deploy_failed` with the report's per-issue messages, an HTTP 400/404 for an invalid/missing placement resource → `deploy_error`, and a faulted content-library `find` during name resolution → `resolve_error` (#3071), both with a structured message carrying the vCenter status — so a placement/mapping/resolution error is a structured status, never a raw vendor fault; the VM is deployed under a unique deploy name and renamed on success, and a failed deploy deletes the partial VM it left and adds a `cleanup` issue, #3697) |
| `vm.import_from_library` | `deployed`, `deploy_failed`, `deploy_error`, plus the same resolution statuses as `vm.deploy_from_library` (typed HttpNfcLease OVF import, #3229; the deploy-envelope family is reused — `CreateImportSpec` descriptor rejection → `deploy_failed`, a vim control-plane / lease / disk-upload fault → `deploy_error` — plus a per-disk `transfer` manifest; see the dedicated section) |
| `vm.snapshot.revert` | `reverted`, `ambiguous`, `not_found`, `timeout` (vim `RevertToSnapshot_Task` polled; a task fault raises `connector_error`, #2970; by `snapshot_name` or `snapshot_id`, an id not in the VM's tree is `not_found`) |
| `vm.snapshot.create` | `created`, `unchanged` (a snapshot with the name exists; nothing written), `not_found`, `invalid_request`, `timeout` (vim `CreateSnapshotEx_Task` polled; a task fault raises `connector_error`) |
| `vm.snapshot.delete` | `deleted`, `unchanged` (no snapshot with the id), `invalid_request`, `still_present`, `timeout` (vim `RemoveSnapshot_Task` polled; the teardown envelope) |
| `vm.migrate` | `migrated`, `no_recommendation` |
| `vm.power` | `ok`, `error`, `tools_unavailable` (single VM; `tools_unavailable` when a soft `guest_shutdown`/`guest_reboot` finds Tools down) |
| `vm.power.bulk` | (per-VM `results` + aggregate `summary` + `aborted_on_failure`) |
| `vm.disk.grow` | `grown`, `invalid_shrink`, `disk_not_found`, `timeout` (grow-only; `invalid_shrink` refuses a request ≤ current capacity before any write; `timeout` when the `ReconfigVM_Task` poll gives up) |
| `vm.disk.attach` | `attached`, `invalid_vmdk_path`, `invalid_unit`, `controller_not_found`, `unit_in_use`, `timeout` (#3256; the first four are pre-write fail-closed refusals; `timeout` when the `ReconfigVM_Task` add poll gives up; a task *fault* raises `connector_error`) |
| `vm.clone_from_template` | `cloned`, `template_not_found`, `ambiguous_template`, `not_a_template`, `timeout` (name-resolution refusals + the template assert are pre-write; `timeout` when the `CloneVM_Task` poll gives up; a task *fault* raises `connector_error`) |
| `host.evacuate` | `evacuated`, `partial`, `aborted`, `timeout`, `not_in_cluster` (the maintenance-enter is the vim `EnterMaintenanceMode_Task`, polled; a task *fault* raises `connector_error`, #2970; a poll `timeout` is a status with the task id and a `warning` issue, not a failure, #4895; `not_in_cluster` is a pre-change refusal) |
| `host.detach_from_vds` | `detached`, `incomplete`, `timeout` (the detach is the vim `ReconfigureDvs_Task` host-member remove, polled, #2970) |
| `cluster.patch` | `completed`, `stopped` (per-host vim maintenance `*_Task`s + the vLCM `software?action=apply&vmw-task=true` cis task, every task polled before the next step, #2970) |
| `cluster.drs_rule.create` | `created`, `rule_exists`, `insufficient_vms`, `timeout` (idempotent on rule name — a duplicate `rule_exists` is refused before any write; `insufficient_vms` when fewer than two named VMs resolve to the cluster; `timeout` when the `ReconfigureComputeResource_Task` poll gives up) |
| `cluster.drs_rule.delete` | `deleted`, `unchanged`, `invalid_request`, `precondition_failed`, `still_present`, `timeout` (removes one rule of any type by name; `unchanged` when no rule has the name; `precondition_failed` when two or more rules share it, listed in `blockers`; `invalid_request` for an unknown cluster; `deleted` only after a new read of the rules misses the rule's key; a task *fault* raises `connector_error`) |
| `cluster.drs_vm_host_rule.create` | `created`, `rule_exists`, `group_exists`, `insufficient_vms`, `insufficient_hosts`, `timeout` (#3505 VM-Host rule; rule + group names are idempotence keys refused before any write; `insufficient_*` when no VM / host name resolves in the cluster; `timeout` when the `ReconfigureComputeResource_Task` poll gives up) |
| `resource_pool.create` | `created`, `cluster_root_pool_unresolved`, `no_parent` (#3505; the REST `POST:/vcenter/resource-pool` create — `cluster_root_pool_unresolved` when the `cluster` convenience cannot read the cluster's root pool, `no_parent` when neither `parent` nor `cluster` is given, both before any write; read-back confirms the pool under the resolved parent) |
| `resource_pool.delete` | `deleted`, `not_empty`, `delete_unverified` (#3505; `not_empty` refuses a pool with child pools / VMs unless `force=true` — the REST `DELETE` reparents children up to the parent; `delete_unverified` when the read-back still lists the pool) |
| `folder.create` | `created`, `parent_not_found`, `ambiguous_parent` (synchronous `CreateFolder`; both resolution refusals happen before any write and carry an `error` issue, so the operation fails and the audit row reads `error`, #3588; `ambiguous_parent` lists `candidate_folders`, behind a result handle when oversized, #4223) |
| `network.portgroup.create` | `created`, `invalid_vlan_spec`, `timeout` (vim `CreateDVPortgroup_Task` polled, #3091; `invalid_vlan_spec` refuses a trunk+access clash before any write; `timeout` when the poll gives up; a task *fault* — e.g. `DuplicateName` — raises `connector_error`. The `created` envelope carries a read-back `observed` = `{name, vlan}` off the new portgroup's `config`. The trunk / access VLAN specs are `InheritablePolicy` subtypes, so each wire body carries `inherited: false` — without it vCenter defaults `inherited: true` and drops the `vlanId`, silently creating an untagged (VLAN 0) portgroup, #3356) |
| `network.portgroup.security.set` | `updated`, `no_change_requested`, `timeout` (vim `ReconfigureDVPortgroup_Task` polled, #3091; `no_change_requested` refuses when none of the three booleans is supplied, before any read/write; `timeout` when the poll gives up; a task *fault* raises `connector_error`. Carries `previous` (pre-write security triple) + `observed` (post-write triple) read-backs) |
| `network.portgroup.vlan.set` | `set`, `unchanged`, `invalid_vlan_spec`, `timeout` (vim `ReconfigureDVPortgroup_Task` polled; reconfigures an **existing** portgroup's `defaultPortConfig.vlan` to a trunk `NumericRange[]` or a single access VLAN — the given spec **replaces** the current config, there is no merge. Idempotent: the current VLAN is read + normalised (ranges sorted + merged-adjacent) first and a request that already matches returns `unchanged` with no write / no task; `invalid_vlan_spec` refuses both/neither VLAN mode (or `replace=false`) before any write; `timeout` when the poll gives up; a task *fault* raises `connector_error`. Carries normalised `before` / `after` VLAN views) |
| `vm.resize` | `resized`, `requires_power_off`, `no_change`, `partial` |
| `vm.nic.repoint` | `repointed`, `not_found`, `ambiguous`, `invalid_request` |
| `vm.device.cdrom` | `removed`, `updated`, `disconnected`, `invalid_request`, `result_unknown` (the change timed out and may still run; carries `running_tasks`, `vm_question` and an `error` issue) |
| `vm.resource_allocation.set` | `set`, `unchanged`, `invalid_request`, `vm_not_found`, `partial`, `timeout` (vim `ReconfigVM_Task` polled, #3880; `invalid_request` refuses an empty request, an out-of-range value (a limit of `0` included — `-1` clears) or a resulting limit below its reservation before any write; `unchanged` when the request already matches (no task); `partial` when the task succeeded but the re-read `after` does not match every requested field; `vm_not_found` on a promoted `ManagedObjectNotFound`; a task *fault* raises `connector_error` like `vm.disk.grow`. Carries `before` / `after` allocation views) |
| `network.portgroup.delete` / `folder.delete` | `deleted`, `unchanged`, `precondition_failed`, `still_present`, `timeout` (#3339; vim `Destroy_Task` polled; `precondition_failed` before any write when something still uses the object -- listed in `blockers`; a task *fault* raises `connector_error`) |
| `host.standard_portgroup.delete` | `deleted`, `unchanged`, `invalid_request`, `precondition_failed`, `still_present` (#3339; synchronous `RemovePortGroup` -- a vim fault raises) |
| `datastore.file.delete` | `deleted`, `unchanged`, `invalid_request`, `precondition_failed`, `still_present`, `timeout` (#3339; `DeleteDatastoreFile_Task` polled; a task fault raises) |
| `datastore.dir.create` | `created`, `unchanged`, `invalid_request`, `precondition_failed`, `not_verified` (#3339; synchronous `MakeDirectory` -- a vim fault raises) |
| `content_library.delete` / `content_library.item.delete` | `deleted`, `unchanged`, `invalid_request`, `precondition_failed`, `still_present` (#3339 / #3331; REST `DELETE` -- a REST fault raises) |

`vm.create` is the only composite that issues a compensating
mutation (`DELETE:/vcenter/vm/{vm}`) on partial failure. When that
rollback DELETE waits for approval, it saves the rollback on the approval
row (#4217); approving it re-runs `vm.create`, which then only deletes the
VM the first run created (`_resumed_vm_create_rollback`) and returns the
same `rolled_back` envelope. An approved rollback DELETE with no saved
rollback (a request saved before #4217) is refused before anything runs
(`composite_resume_unsupported`). A rolled-back `vm.create` is an
`error` result (#3813). Its error message names the failed step, says
what happened to the VM, and ends with the cause, which carries
vCenter's own message (`_vsphere_fault_detail`, for a REST error body
and for a VI-JSON method fault alike). The `rollback` field
gives the VM moid and the outcome (#4057): `removed`,
`awaiting_approval` (the VM exists until the DELETE is approved) or
`not_removed` (the DELETE failed or was denied; delete the VM by hand).
The field is absent when the run failed before any VM existed. The other
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
  portgroup by display name via
  `GET:/vcenter/network?filter.types=<backing_type>`, then PATCHes
  `PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}` with
  `{backing: {type: backing_type, network: <moid>}}` — the same internal
  sub-op the `host.detach_from_vds` fallback path uses. `backing_type` is
  `DISTRIBUTED_PORTGROUP` (default, back-compatible) or
  `STANDARD_PORTGROUP`; the latter moves a NIC onto a host-local
  standard-switch portgroup (e.g. to repair a NIC stranded on an L2 that
  cannot reach its gateway). Standard portgroups are host-scoped, so one
  display name routinely resolves to several `Network` moids (one per
  host) and the REST Automation API exposes no reliable VM->host mapping,
  so an ambiguous name fails closed (`ambiguous`, candidate moids listed)
  and the operator supplies the exact moid via the `network` param. An
  explicit `network` moid skips name resolution and is validated for
  existence + type-consistency with `backing_type` (`invalid_request` on
  mismatch); a name matching zero portgroups returns `not_found`. No PATCH
  is issued on any non-`repointed` status.
- **`vm.device.cdrom`** reads the device's current backing + state via
  `GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}` (surfacing a host-local
  ISO path the approver needs to see), then dispatches the `action`:
  `remove` (`DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}`), `update`
  (`PATCH` the backing — requires a `backing` param, e.g.
  `{type: CLIENT_DEVICE}` to un-pin a host-local ISO), or `disconnect`
  (`POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect`).
  When the write times out, the result is `result_unknown`, not a plain
  timeout: vCenter may still run the change, often because it waits on a
  question in the VM (for example a locked CD-ROM door). The result names
  the VM's queued or running tasks and the pending question, read with
  `vm_wait_state.read_vm_wait_state` (best effort: when only the task
  read fails, the question is kept and `tasks_error` says what could not
  be read), and carries an `error` issue, so the call is recorded as
  failed. Answer the question with `vmware.vm.question.answer`.

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
the #3087 recipe lanes) and *raw* VI-JSON dispatch then mounted on `/api` — a
9.x-fleet accommodation that 404s on vCenter 8.0.x (#2466; raw dispatch moved to
`/sdk/vim25` in #3534, but still has no `/api` fallback or task polling) — so the
substrate's `/sdk/vim25/{release}` mount is the governed cross-version path to
the flag. `vm.create`'s pre-9.0 create arm (#3099) rides them too:
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
fault, `error_message` carries the best available description via the
shared `vim_body.fault_message` extractor, which spans both VI-JSON fault
shapes. On **9.x** `TaskInfo.error` is a `LocalizedMethodFault` and the
chain (#3116) is `error.localizedMessage` (optional on the wire) → the
joined `fault.faultMessage[*].message` texts → the concrete fault's
`_typeName` (e.g. `InvalidArgument`). On **8.0.x** the concrete fault is
flattened directly onto `error` (`_typeName` + `faultstring`, no
`LocalizedMethodFault` wrapper and no nested `fault`); the extractor reads
`error.faultstring` / its joined `faultMessage` and prefixes the concrete
`_typeName`, surfacing e.g. `InvalidArgument: A specified parameter was
not correct: configSpec.guestId` where the 9.x-only chain previously lost
the text (#3663). The same helper backs `tasks.recent`'s `error_message`
and the OVF/lease `fault_text` projection. Every vim-arm composite's
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
the cluster's whole `configurationEx`), `_read.py`'s `_extract_object_props`,
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
form of the vim `VirtualDevice.key`. It also accepts the int key (#3789);
the handler turns it into a string first. The handler matches the device by
`str(key)` and the REST `GET hardware/disk/{disk}` supplies label +
capacity for the park-time preview. The preview is a live-read from→to
capacity diff (`{vm, name, disk, disk_label, current_capacity_bytes,
requested_capacity_bytes, delta_bytes}`) — the delta is the decision the
approver makes; a failing disk read parks with the #1628
`preview_unavailable` marker (the delta is unknowable).

### VM CPU / memory limit + reservation (`vm.resource_allocation.show` / `.set`, #3880)

A VM's resource allocation (`VirtualMachineConfigInfo.cpuAllocation` /
`memoryAllocation`, each a vim `ResourceAllocationInfo`: `limit` /
`reservation` / `shares`) has no REST expression — the pinned `vcenter.yaml`
`hardware/cpu` + `hardware/memory` resources carry the vCPU count and memory
size only, and `vmware.vm.info` does not project it — and the ingested
vim-object `ReconfigVM_Task` binding 404s under `/api` (#3534). Both ops live
in `composites/_vm_allocation.py` and ride the `/sdk/vim25` seam:

- **`vm.resource_allocation.show`** (`safe`) — one ungated
  `RetrievePropertiesEx` of `name` + `config.cpuAllocation` +
  `config.memoryAllocation`; returns `{limit, reservation, shares: {level,
  shares}}` for CPU (MHz) and memory (MB). `limit = -1` means unlimited.
- **`vm.resource_allocation.set`** (`caution`, `requires_approval=True` — the
  `#3505` allocation-write posture; reversible, no guest or data impact) —
  params `vm` + at least one of `cpu_limit_mhz` / `cpu_reservation_mhz` /
  `memory_limit_mb` / `memory_reservation_mb` (`-1` clears a limit; a limit
  of `0` is refused). Reads the
  current allocation (`before`), refuses `invalid_request` / returns
  `unchanged` before any write, then issues one `ReconfigVM_Task` whose
  `VirtualMachineConfigSpec` carries **only** the requested
  `ResourceAllocationInfo` fields (unset fields are left unchanged by
  vSphere) through the shared `_write_vmomi_sub_op` gate, polls the task and
  re-reads (`after`). The response carries the task moid + terminal
  `task_state`; a task fault raises (`connector_error`, audited as failed). The park-time preview live-reads the current allocation and
  echoes the requested fields. The vim pair is the same
  `RetrievePropertiesEx` / `ReconfigVM_Task` `vm.disk.grow` reconciles, so no
  new vim op_id is introduced. vCenter targets only (the standalone-ESXi SOAP
  transport has no `ReconfigVM_Task` builder).

### VM hardware read, CD drive add and Tools installer unmount

Three operations replace raw vCenter calls that people used often for VM
hardware. All three call vSphere REST on the connector session, so they need
no catalog ingest. Every path exists on vCenter 8.0 and 9.0, and all three are
on both catalogs (`vmware-rest-8.0` and `vmware-rest-9.0`). The read names the
VM by id (`vm`, such as `vm-42`) or by `name`, like `vmware.vm.info`: an
unknown name gives `not_found`, a name that fits two VMs is an error. The two
changes take the VM id (`vm`) only, and no name. A change waits for approval,
and it must then run on the VM the approver saw. A name looked up again after
approval could point to another VM (for example after a rename). So get the
id from the read first.

- **`vmware.vm.hardware.show`** (typed read, `safe`, no approval;
  `typed_ops_vm_hardware.py` with its schemas in `_vm_hardware_schemas.py`,
  handler `vm_hardware_show`, group `vmware-vm-hardware`). Reads, all `GET`
  under `/vcenter/vm/{vm}`:
  `hardware/cpu` and `hardware/memory` (must work; a 404 gives `not_found`),
  `hardware/cdrom` and `hardware/cdrom/{cdrom}`, `hardware/disk` and
  `hardware/disk/{disk}`, `hardware/adapter/scsi`, `/sata` and `/nvme` with
  `.../{adapter}` for the bus, and `tools/installer`.
  Answer: `cpu` (count, cores per socket, hot add / remove), `memory`
  (`size_mib`, hot add, step and limit), `cdroms` (backing type, ISO file,
  host device, state, `connected`, `start_connected`), `disks` (type, address,
  `capacity_bytes`, `vmdk_file`), `disk_controllers` (kind, adapter, type,
  bus, sharing, `devices`) and `tools_installer_mounted`. The vSphere REST
  controller resources do not list their devices, so the read joins them
  itself: a disk or CD drive sits on the controller of the same kind and bus
  number (`Disk.Info.scsi.bus`, `Cdrom.Info.sata.bus`, and so on). Each
  controller's `devices` lists them by unit, with id, label, and for a disk
  its size and VMDK file. The CD drive, disk, controller and installer reads
  are best effort: a failed part is `null` and `read_notes` says why. When a
  list works but one drive or disk cannot be read, that row keeps its id
  with `null` fields and `read_notes` names it.
- **`vmware.composite.vm.cdrom.add`** (`dangerous`, waits for approval;
  `composites/_vm_cdrom_tools.py`). One `POST /vcenter/vm/{vm}/hardware/cdrom`
  with a top-level `Cdrom.CreateSpec`: an ISO file backing when `iso_file` is
  set (then `start_connected` defaults to true), else a client device backing
  (an empty drive). Optional `type` (IDE or SATA) and `allow_guest_control`.
  It reads the new drive back (`cdrom_info`). vCenter answers HTTP 400 for an
  IDE drive on a running VM or a suspended VM; the answer is then
  `status="refused"` with vCenter's error, and nothing changed. Not safe to
  run twice: each call adds a drive.
- **`vmware.composite.vm.tools.installer.unmount`** (`caution`, waits for
  approval; same module). Reads `GET /vcenter/vm/{vm}/tools/installer` first.
  When the installer is not mounted it sends nothing (`not_mounted`), so it is
  safe to run twice. Else `POST .../tools/installer?action=disconnect`, then it
  reads again: `unmounted`, or `still_mounted` when it still reads as mounted.
  vCenter refuses the call for a VM that is off (`refused`).

The two changes go through the shared write seam (`_write._write_sub_op`), so
each REST write is checked by policy like the other composite writes. Their
approval previews are registered in their own module: the cdrom add shows the
drive to add and the drives the VM has now; the unmount shows whether the
installer is mounted now. The `vm` id goes into the URL path, so it must match
`^vm-[0-9]+$`; a device id from vCenter is checked and percent-encoded too.
`test_connectors_vmware_rest_vm_hardware_reconcile.py` checks every REST call
of the three operations against the pinned `vcenter.yaml` of 8.0 and 9.0 (it
skips without the vendor spec files). Tests use saved responses
(`test_connectors_vmware_rest_typed_vm_hardware.py`).

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

**Choose the storage mechanism before applying those knobs.** The VMware
guidance distinguishes two shared-disk paths; they do not share a universal
EZT or datastore-flag requirement (vSAN 8.0 documentation; vSphere 8.0,
Table 568).

- **VMFS clustered VMDK.** This path requires `eagerzeroedthick` shared disks,
  FC or NVMe-FC connectivity (and NVMe-TCP on 8.0 U3), and the datastore's
  Clustered VMDK flag. Use the physical SCSI-bus-sharing controller for the
  WSFC/SQL FCI reservation model.
- **vSAN native shared VMDK.** This path is provisioned through the vSAN
  storage policy, including Object Space Reservation where required. Any disk
  type is allowed; there is no VMFS-style datastore Clustered VMDK flag to
  set on vSAN.

The shared operational recipe is the same after selecting the applicable
mechanism: create the OS separately (or clone from a template), give each node
a dedicated `physical`-bus-sharing controller, create the shared disks on the
first node with that mechanism's provisioning requirements, then
`vm.disk.attach` the same VMDKs onto the second node at identical
`controller_key`/`unit_number`. Leave `sharing="none"` for WSFC — reach for
`multi_writer` only when the guest application, rather than SCSI-3 PR, owns
locking. (`vm.create` currently folds shared disks at create time; adding a
newly-created shared disk to an *already-provisioned* VM is a follow-up, not
in this task's scope.)

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

**`vm` must be a moid, not a name (#3479).** `vm.destroy` and `vm.power`
both declare `"pattern": "^vm-[0-9]+$"` on `vm` (`schemas.VM_MOID_PATTERN`).
A VM name such as `web-01` fails schema validation, so `preview_operation`
and `call_operation` return `invalid_params` with a `validation_errors` entry
at `$.vm` (`validator: "pattern"`). This happens before any vCenter read. Before
the pattern, a name made the `vm.destroy` preview read 404, and the call was
denied as `blast_radius_required`, which hid the real cause. For `vm.power` a
name failed later at vCenter with a 404. The composites do not look a name up:
two VMs can have the same name, and a destroy must not guess. The caller reads
the inventory (for example `GET:/vcenter/vm` with `filter.names`) and passes
the moid.

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
  pinned `vi-json.yaml` (the same lane as disk-grow). The REST arm's
  `GET:/vcenter/vm/{vm}` read + `DELETE:/vcenter/vm/{vm}` are declared in
  `_write._SUB_OPS_VM_DESTROY` and reconciled against the pinned
  `vcenter.yaml`. The governed-subop discovery surface
  (`GET /api/v1/operations/governed-subops?op_id=vmware.composite.vm.destroy`)
  joins both lists, so an operator sees the delete step of either arm, and
  the REST delete is flagged un-grantable (delete-shaped).

The blast-radius preview builder (`_write_preview._vm_destroy_preview`)
live-reads `Vm.Info` for object identity (moid / name / power state) plus
the enumerated disks (with capacities) and NICs, and best-effort enumerates
snapshots via the vim snapshot read (`_read_vm_snapshots_best_effort` — a
fault yields "no snapshots enumerated", never sinks the park). It declines
(`None` → the park is refused `blast_radius_required`, fail-closed) when the
VM cannot be read.

### Governed teardown deletes (#3339)

Seven composites let a teardown remove what the create-side composites and the
deploy paths leave behind, without leaving the governed path. Six are deletes
at the `destructive` level: a second person must always approve each call,
after reading what will be deleted (the blast radius). The seventh,
`datastore.dir.create`, is the matching create at the `caution` level.

| op_id | What it removes | vSphere call | Refused before any change when |
| --- | --- | --- | --- |
| `network.portgroup.delete` | one distributed portgroup | vim `DistributedVirtualPortgroup.Destroy_Task` (polled) | a VM is on it; any other port is connected (VMkernel / host adapter, read with `FetchDVPorts`); it is the uplink portgroup; it is backed by an NSX segment |
| `host.standard_portgroup.delete` | one port group on one host's standard switch | vim `HostNetworkSystem.RemovePortGroup` (synchronous) | a VM registered on that host (also powered off) or a VMkernel adapter uses it; the host has no network config manager |
| `folder.delete` | one EMPTY folder | vim `Folder.Destroy_Task` (polled) | the folder has any content (vSphere would delete it too); it is a root / datacenter top folder; it is a datastore cluster (`group-p`) |
| `datastore.file.delete` | one file, or one folder with all its files | vim `FileManager.DeleteDatastoreFile_Task` (polled) | the path breaks the path rules below; the datastore is not VMFS or NFS; the path is, or contains, a file a registered VM uses |
| `datastore.dir.create` | -- (creates one folder) | vim `FileManager.MakeDirectory` (synchronous) | the path rules; not VMFS / NFS; a file has that name; the parent is missing and `create_parents` is not true |
| `content_library.delete` | one library (local or subscribed) | REST `DELETE /content/local-library/{id}` or `/content/subscribed-library/{id}` | the id is not a UUID; another library subscribes to it; a VM mounts one of its items; it has items and `delete_items` is not true |
| `content_library.item.delete` | one item of a local library | REST `DELETE /content/library/item/{id}` | the id is not a UUID; its library does not read back as LOCAL on this vCenter; a VM mounts the item |

**One plan, used twice.** Each op has one read-only planning function
(`_teardown.TeardownPlan`). It reads the object, decides if it exists, lists
what goes with it and what blocks it, and decides a refusal. The park-time
preview builder and the handler both call it. So the approver sees exactly the
refusal the handler will apply, and the handler checks the live state again
after approval. The builders return `None` when there is no connector, like
`vm.destroy`'s: the egress-free `preview_operation` then binds only the params
hash, and the live blast radius is built when the call parks.

**Absent means `unchanged`.** When the object is already gone the handler
writes nothing and returns `status="unchanged"`. The preview still returns a
well-formed blast radius (`object.present = false`, irreversibility
`none-object-already-absent`), so a second run of a finished teardown can park
instead of failing with `blast_radius_required`.

**Faults fail the call.** A task fault or a vim / REST fault raises, so the
dispatcher records `connector_error` and audits the call as failed -- never as
success (the #3881 review lesson). A poll that runs out of time returns
`status="timeout"`.

**Strict inputs.** Every moid or id that goes into a URL path is shape-checked
in the parameter schema (preview time) and again in the handler, before any
request: `dvportgroup-N`, `host-N`, `group-xN` (not `group-p`), `datastore-N`,
and UUIDs for content-library and item ids. So no input can point a request at
another object (httpx removes `..` path segments before it sends a request).
The patterns live once in `schemas.py`.

**Partial answers refuse.** `_teardown_reads` never reads an incomplete
vCenter answer as "nothing uses it". A property vCenter could not read (a
`missingSet` entry, e.g. missing permission) on the object to delete fails the
call before any write. For the "who uses it" reads of many objects (VMs on a
datastore, network objects of a host, VMs that mount library items) a paged
answer (`token`), a left-out object or an unreadable property refuses the
delete. Only the distributed-portgroup port list (`FetchDVPorts`) is best
effort: it only names users, `object.port_check` shows when it failed, and
vSphere itself refuses to delete a portgroup with connected ports
(`ResourceInUse`, which raises).

**Read-back.** After the write each op reads the object again (#3313 pattern):

| op_id | Read-back | Absent means |
| --- | --- | --- |
| `network.portgroup.delete` | `RetrievePropertiesEx` of the portgroup | `ManagedObjectNotFound` (SOAP- or JSON-shaped) or an empty result |
| `host.standard_portgroup.delete` | host `config.network.portgroup` | no entry with that `spec.name` |
| `folder.delete` | `RetrievePropertiesEx` of the folder | `ManagedObjectNotFound` or an empty result |
| `datastore.file.delete` | `SearchDatastore_Task` on the parent folder | no exact name match, or the parent is gone (`FileNotFound`) |
| `datastore.dir.create` | `SearchDatastore_Task` on the parent folder | -- (must find a `FolderFileInfo`) |
| `content_library.delete` | `GET /content/library/{id}` | HTTP 404 |
| `content_library.item.delete` | `GET /content/library/item/{id}` | HTTP 404 |

**Why these calls work on vCenter 8.0.x.** The vim calls go through the
documented `/sdk/vim25/{release}` VI-JSON base (`_post_vmomi_json`), the same
path the `vm.destroy` vim arm, `vm.disk.grow` and the portgroup writes already
use on 8.0.3. The composites do not call the ingested vim bindings directly. The pinned REST spec has no delete for portgroups, folders or datastore
files, so vim is the only option there. The content-library calls are REST on
`/api`; vCenter 8.0 and 9.0 serve the same paths (only the path parameter names
differ), and `vm.deploy_from_library` already uses the same find / item reads
on 8.0.x. Composites are not affected by the per-catalog version guard (#3568),
which applies to ingested ops only.

**Datastore path rules.** `datastore` is a moid and `path` is relative to the
datastore root. The parameter schema and `_datastore_browse.path_problem`
share one pattern (`schemas.DATASTORE_PATH_PATTERN`), so preview and call
reject the same paths: no root, no leading `/`, no empty segment, no segment
that starts with `.` (hidden and lock files at any depth, and so no `.` / `..`)
or starts or ends with whitespace, no wildcards, brackets, backslashes or
control characters. The top-level folder may not be `contentlib-*`, `fcd` or
`catalog` (ignoring case): content-library folders are deleted through the
library ops, and `fcd` / `catalog` hold first-class disks (for example
Kubernetes volumes that no VM lists) and their index.

**Datastore file details.** Only `VMFS`, `NFS` and `NFS41` datastores
(`summary.type`) are supported. On vSAN and vVol vCenter lists VM files by a
folder UUID while people use the friendly folder name, so the VM file check
could not match them; these and unknown types are refused. The browse answer
gives the path vSphere resolved (`folderPath` + entry name); the VM file check,
the delete call and the read-back all use that path, never the raw input. The
VM file check (`_datastore_claims.vm_claims`) reads every VM in `Datastore.vm`:
`layoutEx.file`, the `fileName` of every device backing (disks, and the ISO /
floppy images and serial-port files `layoutEx` does not list, also on powered-off
VMs and templates) and the `.vmx`. A VM without `layoutEx` blocks its whole
home folder; a VM whose files cannot be known refuses the delete. The
comparison ignores case, so it refuses more, never less. Not detectable: VMs
of another vCenter that share the datastore. `VimTaskResult.fault_type` (new)
carries the vim fault class so a browse of a missing folder (`FileNotFound`)
reads as "absent". vCenter targets only.

**Port group details.** For a host standard port group, VMs registered on that
host whose NICs use the port group's vCenter `Network` object block the delete,
also when powered off (vSphere only refuses for live ports). A distributed
portgroup backed by NSX (`config.backingType == "nsx"`) is refused: delete the
NSX segment instead.

**Folder details.** vSphere has no "delete only if empty" call. Something
moved into the folder in the moment between the check and `Destroy_Task` would
be deleted with it. This short race is accepted.

**Content library details.** Library and item ids must be UUIDs
(`schemas.CONTENT_LIBRARY_ID_PATTERN`), checked in the schema and the handler
(`_library_checks.id_problem`), also for ids vCenter returns for a name. A
library with items needs `delete_items=true` (the #3331 explicit-flag shape);
the blast radius lists up to 50 items with their sizes, and `item_count` is
always exact. Subscribers are subscribed libraries on this vCenter whose
`subscription_url` names the library id, plus the subscriptions the published
library itself knows (`GET /content/library/{library}/subscriptions`, which
also lists other vCenters). Subscribers on other vCenters that the publisher
does not know of cannot be seen. A VM mounts an item when a CD-ROM or floppy
backing `fileName` contains `/<item id>/`; the VMs are read through the
library's datastores (`Datastore.vm`). A library not stored only on datastores
(with items) is refused, because the check cannot run. The item delete refuses
unless the item's `library_id` is a UUID that reads back as a LOCAL library.

Code: `composites/_teardown.py` (shared plan helpers + `folder.delete`),
`_teardown_reads.py` (strict reads), `_teardown_network.py`,
`_datastore_browse.py` + `_datastore_claims.py` + `_datastore_files.py`,
`_library_checks.py` + `_library_delete.py`. Governed sub-op manifests are
listed in `_governed_subops.py`; the vim paths are reconciled against the
pinned `vi-json.yaml` and the REST paths against `vcenter.yaml`.

Follow-up (not in #3339): `_write._split_sub_op` puts path values into URLs
without percent-encoding. The new ops validate every id first; the older
library ops (`content_library.subscribed.status` / `.sync`) do not yet.

### Governed DRS rule delete (`cluster.drs_rule.delete`)

`vmware.composite.cluster.drs_rule.delete` removes one DRS rule from a cluster,
by name. It is the opposite of `cluster.drs_rule.create` and
`cluster.drs_vm_host_rule.create`. A DRS rule tells vCenter where to run VMs:
keep them together, keep them apart, or run them on (or away from) a group of
hosts.

- **Risk level:** `destructive` + `requires_approval=True`. A second person
  always approves, after reading the rule on the approval card. An agent
  principal is refused.
- **vSphere call:** the same as the creates. There is no REST path for
  cluster rules, so it runs `ClusterComputeResource.ReconfigureComputeResource_Task`
  with one `rulesSpec` entry: `operation: "remove"` and `removeKey` = the
  rule's key, `modify=true` (nothing else on the cluster changes). The call is
  the same for every rule type. VM groups and host groups are not removed.
- **The key is boxed.** `ArrayUpdateSpec.removeKey` is typed `Any` in the
  pinned `vi-json.yaml`, so the int key is sent as
  `{"_typeName": "int", "_value": <key>}` (the `Any` discriminator maps `int`
  to `PrimitiveInt`). This is the first request body that sends a boxed
  primitive; the body-shape reconcile lane checks the alias against the spec.
- **One plan, used twice** (the `_teardown.TeardownPlan` seam). It reads the
  cluster's `name` and its whole `configurationEx` once, and takes the rules
  from the `rule` array (no array means no rules). It never asks for
  `configurationEx.rule`: vCenter 8.0.x rejects that path (see "Known issues
  / gaps" below). An unknown cluster gives
  `invalid_request`. No rule with the name gives `unchanged` (safe to run
  twice). Two or more rules with the name give `precondition_failed`, with the
  rules listed in `blockers`. A rule list vCenter could not read fails the call
  before any write.
- **Approval card** (`blast_radius.object`): cluster, cluster name, rule name,
  key, rule type (`affinity`, `anti_affinity`, `vm_host`, or the vim type name
  for any other type), enabled, mandatory (required or preferred), and the VMs
  (up to 50, names best effort) or, for a VM-Host rule, the VM group and the
  host groups. `children` is empty: removing a rule deletes no other object.
- **Check after delete:** after the task succeeds it reads the rules again.
  `deleted` only when no rule has the key any more; otherwise `still_present`.

Code: `composites/_drs_rule_delete.py`. The governed sub-op manifest
(`RetrievePropertiesEx` + `ReconfigureComputeResource_Task`) is in
`_governed_subops.py`.

### VM snapshot operations (`vmware.snapshot.list`, `vm.snapshot.create` / `.delete` / `.revert`)

Four operations cover the snapshot work on one VM. vCenter's REST API has no
snapshot paths, so all four use the VIM API, as VI-JSON on vCenter.

| Operation | What it does | Returns | Risk |
|---|---|---|---|
| `vmware.snapshot.list` (typed) | Reads the VM's `snapshot` property (`rootSnapshotList`, `currentSnapshot`) | `{vm, vm_name, current_snapshot_id, snapshot_count, snapshots}`; each row: `snapshot_id`, `name`, `description`, `created`, `power_state`, `quiesced`, `current`, `parent_snapshot_id` | `safe` |
| `vmware.composite.vm.snapshot.create` | `VirtualMachine.CreateSnapshotEx_Task` with `snapshot_name`, optional `description`, `memory` (default false), `quiesce` (default false) | `{status, vm, vm_name, snapshot_id, snapshot, task, task_state, guidance}` | `caution`, `requires_approval=True` |
| `vmware.composite.vm.snapshot.delete` | `VirtualMachineSnapshot.RemoveSnapshot_Task` for one `snapshot_id`; `remove_children` (default false) | the teardown envelope `{status, object, blockers, task, task_state, guidance}` | `destructive`, `requires_approval=True` |
| `vmware.composite.vm.snapshot.revert` | `VirtualMachineSnapshot.RevertToSnapshot_Task`, by `snapshot_name` or `snapshot_id` (exactly one) | `{status, snapshot_id, candidates, guidance}` | `dangerous`, `requires_approval=True` |

| Versions | Catalog |
|---|---|
| vCenter 8.0 U1, 8.0 U2, 8.0 U3 | `vmware-rest-8.0` |
| vCenter 9.0, 9.1, 9.1.1 | `vmware-rest-9.0` |

Every vendor call is in the pinned 8.0 U1, 9.0 and 9.1.1 `vi-json.yaml`.

- **Shared rows.** `snapshot_tree.py` turns the tree into flat rows, a parent
  before its children. It imports nothing from the connector, so the typed
  read, the composites and `schemas.py` share it without an import cycle. A
  one-node list that arrives as a plain dict (the SOAP shape of a single ESXi
  host) is read too.
- **list.** A missing VM is `not_found`. A `snapshot` property vCenter could
  not read (a `missingSet` entry) fails the call: an empty list would wrongly
  say "no snapshots".
- **create.** It uses `CreateSnapshotEx_Task`, because the pinned files mark the
  plain `CreateSnapshot_Task` deprecated since vSphere 8.0. `quiesce` sends an
  empty `VirtualMachineGuestQuiesceSpec`, like `govc snapshot.create -q`. A VM
  that already has a snapshot with the name gets none (`unchanged`, with that
  snapshot's id), so a retry after a timeout does not take a second snapshot.
  After the task it reads the tree again and returns the new row. A task fault
  raises (`connector_error`); a poll timeout returns `timeout` with an `error`
  issue.
- **delete.** One plan feeds the approval card and the handler (the
  `_teardown.TeardownPlan` seam). An unknown VM is `invalid_request`; a
  snapshot id that is not in the VM's tree is `unchanged`. The approval card
  shows the VM and the snapshot (name, description, time taken, saved power
  state, current or not, `child_count`). `children` lists the snapshots below
  it only when `remove_children` is set, because only then do they go too.
  `consolidate` keeps its vendor default (true). `deleted` only when a new read
  no longer shows the snapshot.
- **revert.** A `snapshot_id` must be in the VM's own tree, else `not_found`.
  The approval record names the snapshot by name too. A name that two
  snapshots share is still `ambiguous`; the caller then passes the id.
- **Approval.** Create waits for approval for every caller, a person's call
  too: every VMware write composite, and the CI operation-contract check, need
  `requires_approval=True`. Delete needs a preview and a second person, and an
  agent is refused. Revert waits for approval.
- **Single ESXi host.** The SOAP path of a single ESXi host has no request body
  for the three snapshot tasks, so create, delete and revert stop there with an
  error. The list uses the supported `RetrievePropertiesEx`, but no test covers
  it on SOAP.
- **Not here** (Task #4334): the snapshots of all VMs in one call, an "older
  than N days" filter, the size on disk, delete all snapshots, and disk
  consolidation.

Code: `typed_ops_snapshot_list.py`, `snapshot_tree.py`, `composites/_snapshot.py`
(create and delete) and `composites/_write.py` (revert). The governed sub-op
manifests are in `_governed_subops.py`.

### AI guidance of the host and cluster operations

The twelve host and cluster multi-step operations (`host.evacuate`,
`host.detach_from_vds`, `host.datastore_mount_nfs`, `host.disk_mark_flash`,
`host.service_control`, `cluster.drs_recommendations`, `cluster.patch`, the
three DRS rule operations and `resource_pool.create` / `.delete`) keep their
AI guidance (`llm_instructions`: `when_to_call`, `preconditions`,
`output_shape`, `next_step`) in `composites/_host_cluster_llm_instructions.py`,
one entry per op id. Their descriptions in `_register.py` stay short. Each
parameter schema has one example call under `examples`, and
`test_connectors_vmware_rest_host_cluster_composite_contracts.py` checks that
every example validates. `resource_pool.create` takes exactly one of `parent`
and `cluster`; the schema says so with a `oneOf`, so MEHO refuses both or
neither before the call.

### Governed NFS tag-based SPBM storage policy (`storage_policy.*`, #3494)

NFS-principal datastores have **no default VM storage policy** (`vSAN Default
Storage Policy` is vSAN-only), so a tag-based SPBM policy must be minted before
a vSphere Supervisor can be enabled (its `EnableSpec` requires
`master_storage_policy` / `ephemeral_storage_policy` / `image_storage`, all
policy ids) and before a vSphere Namespace / VKS guest cluster can bind
storage. Creating it out of band (`govc` / PowerCLI `New-SpbmStoragePolicy`) is
exactly the escape from policy / audit / approval the dogfooding story cannot
have, so three composites (`group_key="storage"`) close the gap:

- **`storage_policy.list`** — a read (`safe`, no approval) over
  `GET /vcenter/storage/policies`; the set-shaped `policies` list is
  JSONFlux-reduced to a result handle by the dispatcher when large.
- **`storage_policy.create`** (`safety_level="caution"`, `requires_approval=True`)
  — the first link in the Supervisor-enable chain. Fans out, each child
  gated through `enforce_subop_policy`: resolve every `datastore_names` entry
  to a moid (`GET /vcenter/datastore`, fail-closed `datastore_not_found`
  before any write if one resolves to zero / many) → ensure the tag
  **category** (MULTIPLE cardinality, associable to `Datastore`) → ensure the
  **tag** in it → **attach** the tag to each datastore
  (`POST /cis/tagging/tag-association/{tagId}?action=attach`, `object_id` =
  `{id: <datastore-moid>, type: "Datastore"}`) → mint the **PBM policy**
  whose one rule requires the tag → read-back `GET /vcenter/storage/policies`.
  Returns the policy id (the vCenter `StoragePolicy` identifier).
  **Resolve-before-create idempotency (#3826):** each sub-step adopts an
  existing object keyed by name instead of blindly creating (which used to make
  a retry after a partial run die at `POST /cis/tagging/category` with
  `ALREADY_EXISTS`). Category: `GET /cis/tagging/category` + per-id
  `GET /cis/tagging/category/{categoryId}` matched on `name` — reused when its
  cardinality / `associable_types` are compatible, else terminal
  `category_conflict`. Tag: the same id-first walk over
  `GET /cis/tagging/tag` + `/cis/tagging/tag/{tagId}`, matched on `name` **and**
  `category_id`. Attach is idempotent (a re-attach is a no-op), so it needs no
  read-before-write. Policy: an existing policy of the same name whose tag-rule
  signature (`pbm_retrieve_profile_tag_rules`, an `error`-severity-free read of
  `PbmRetrieveContent`) matches the one this op would mint is **adopted**
  (`status="adopted"`, no `PbmCreate`); a different rule set is terminal
  `policy_conflict`. Adopted sub-steps are reported in the response `adopted`
  map (`{category, tag, policy}` booleans) and as `issues[]` `warning`s; the
  fresh path (nothing pre-exists) is unchanged. The terminal
  `category_conflict` / `policy_conflict` and the `PbmCreate`-returned-no-id
  `policy_create_failed` all carry an `error`-severity `issues[]` entry so the
  dispatcher surfaces them as dispatch errors (#3812) rather than a false `ok`;
  `policy_create_failed` now says to **retry** (the ensured category / tag are
  adopted — no orphan to clean up). The caution-tier park preview resolves the
  same live state read-only and labels each sub-step `create` / `adopt` /
  `conflict` (`exists` for the policy) so the approver sees what will actually
  happen.
- **`storage_policy.delete`** (`safety_level="destructive"`,
  `requires_approval=True`) — issues PBM `PbmDelete` for the id, then read-backs
  the policies list; a per-id `PbmDelete` fault (e.g. the policy is in use)
  returns `delete_failed` with the fault type (an in-use policy must be freed
  first — no force). Deletes only the policy; the tag / category are left in
  place (they may be shared).

**Two transports, one op.** The tag substrate rides the generic REST sub-op
seam (`_write_sub_op` → `connector._post_json`, the tag rows are ingested
`vcenter.yaml` paths). Policy **creation has no vCenter REST equivalent** — the
spec exposes only `GET /vcenter/storage/policies` (read) — so it rides the
**PBM SOAP** API (`PbmProfileProfileManager.PbmCreate` / `PbmDelete` on `/pbm`,
namespace `urn:pbm`). PBM is a distinct SOAP service from the vim25 `/sdk`
codec: `soap_pbm.py` carries its envelope builders + parsers (reusing the
vim25 codec's namespace-agnostic low-level helpers), and the connector's
`_ensure_pbm` mints a **separate** vim SOAP session — a `SessionManager.Login`
on `/sdk` for the `vmware_soap_session` cookie value, then
`PbmRetrieveServiceContent` on `/pbm` for the `profileManager` moid
(`_soap_post` gained a `path` argument so the same span-recording,
credential-free wire helper serves both `/sdk` and `/pbm`). **vCenter's PBM
endpoint authenticates a request by the vim session cookie value carried in a
`vcSessionCookie` SOAP `<Header>` element — NOT the HTTP `vmware_soap_session`
cookie** (which `/pbm` ignores) and not the vAPI `vmware-api-session-id` token
the REST/VI-JSON path uses. A request that presents only the HTTP cookie faults
`NotAuthenticated` on any authenticated method (`#3810`, live-proven: the
canonical pyvmomi/govmomi path sets this header —
`GetRequestContext()["vcSessionCookie"]` / `soap.Client.Cookie` — and succeeds,
while the same call without it faults). So every PBM envelope
(`build_vc_session_cookie_header` in `soap_pbm.py`) carries the vim cookie value
as that header, and every PBM method funnels through `_pbm_call`. With the
header present, a `NotAuthenticated` fault means the underlying vim session
expired server-side (the session-liveness signal the vim25 read path also
recovers, `#3773` / `#3776`), so `_pbm_call` re-logs in for a fresh cookie
(`_invalidate_pbm` + `_ensure_pbm`) and retries once against the fresh
`profileManager` moid. An `InvalidLogin` / `NoPermission` fault is a genuine
credential / privilege rejection and is **not** retried (`ConnectorAuthError`).
If `/pbm` still rejects a request that carried a valid `vcSessionCookie` header
after a fresh login, that is a connector-side PBM transport/auth defect, not a
credential problem — surfaced as a `RuntimeError` (not a `ConnectorAuthError`)
that says do-not-restage, because the same credential authenticates the
target's vim25 and REST sessions. The tag-rule
create-spec shape (namespace `http://www.vmware.com/storage/tag`, property id
`com.vmware.storage.tag.<category>.property`, subprofile "Tag based placement",
`category=REQUIREMENT`, `resourceType=STORAGE`, with the `xsi:type`
discriminators vmomi requires on the polymorphic `constraints` / `value` /
`values` slots) is grounded on govmomi `pbm` + `govc storage.policy.create` +
the community `vmware_vm_storage_policy` Ansible module. The `storage_policy.*`
group's REST paths are pinned against the vendor spec by
`test_connectors_vmware_rest_storage_policy_reconcile.py` (the tag / datastore /
policies paths must be served; the `/pbm` keys must **not** be — proving they
have no REST equivalent).

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
and friends). vSphere Automation REST — no `pyvmomi`. The seven REST
sub-ops (two finds, the OVF summary, the deploy, power-on, and the cleanup
listing + delete) are `vcenter.yaml`-served paths, so they reconcile
through the generic `_SUB_OPS_VM_DEPLOY_FROM_LIBRARY` sweep. The vim legs
— the `Rename_Task` that gives the finished VM its requested name (#3697,
no REST rename exists), the vApp `Destroy_Task` that cleans up a failed
vApp deploy, and the property read that polls them and finds such a vApp
— ride the governed VI-JSON seam and live in the
`_VIM_SUB_OPS_VM_DEPLOY_FROM_LIBRARY` lane.

**Item resolution.** The caller gives exactly one of `library_item` (an id)
and `library_item_name`. The parameter schema says so with a `oneOf`, so
MEHO refuses both or neither before the call; `vm.import_from_library`
has the same rule. `library_item` is a passthrough. `library_item_name` is
resolved to an id via
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
`PropertyParams` entry in `additional_parameters`. An optional
`deployment_option` (the OVF deployment option key, for example the size
profile of an appliance) goes into the same list as a
`DeploymentOptionParams` entry with `selected_key` (#4035). Before the
deploy, MEHO reads the OVF summary (`?action=filter`) and checks the key
against `additional_params[type=DeploymentOptionParams].deployment_options[].key`.
A key the item does not offer is refused with `invalid_reference`, and
`candidates` lists the offered keys. One summary read serves both this
check and the default VM name. The EULA-accept wire
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

**No partial VM after a failed deploy (#3697).** vCenter's own rollback of
an interrupted deploy depends on timing: the copy can also finish, or stop
half-way, after the caller is gone. So the composite cleans up itself, the
same guarantee `vm.import_from_library` gets from aborting its lease. The
REST VM listing cannot show which deploy created a VM, so the composite
gives its VM a name nobody else uses:

1. The final name is `name`, or — when `name` is not set — the OVF
   descriptor's default name, read before the deploy from the un-gated OVF
   summary (`POST:/vcenter/ovf/library-item/{id}?action=filter`,
   `OvfSummary.name`). A faulted summary read is `resolve_error`; a
   descriptor without a name is `invalid_reference` ("set `name`"). Both
   refuse before any VM exists.
2. The deploy runs under the unique deploy name `<name>-meho-<token>`
   (`_deploy_vm_name`, ten random hex characters, cut to vCenter's
   80-character VM-name limit). While the copy runs, vCenter shows this
   name, and an adopt-by-name consumer does not see a half-copied VM under
   the requested name.
3. When the deploy call faults (`deploy_error`), answers with a body that
   is not JSON (also `deploy_error`: the result is unknown, so the VM may
   exist), reports `succeeded=false` (`deploy_failed`), or is cancelled, the
   composite looks the deploy name up twice, both un-gated reads: the VM
   listing (`GET:/vcenter/vm`), and the target resource pool's child
   resource pools (`RetrievePropertiesEx` on `ResourcePool.resourcePool`,
   then the `name` of every `VirtualApp` child). The second lookup matters
   for an OVF with several VMs: vCenter deploys it as a **vApp** that
   carries the deploy name, while its VMs carry the OVF's own names, so the
   VM listing alone would miss them. Every VM found, plus the entity the
   `DeploymentResult` names (a `VirtualMachine` or a `VirtualApp`), is
   removed: a VM with the best-effort `DELETE:/vcenter/vm/{vm}`
   (`_delete_leftover_vm`), a vApp with its own governed vim
   `POST:/VirtualApp/{moId}/Destroy_Task` (`_destroy_leftover_vapp`,
   task-polled), which destroys the vApp and every VM in it. An older VM
   with the requested name, or a same-name VM another caller created
   meanwhile, never carries the deploy name, so it is never touched.
4. Each VM or vApp gets a `cleanup` issue: `info` when it was removed,
   `warning` when the removal faulted, was denied, or waits for approval
   (the message then names the approval request). A faulted lookup (a
   transport fault, an unavailable VI-JSON API, or a response that is not
   JSON) is a `warning` that names the deploy name (and the pool, for the
   vApp lookup), so the operator can look for it by hand. The other lookup
   and the removals still run, so a VM the listing already found is still
   deleted. A removal whose response is not JSON is a `warning` for that
   entity, and the other removals still run.
5. On every failure path the cleanup runs as a shielded task (the audit
   middleware's drain pattern, `_drain_deploy_cleanup`): a caller cancel
   during the lookups or the removal does not stop it. A cancel seen during
   the cleanup is re-raised once the cleanup is done, and the cleanup
   outcome is logged because no envelope reaches the caller. A cleanup
   that raises anything the steps above do not catch becomes a `cleanup`
   warning; it never replaces the deploy failure and never hides a cancel.
6. A success renames the VM (or vApp) to the final name through the
   governed vim `Rename_Task` (`POST:/VirtualMachine/{moId}/Rename_Task` or
   `POST:/VirtualApp/{moId}/Rename_Task`, task-polled, 600 s bound). The
   rename is best-effort like the power-on: a parked/denied gate, a transport
   fault, an unavailable VI-JSON API, a response that is not JSON from the
   rename or from its task poll, a task fault (for example the requested
   name now exists) or a poll timeout leaves `status='deployed'` with a
   `rename` warning that names both names. A success issues no listing and
   no delete.
7. **An approval never deploys twice (#4217).** A governed write that
   waits for approval records this composite on its approval row, and
   approving it runs the composite again (#3351). Each write saves on the
   row where that re-run continues (`resume_state`, read back with
   `composite_resume_state`):
   - The deploy saves its resolved deploy parameters (the library item id
     and the deploy body under its unique deploy name) and the final VM
     name (`"deploy"`). The re-run sends exactly those and resolves
     nothing again, so the approved deploy matches its stored parameter
     hash even when `library_item_name` would now find another item.
   - The rename and the power-on save the deployed VM and the step
     (`"deployed"`). The re-run goes straight to `_finish_deploy` at that
     step, on the VM that exists. A rename that waits ends the first run
     before the power-on; the power-on then runs after the rename is
     approved, and a `power_on` warning says so.
   - A cleanup removal saves the failed deploy (`"failed"`) and the one
     object it removes (`removal`: id and type). The re-run goes straight
     to `_resume_failed_deploy_removal`: it removes exactly that object and
     does not look the deploy name up again. So a leftover that was renamed
     meanwhile is still removed, and an object that took the deploy name
     since is never touched. When both a VM and a vApp wait, each approval
     removes its own object.
   These steps are marked `after_main_effect`, so the seam refuses any
   earlier write a wrong re-run would repeat, and they may wait for
   approval again during a re-run (see "Continuing where it stopped" in
   `docs/codebase/approvals.md`). An approval row with no saved state (one
   written before #4217), or with a state the composite cannot continue
   from (a deploy without its parameters, a removal without its object),
   is refused before any read or write (`_refuse_deploy_resume`,
   `composite_resume_unsupported`).

Operator note: the rename and the cleanup removals are governed writes
with their own ops (`POST:/VirtualMachine/{moId}/Rename_Task`, the
`VirtualApp` form, `DELETE:/vcenter/vm/{vm}` and
`POST:/VirtualApp/{moId}/Destroy_Task`), listed on the governed-subops
discovery surface. A service principal that runs the deploy unattended
needs a grant for the rename op (and the power-on op when it asks for
`power_on`). Without one, the step waits for approval: the VM keeps its
deploy name, or stays behind, and the envelope names the approval
request. Approving that request runs only that step on the existing VM
(see point 7). The op's description says this too.

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
slower fabric can outrun any fixed window. On the synchronous path the caller
must also hold the connection open for the whole copy. If the caller gives up
and disconnects, MEHO does **not** stop the deploy: the server does not cancel
the request, and MEHO lets the vCenter POST finish so no half-applied change
is left behind. The caller just never sees the answer. MEHO notices the
disconnect and writes the time into `payload.caller_gone_at` on the audit
rows written after it, so the finished deploy's audit row shows that nobody
was waiting (#3696; see `operations/caller_gone.py`). Both problems are
cured by dispatching the deploy in **async governed mode**
(`POST /api/v1/operations/call` with `async: true`, or the `call_operation`
meta-tool's async flag; #3079 / PR #3201): the dispatch runs on a durable
background task that owns the vCenter POST, the route returns `202` + a run
handle (`GET /api/v1/operations/runs/{handle}` polls it, and the completed
`DeploymentResult` envelope is persisted on the run row), and because that task
is spawned via `asyncio.create_task` — independent of the request handler
task — a dropped caller loses nothing: the outcome waits on the run row.
**Async dispatch (or the typed `vm.import_from_library` below) is the supported
route for any deploy that can take more than a few minutes.** The synchronous
path stays the default for small items but is bounded by the ceiling above; an item large enough to outrun even 3 h uses the durable
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

**Power-on after the import (#4217).** With `power_on`, a best-effort
`POST:/vcenter/vm/{vm}/power?action=start` follows a successful import.
When it waits for approval, it saves the import's envelope on the approval
row and is marked `after_main_effect`. Approving it re-runs the composite,
which then only powers on the imported VM (`_power_on_imported_vm`): no
library lookup, no second import. An approved power-on with no saved
envelope (a request saved before #4217) is refused before anything runs
(`composite_resume_unsupported`).

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
governed dispatch** (#3079) so a dropped caller does not lose the outcome.

**Live-appliance verification is deferred** (no lab access at implementation
time): the deliverable is the implementation + full conformance/unit coverage,
consistent with how every recent connector task shipped. The vim methods,
content-library download-session paths, and every emitted `_typeName` are
grounded against the pinned `vcenter.yaml` / `vi-json.yaml` by the reconcile
lanes (`..._l2_ingest_reconcile` + `..._write_body_reconcile`); the engine +
source mechanics are proven by mock-transport unit tests
(`test_connectors_vmware_rest_ovf_import.py` /
`test_connectors_vmware_rest_library_download.py`).

### Copy helper: vCenter download to a destination URL (`_transfer.py`, #4343)

`_transfer.copy_files` copies files from a vCenter download (a content
library download session, a host configuration download, a datastore file)
to a destination URL that accepts HTTP `PUT`. It adds no operation. The
content library export, the host configuration backup and restore, and the
datastore file export use it. Other connectors keep their own upload code.

The calling operation owns the vCenter side. It passes the source files
(name, download URL, expected size when known), the destination
(`Destination`: an `https://` base URL plus a Vault reference to its token),
a keep-alive callback, a clean-up callback, and optionally a callback that
lists and removes leftovers from an earlier run.

Before the first copy it checks every name: a safe path, listed once, and
not ending in `.meho-partial`. MEHO keeps that ending for its temporary
files; a file called `disk.meho-partial` would be removed as a leftover when
`disk` is copied after it.

Per file, one at a time:

1. `HEAD` the final path: `401` / `403` stops (no write access); a file that
   is there stops unless `overwrite` is set.
2. `HEAD` `<name>.meho-partial`: a partial file from an earlier run is
   deleted. A destination without `DELETE` (`405` / `501`) keeps it and the
   new `PUT` replaces it. Any other error answer to this check or to the
   delete stops the run before the upload.
3. Stream the `GET` into a `PUT` of `<name>.meho-partial`, in chunks of at
   most 8 MiB, counting bytes and computing SHA-256. It stops at the per-file
   or total size limit, on a size that does not match, or on an error answer.
   The `GET` sends `Accept-Encoding: identity`, so vCenter sends the raw
   bytes and `Content-Length` is the size the copy counts.
4. `MOVE` the partial file to its final name. A destination without `MOVE`
   (`405` / `501`) keeps the temporary name and the row says `partial`.

Each result row has `name`, `size`, `sha256`, `destination_path` (a path, no
token) and `status` (`done`, `partial`, `failed`). On a stop it raises
`TransferError`, which carries the rows so far.

**Credentials.** No redirect is followed: a `3xx` from the source or the
destination stops the run, so the token goes only to the destination host and
the vCenter session only to vCenter. The destination host passes the same
address screen as a target host (`targets/ssrf_guard.py`, through the
address-pinning transport). Rows, errors and logs never carry the
vCenter download URL or a token; a transport error shows only its class name.
`httpx` logs one line per request with the URL; the production logging
(`meho_backplane/logging.py`) cuts that URL down to its scheme and host.

**Clean-up.** The clean-up callback runs in a `finally` block on success,
error and cancel. When the MEHO process dies, it cannot run. What is left, and
how it goes away:

- the vCenter download session expires after its own timeout;
- a temporary library item or session carries a name from `meho_temp_name`
  (prefix `meho-transfer-`); the next run of the same operation lists and
  removes it through `remove_leftovers`;
- a `.meho-partial` file at the destination is removed or replaced by the
  next run. It never looks like a complete file.

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
whole `configurationEx` via `RetrievePropertiesEx` (un-gated) and take the
existing rules from its `rule` array for the idempotence / name-collision
check — a duplicate name →
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

**`folder.create`** creates a VM folder under a parent through vim
`Folder.CreateFolder` — which is **synchronous**: it returns the new Folder
`ManagedObjectReference` directly, **not** a `*_Task`, so (unlike
disk-grow / drs_rule) the returned MoRef is unwrapped into the result and
**never polled** (`poll_vim_task` is not called). The parent is given in one
of two ways (#3588, the `vm.create` `folder_name` / `folder` shape from #3115):

- `parent_folder` — a display name, resolved to a moid with
  `GET:/vcenter/folder?filter.names&filter.type=VIRTUAL_MACHINE` (un-gated).
  An optional `datacenter` moid adds `filter.datacenters`. Every datacenter
  has a root VM folder named `vm`, so on a multi-datacenter vCenter the bare
  name `vm` is ambiguous without `datacenter`.
- `parent_folder_id` — a `group-v…` folder ID (moid). The lookup is skipped.

No match → `status='parent_not_found'`, more than one → `ambiguous_parent`
with `candidate_folders`. Both happen before any write and carry an
`error`-severity issue, so the dispatcher's composite terminal-error check
fails the operation and audits `error` — before #3588 they audited `ok` for a
write that never happened. The issue message names only the match count;
the ids ride `candidate_folders`, which the dispatcher's reducer puts behind
a result handle when the envelope is oversized (#4223), so a vCenter with
hundreds of same-named folders never ships the whole list inline. Then the single `CreateFolder` goes through
`_write_vmomi_sub_op`. The preview is a param echo `{parent_folder |
parent_folder_id, datacenter?, new_folder_name}` (no I/O — the params fully
name the blast radius). Both
mutating writes flow through the same `enforce_subop_policy` gate the REST
and disk-grow writes do, so an agent principal without a grant is denied and
a policy-parked write never reaches the wire.

### vim distributed-portgroup writes (`network.portgroup.create` + `network.portgroup.security.set`, #3091; `network.portgroup.vlan.set`)

Three vim-only distributed-portgroup writes for standing up an L2 substrate —
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
access VLAN (`VmwareDistributedVirtualSwitchVlanIdSpec`). The parameter
schema refuses both (a `not` clause), so MEHO stops such a call before the
handler runs; the handler still answers `status='invalid_vlan_spec'` if it is
called with both. Passing neither inherits the switch default. The returned Task's `TaskInfo.result` MoRef is
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
connector; only `vm.destroy` is `destructive`).

**`network.portgroup.vlan.set`** reconfigures an **existing** portgroup's
`defaultPortConfig.vlan` — the sibling `portgroup.create` sets the VLAN only at
create time, and nothing else could change it (an operator who created a trunk
without the native/untagged VLAN 0 had no governed way to add it). It takes the
same VLAN-spec choice as `create` — a trunk
(`VmwareDistributedVirtualSwitchTrunkVlanSpec`, a `NumericRange[]`) **or** a
single access VLAN (`VmwareDistributedVirtualSwitchVlanIdSpec`), rendered by the
**shared** `_build_portgroup_vlan_spec` helper both ops call — and writes it
through the same vim `ReconfigureDVPortgroup_Task` seam as `security.set` (the
required `configVersion` is read + echoed first). The given spec **replaces**
the current VLAN config; there is **no merge**, so to add VLAN 0 to an existing
trunk the operator passes the full desired range list (e.g.
`[{start:0,end:0},{start:3251,end:3271}]`) and `replace=false` is refused. It is
**idempotent**: the current VLAN is read first and normalised (ranges sorted +
merged-adjacent, so `[{0,0},{1,4094}]` and `[{0,4094}]` compare equal) and a
request that already matches returns `status='unchanged'` with no
`ReconfigureDVPortgroup_Task`; otherwise the reconfigure is polled to terminal
and the applied VLAN is read back into the `before` / `after` normalised views.
The parameter schema takes exactly one VLAN mode (a `oneOf`), so MEHO refuses
both or neither before the handler runs; the handler still answers
`status='invalid_vlan_spec'` for both, neither or `replace=false`.

None of the three composites registers a park-time preview builder (matching
`vm.import_from_library`); the read-back rows in each response
(`observed` for `create`, `previous` / `observed` for `security.set`,
`before` / `after` for `vlan.set`) are the verification surface.

### AI guidance of the storage and network operations

The thirteen storage and network multi-step operations (`datastore.usage`,
`datastore.refresh`, `datastore.dir.create`, `datastore.file.delete`, the three
`storage_policy.*` operations, `network.portgroup.audit` / `.create` /
`.security.set` / `.vlan.set` / `.delete` and `host.standard_portgroup.delete`)
keep their AI guidance (`llm_instructions`: `when_to_call`, `preconditions`,
`output_shape`, `next_step`) in `composites/_storage_network_llm_instructions.py`,
one entry per op id. Their descriptions in `_register.py` stay short. Each
parameter schema has one example call under `examples`, and
`test_connectors_vmware_rest_storage_network_composite_contracts.py` checks that
every example validates and that the two VLAN rules above hold. The response
schemas name the nested fields: the `object` and `blockers` of the four teardown
operations, the `observed` VLAN of `portgroup.create`, the security values of
`portgroup.security.set` and the VLAN views of `portgroup.vlan.set`.

### Supervisor (WCP) namespace-management composites (`supervisor.enable` / `supervisor.disable` / `supervisor.status`, #3281)

`composites/_supervisor.py` (group `namespace_management`) governs vSphere
Supervisor (Workload Control Plane) lifecycle on a cluster — the governed
replacement for out-of-band `govc` / `kubectl-vsphere` enablement. Three ops:

**`supervisor.enable`** (`dangerous` / `requires_approval=True`) issues
`POST:/vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster`
— the current 9.x path. The 7.x `clusters/{cluster}?action=enable` form is
**deprecated as of vSphere 9.0** and is not used. The body is the nested
`EnableOnComputeClusterSpec` (`name` + `control_plane` + `workloads` +
optional `zone`), passed as the **top-level** POST body verbatim (the
top-level-`*Spec` envelope convention documented above — never wrapped in
`{"spec": {...}}`); `{cluster}` is the only path var, so the composite's
`params` carry `cluster` plus the spec sub-objects and `_split_sub_op` lifts
the remainder into the body. The handler validates the spec **server-side
before any write**: `control_plane` must carry a management `network` and a
`storage_policy` (on NFS there is no default SPBM policy), and the workload
network stack — `workloads.network.network_type` (`VSPHERE` / `NSXT` /
`NSX_VPC`) paired with `workloads.edge.provider` (`VSPHERE_FOUNDATION` / `NSX`
/ `NSX_VPC` / `NSX_ADVANCED` / `HAPROXY`) — is checked against the
authoritative 9.x enums and an unknown value is **refused loudly**
(`status='unknown_network_provider'`, nothing reaches the wire, the gate is
never consulted). The VDS + Foundation LB model is `VSPHERE` +
`VSPHERE_FOUNDATION` (no separate NSX edge cluster required); the NSX VPC
model is `NSX_VPC` + `NSX_VPC`. Enable is **asynchronous**: vCenter returns
the new Supervisor id immediately (the handler returns
`status='enabling'`), and the composite deliberately does **not** block the
dispatcher polling the 30–60 min `CONFIGURING` → `RUNNING` convergence —
readiness is the separate `supervisor.status` op.

**`supervisor.disable`** (`dangerous` / `requires_approval=True`) issues
`POST:/vcenter/namespace-management/clusters/{cluster}?action=disable`
(`DELETE clusters/{cluster}` 404s — the action form is the documented
teardown). No request body; removes the control-plane VMs + worker nodes but
leaves the cluster's networking / zone intact for a fresh re-enable.
Asynchronous (`status='disabling'`; poll `supervisor.status`, `config_status`
moves through `REMOVING`).

Both writes ride the standard governed REST sub-op seam
(`_write._write_sub_op` → `enforce_subop_policy` → `_post_json`): the
top-level composite's own `dangerous` / `requires_approval=True` posture pops
the human approval park at dispatch, and the governed child POST re-applies
policy under the shared `dangerous` / `requires_approval=False` sub-op posture
(`_governed_subops._GOVERNED_SUBOP_MANIFEST` publishes each write's grant set).
Both register echo-only park-time preview builders (`_write_preview`) — enable
echoes cluster / name / size / count / network stack, disable echoes the
cluster + an irreversibility marker.

**`supervisor.status`** (`safe` / `requires_approval=False`) reads
`GET:/vcenter/namespace-management/clusters/{cluster}` and reshapes
`Clusters.Info` into a **poll-friendly, inline** envelope: the scalar
`config_status` (`CONFIGURING` / `REMOVING` / `RUNNING` / `ERROR`),
`kubernetes_status` (`READY` / `WARNING` / `ERROR`), and a derived `ready`
flag (`RUNNING` AND `READY`) stay top-level so a runbook `OperationCallVerify`
step or a Sensor assertion can poll `ready == true` / `config_status ==
'RUNNING'` **directly**, never behind a JSONFlux handle. The `messages` +
`conditions` arrays (which can grow during `CONFIGURING`) are capped inline
(`messages_limit`, default 25); `message_count` / `condition_count` carry the
uncapped sizes.

The three op_ids are byte-for-byte the strings the ingest parser emits from
the pinned `vcenter.yaml`; the reconcile guard
`tests/acceptance/test_supervisor_op_id_reconcile.py` pins them (always-on
string test + spec-backed parse).

### Governed vSphere Namespace composites (`namespace.create` / `namespace.delete` / `namespace.status`, #3502)

`composites/_namespace.py` (group `namespace_management`) governs the vSphere
Namespace lifecycle on an enabled Supervisor. A vSphere Namespace is the
container a VKS guest cluster is created into; it binds an SPBM storage policy
(`#3494`), VM classes, and a TKr/VKr content library (`#3495`). VKS guest-cluster
lifecycle then runs entirely from *inside* the Supervisor via Kubernetes CRDs
(there is no `/tkg/*` vCenter REST family), so the namespace create is the last
vCenter-REST step before the flow moves to `k8s.apply` of a `Cluster` CR on the
Supervisor target. Three ops (two writes + one `safe` status read):

**`namespace.create`** (`caution` / `requires_approval=True`) issues
`POST:/vcenter/namespaces/instances/v2` with the
`Namespaces.Instances.CreateSpecV2` body — `supervisor` (the enabled Supervisor
id) + `namespace` (the DNS-1123 name) are required, and `access_list` /
`storage_specs` (SPBM policy ids from `#3494`) / `vm_service_spec`
(`content_libraries` from `#3495` + `vm_classes`) are optional pass-through
sub-objects. Both are lifted straight into the top-level POST body (the
top-level-`*Spec` envelope convention; there are no path vars). The create is
**asynchronous** vCenter-side (`config_status` moves `CONFIGURING` → `RUNNING`);
the handler read-backs `GET:/vcenter/namespaces/instances/v2/{namespace}` best-effort
to surface `config_status` and returns `status='created'`. A read-back that 404s
(not yet visible) or faults leaves `config_status=None` — the create was still
accepted.

**`namespace.delete`** (`destructive` / `requires_approval=True`) issues
`DELETE:/vcenter/namespaces/instances/{namespace}`, then read-back verifies
absence. Mandatory human approval — the `DELETE` child is never grant-clearable,
so it always parks. Deleting a namespace **cascades**: it destroys every
workload inside it (VKS guest clusters, pods, PVCs), which the destructive-tier
`_write_preview` builder states as the `permanent-cascades-to-workloads`
irreversibility class (the individual workloads are not enumerable through the
vCenter-REST namespace-instances surface, so `children` is an explicit empty
list). Asynchronous read-back matrix: a `GET` that 404s → `status='deleted'`
(the verified happy path); `config_status='REMOVING'` → `status='removing'`
(teardown draining); a non-REMOVING status → `status='still_present'`.

**`namespace.status`** (`safe` / `requires_approval=False`) reads
`GET:/vcenter/namespaces/instances/v2/{namespace}` and reshapes
`Namespaces.Instances.InfoV2` into a compact, inline-pollable envelope: the
scalar `config_status` (`CONFIGURING`/`REMOVING`/`RUNNING`/`ERROR`) + a derived
`ready` flag (`config_status == 'RUNNING'`) + `stats` + `description` + capped
`messages` (`messages_limit`, default 25; `message_count` carries the uncapped
size) stay top-level so a runbook `OperationCallVerify` step or a Sensor
assertion can poll `ready==true` directly — never behind a set-shaped handle.
A `GET` 404 returns `exists=false` (namespace not yet visible mid-create, or
gone after delete) rather than a fault; any other transport / status fault
propagates as `connector_error`. This is the **governed, boot-enabled** poll op
`namespace.create` / `.delete` hand the caller, and it is what their runtime
`guidance` names (rather than the raw `GET` path). **Delivery shape — a
boot-enabled read composite, not the ingested get-by-name row:** the ingested
`GET .../instances/v2/{namespace}` row lands `is_enabled=False` behind
per-deployment operator review, so a caller polling create/delete convergence
on a fresh boot would have no governed read; a typed read composite is
dispatchable at connector import on every deployment — symmetric with
`supervisor.status`.

The get-by-name path is `GET:/vcenter/namespaces/instances/v2/{namespace}`. It returns `Namespaces.Instances.InfoV2` for the Supervisor-backed 9.x surface (including its Supervisor identity and optional zone data).
Both writes ride the standard governed REST sub-op seam
(`_write._write_sub_op` → `enforce_subop_policy` → `_post_json`/DELETE); the
`namespace.status` read and the create/delete read-backs ride the un-gated
`_read_sub_op` read seam. `_governed_subops._GOVERNED_SUBOP_MANIFEST` publishes
each write's grant set. The **list** read
(`GET /vcenter/namespaces/instances` / `.../v2`) is left to the already-enabled
ingested rows, JSONFlux-reduced by the dispatcher; the single-namespace **get**
is wrapped as the boot-enabled `namespace.status` composite above. The three
op_ids (`POST .../instances/v2`, `DELETE .../instances/{namespace}`, `GET
.../instances/v2/{namespace}`) are pinned against `vcenter.yaml` by the reconcile
lane `tests/test_connectors_vmware_rest_namespace_reconcile.py` (always-on shape
test + spec-backed path-existence).

### AI guidance of the Supervisor, namespace, content library and guest customization operations

Fourteen multi-step operations keep their AI guidance (`llm_instructions`:
`when_to_call`, `preconditions`, `output_shape`, `next_step`) in
`composites/_supervisor_library_llm_instructions.py`, one entry per op id:
`supervisor.enable` / `.disable` / `.status`, `namespace.create` / `.delete` /
`.status`, `content_library.delete`, `content_library.item.delete`, the four
`content_library.subscribed.*` operations, `guest.customization_spec.create` and
`vm.customize`. Their descriptions in `_register.py` stay short. Each parameter
schema has one example call under `examples`, and
`test_connectors_vmware_rest_supervisor_library_composite_contracts.py` checks
that every example validates.

Enums come from the vCenter REST spec (`vcenter.yaml` 9.1.1.0 in
`vmware/vcf-api-specs`): `control_plane.size` of `supervisor.enable`
(`Supervisors.ControlPlane.size`, TINY / SMALL / MEDIUM / LARGE) and
`access_list[].subject_type` / `.role` of `namespace.create`
(`Namespaces.Access.SubjectType`, `Namespaces.Access.Role`), and
`workloads.edge.provider` of `supervisor.enable` (`Networks.Edges.EdgeProvider`,
VSPHERE_FOUNDATION / NSX / NSX_VPC / NSX_ADVANCED / HAPROXY, the same set as the
handler's `_ALLOWED_EDGE_PROVIDERS`). The `content_library.subscribed.sync`,
`.status` and `.items.list` schemas have the same `anyOf` as
`content_library.delete`: give `library_id` or `library_name`. A call with
neither is refused before it runs (the handler would answer
`invalid_reference` without calling vCenter); a call with both still passes,
and `library_id` wins. `workloads.network.network_type` stays a free string;
the handler answers an unknown value with status `unknown_network_provider`.
The `windows_*` fields of
`guest.customization_spec.create` are ignored for a Linux spec; a schema rule
there would refuse calls that work today, so the field texts say it instead.

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

### Datastore cache refresh (`datastore.refresh`, #3789)

`vmware.composite.datastore.refresh` is a read composite (safety
`safe` / no approval) that re-probes one datastore's cached
capacity/free-space. ESXi caches an NFS datastore's `capacity`/`freeSpace`
from mount time, so after the export grows `Datastore.summary` keeps the
stale numbers until the host is told to re-probe — a vCenter deploy
precheck then wrongly fails on "free space less than the minimum" though
the export has hundreds of free GB. The ingested REST binding
`POST:/Datastore/{moId}/RefreshDatastore` is routed under `/api` and 400s
on a standalone ESXi target (the `#3534` class), and there was no typed
composite, so operators dropped to SSH/`govc`. This composite closes that
gap through the governed backplane.

Layout — three vim (VI-JSON / ESXi-SOAP) legs on the connector session,
all load-bearing:

1. `POST:/Datastore/{moId}/RefreshDatastore` — the base free-space +
   capacity refresh (void / 204).
2. `POST:/Datastore/{moId}/RefreshDatastoreStorageInfo` — only when
   `storage_info=true` (the deeper refresh incl. per-VM usage; void / 204).
3. `POST:/PropertyCollector/{moId}/RetrievePropertiesEx` — reads the
   refreshed `Datastore.summary` back (bounded single-object read, the same
   seam `datastore.usage` / `object.collect` use), projected to
   `{name, capacity, free_space, accessible, type, url}`.

Both refresh methods are **`System.Read`-privilege** in the pinned
`vi-json.yaml` and change no configuration — hence `safe`, not
`caution`/`dangerous`, and it dispatches through the un-gated `_read_sub_op`
seam like every other read composite (no `enforce_subop_policy` gate, no
governed-subop grant, no proposed-effect preview). The moid is a
`datastore-NNN` moref on vCenter and the `<server>:/<export>` NAS
identifier on a standalone ESXi host, where `_post_vmomi_json`'s esxi guard
carries it in the `<_this type="Datastore">` self-reference over SOAP. The
two POST legs are reconciled against `vi-json.yaml` by
`test_connectors_vmware_rest_composites_read_reconcile.py`.

### AI guidance of the guest, event, performance and typed reads

The six guest operations (`vm.guest.*`), `event.tail`,
`performance.summary` and the seven typed reads (`vmware.host.usage`,
`.host.network_uplinks`, `.host.storage_devices`, `.host.vsan_health`,
`vmware.object.collect`, `vmware.tasks.recent`, `vmware.vm.info`) each
carry `llm_instructions` with `when_to_call`, `output_shape` and
`next_step`, plus `preconditions` for the guest file read and the two guest
writes. The guidance of the eight multi-step operations lives in
`composites/_guest_event_perf_llm_instructions.py`; the typed reads keep
theirs on their `VmwareTypedOp` row. Descriptions stay short. Each
parameter schema has one example call under `examples`.

The guest and performance reads pass vSphere data objects through as
vCenter sends them, so their nested response fields use the VI-JSON names
(`GuestProcessInfo`, `GuestNicInfo`, `GuestStackInfo`,
`GuestFileAttributes`, `PerfMetricId`, `PerfEntityMetric`). Those parts live
in `composites/_guest_perf_response_schemas.py`. `host.usage` reshapes its
answer, so its schema (`_typed_response_schemas.py`) follows the handler.
`test_connectors_vmware_rest_guest_event_typed_read_contracts.py` checks
that every example validates and every list names its fields.
`vm.guest.file.read` stays `caution` without `requires_approval`: an agent's
call waits for approval, a person's call runs at once. That is the one
listed exception for these operations in the contract check's gap list.

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

All write composites ship `requires_approval=True`, so a human/agent
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
| `host.evacuate` | `{host, tolerate_partial_failure, vsan_mode, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `host.detach_from_vds` | `{host, dvs, fallback_network, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `cluster.patch` | `{cluster, resolved, total_resolved}` | live read (`GET:/vcenter/host?clusters=...`) |
| `vm.resize` | `{vm, name, power_state, current, requested}` sizing from->to | live read (`GET:/vcenter/vm/{vm}`) |
| `vm.nic.repoint` | `{vm, name, nic, mac_address, current_backing, requested_backing}` network from->to (`requested_backing` carries `backing_type`, so distributed and standard previews are identical) | live read (`ethernet/{nic}` + `GET:/vcenter/network`) |
| `vm.device.cdrom` | `{vm, name, cdrom, action, current_backing, state}` (the host-local ISO path) | live read (`cdrom/{cdrom}`) |
| `vm.resource_allocation.set` | `{vm, name, current: {cpu_allocation, memory_allocation}, requested}` limit / reservation from->to | live read (vim `RetrievePropertiesEx`) |
| `network.portgroup.delete` / `host.standard_portgroup.delete` / `folder.delete` / `datastore.file.delete` / `content_library.delete` / `content_library.item.delete` | mandatory `blast_radius`: `{object: {..., present}, children, irreversibility, blockers?, refusal?}` -- the handler's own plan (#3339) | live read (vim `RetrievePropertiesEx` / browse, or REST GET) |
| `datastore.dir.create` | `{action, datastore, path, create_parents}` | param echo, no I/O |
| `vm.create` | creation-spec echo (name, guest_os, placement pins — folder_name, folder (#3115), resource_pool, datastore, host (#3096) — sizing, networks, disks_gb (#3117), nested_hv, power-on) | param echo, no I/O |
| `vm.clone` | clone-coordinates echo | param echo, no I/O |
| `vm.deploy_from_library` | deploy-coordinates echo (item ref, placement, network mappings, provisioning, `ovf_property_keys` — **ids only**, never values #1503, `deployment_option`, power-on) | param echo, no I/O |
| `vm.snapshot.revert` | `{vm, snapshot_name}` or `{vm, snapshot_id}` echo | param echo, no I/O |
| `vm.snapshot.create` | `{action, vm, snapshot_name, memory, quiesce}` echo | param echo, no I/O |
| `vm.snapshot.delete` | mandatory `blast_radius` from the handler's own plan: the snapshot, and the snapshots below it when `remove_children` is set | live read (vim `RetrievePropertiesEx` of `VirtualMachine.snapshot`) |
| `vm.migrate` | `{vm, cluster, target_host, target_host_source}` | param echo, no I/O |
| `vm.power` | `{vm, verb, power_kind}` echo (`power_kind` = `hard` vs Tools-soft `guest`) | param echo, no I/O |
| `cluster.drs_rule.create` | `{cluster, cluster_name, rule_type, rule_name, enabled, resolved, total_resolved}` | live read (`GET:/vcenter/vm` + `GET:/vcenter/cluster/{cluster}`) |
| `folder.create` | `{parent_folder` / `parent_folder_id, datacenter?, new_folder_name}` echo | param echo, no I/O |

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
genuinely small action. The read composites register no builder.

### VM moves that copy disks wait for approval and show the copy size (#4749)

A VM move that names a datastore copies the VM's disks to that datastore.
(A datastore is the storage volume that holds a VM's files and disks.) In a
cold move the VM is off for the whole copy, and the copy fills the target
datastore. So every call that can copy disks waits for approval, the same
way the typed `vmware.composite.vm.migrate` waits, and the approver sees the
copy size first.

**Which calls copy disks.** Three operations, on `vmware-rest-8.0` and
`vmware-rest-9.0`:

- the generic relocate `POST:/vcenter/vm/{vm}?action=relocate`, also the
  task form `POST:/vcenter/vm/{vm}?action=relocate&vmw-task=true`;
- the older-API method `POST:/VirtualMachine/{moId}/RelocateVM_Task`
  (VI-JSON), where `vi-json.yaml` is ingested;
- the typed `vmware.composite.vm.migrate` (a host move; it stays
  `dangerous` with `requires_approval: true`, pinned by a test).

**The rule.** `ingest_safety.py` (`is_vmware_vm_relocate`, used by
`_requires_vmware_safety_floor`) puts the generic relocate and
`RelocateVM_Task` in the `dangerous` risk level with `requires_approval:
true`, next to the VM power and hardware writes. The rule is on the
operation, not on the request body: the call waits for approval with or
without a datastore in it. A floor only raises a level; a row that is
already `destructive` stays `destructive`. The rule covers the `/api`
mount and any spelling of the path parameter.

**Running installs.** Rows already in the database change only on the
next ingest. To get the new level, run the catalog ingest again for
`vmware/8.0` and `vmware/9.0` (the same `--spec` ingest of `vcenter.yaml`
and `vi-json.yaml` the install used before). The level is part of the
row's body hash, so only the changed rows are updated.

**The copy-size preview.** `vm_relocate_preview.py` registers one builder
(`vm_relocate_preview`) under every op_id spelling of the generic relocate
and of `RelocateVM_Task`: with and without the `/api` mount, and with each
spelling of the VM id parameter (`{vm}` or `{vm_id}` on the REST relocate,
`{moId}` or `{mo_id}` on the older-API method). A test checks that every
spelling the rule accepts has the builder, so no parked call lacks the copy
size. It reads the REST body (`placement.datastore`,
`disks.{disk}.datastore`) and the older-API body (`spec.datastore`,
`spec.disk[].datastore`), then:

- **No datastore in the call:** the preview says that no disks are copied.
  Nothing is read.
- **A datastore for the VM or for a disk:** two read-only VI-JSON
  `RetrievePropertiesEx` reads, on the connector session like
  `vmware.vm.info` (no catalog ingest needed). The VM read gives the name,
  `runtime.powerState`, the datastores the VM's files are on, and
  `summary.storage` (`committed` = the data copied now; `committed +
  uncommitted` = the most the disks can grow to, for thin disks). The
  datastore read gives the name, capacity and free space of the source
  and target datastores.

The preview is a dict with a plain-language `summary` sentence first,
then `vm`, `name`, `power_state`, `copies_disks`, `copy_scope`
(`whole_vm` / `some_disks` / `none`), `copy_size_is_upper_bound`,
`copied_now_bytes`, `copy_max_bytes`, `source_datastores` (`[{id, name}]`)
and `target_datastores` (`[{id, name, capacity_bytes, free_space_bytes,
free_space_after_copy_bytes, free_space_after_max_copy_bytes}]`). When
only some disks move, or the disks go to more than one datastore, the
copy is *at most* this size and the summary says so. A failed VM or
datastore read never blocks the approval: the preview keeps what it has,
adds `size_unavailable_reason` (redacted like a `preview_error`), and the
summary says why the size is missing. The same holds when the call
succeeds but vCenter could not read one property (a `missingSet` entry,
for example no permission on the storage data): the preview keeps the
rest (name, power state, source datastores, the other datastores) and
`size_unavailable_reason` names the property and the fault type, never
the fault text. The approval row carries the preview under
`proposed_effect.preview`.

`read_vm_copy_size` and `read_datastore_space` are the shared size reads,
so the typed datastore move (#4346) can show the same numbers. Both keep a
successful answer with an unreadable property and list it in
`unreadable_properties`; only `read_vm_copy_size` raises, when the VM is
not in the answer at all.

Tests: `test_connectors_vmware_rest_vm_relocate_approval.py` (the floor on
a hand-made spec for 8.0 and 9.0, the `vm.migrate` pin, the preview; the
shelf-gated lane checks each rule against the pinned 8.0 and 9.0
`vcenter.yaml` and the 9.0 `vi-json.yaml`) and
`test_connectors_vmware_rest_vm_relocate_dispatch.py` (a person's call
waits for approval with the copy size on the row).

### Alarms: list, acknowledge, reset to green (`#4383`, part 1)

What an operator or agent can do now, through MEHO:

- See every alarm that is red or yellow now, for the whole vCenter or under
  one object.
- Acknowledge one alarm on one object, so its actions (emails, SNMP traps)
  stop while someone works on the cause. The alarm stays red or yellow.
- Reset alarms to green after the cause is fixed. This is for alarms whose
  rule has no green condition: they stay red until someone resets them.

| Operation | Risk | vSphere call | Versions |
|---|---|---|---|
| `vmware.alarm.triggered.list` | `safe` | `ManagedEntity.triggeredAlarmState`, `Alarm.info.name` + `.expression`, `ManagedEntity.name` (all through `PropertyCollector.RetrievePropertiesEx`) | vCenter 8.0 U1, U2, U3; 9.0, 9.1, 9.1.1 |
| `vmware.composite.alarm.acknowledge` | `caution`, waits for approval | `AlarmManager.AcknowledgeAlarm` | the same |
| `vmware.composite.alarm.reset` | `caution`, waits for approval | `AlarmManager.ClearTriggeredAlarms` | the same |

**The reads** (`alarm_state.py`, shared by all three operations).
`ServiceInstance.RetrieveServiceContent` gives the root folder and the alarm
manager; their ids are read, never guessed. vCenter copies every triggered
alarm up the inventory tree, so the root folder's `triggeredAlarmState` holds
the triggered alarms of the whole vCenter. Each entry names the object the
alarm is triggered on. Two more reads add the alarm names and rules and the
object names. When one of them fails, or vCenter could not read some names
(a `missingSet` fault), the rows keep their ids and `read_note` says which
read failed. A long list is stored behind a result handle, like every other
list.

**An unreadable list is never an empty list.** vCenter can answer a
`RetrievePropertiesEx` call and still not give a property: the property is
then in the object's `missingSet`, usually with a fault such as
`NoPermission`. When that happens to `triggeredAlarmState`, or the answer has
no entry for the object, the read raises `AlarmStateUnreadableError`. The
list operation then fails (`connector_error`), and so do acknowledge and
reset before they change anything. The read after a change gives
`not_verified`, so a reset never reports alarms as cleared that it could not
read. An unset property has no `missingSet` entry and means no triggered
alarms.

**Every page is read.** vCenter may split a large answer into pages: a
`RetrieveResult` with some objects and a `token`. Every alarm read goes
through the shared paged read `vim_retrieve.retrieve_all_pages` (see "Reads
of many objects read every page" below), which follows each token with
`PropertyCollector.ContinueRetrievePropertiesEx` on the session of the first
call, until a page comes back without a token; at most 100 pages. A failed
page fails the whole read: the list or the change fails, the read after a
change gives `not_verified`, and a failed name read shows in `read_note`, with
no names of that object type used. So a lost page never leaves names or
trigger kinds silently empty.

**Trigger kind.** Each row has `trigger_kind`, from the alarm rule
(`Alarm.info.expression`): `event` (only event conditions), `metric` (only
metric or state conditions), `mixed` (both) or null (not known). The reset
filter needs it.

**Why the reset takes a filter.** The public vSphere API cannot reset one
alarm on one object. vCenter's own manual reset uses
`AlarmManager.setAlarmStatus` (the pinned spec names it once, in the note on
`AlarmState.eventKey`). That call is in neither pyVmomi 8.0.3 nor the pinned
VI-JSON files of 8.0 U1 to U3 and 9.0, so MEHO cannot call or check it.
`alarm.reset` uses `ClearTriggeredAlarms` with an `AlarmFilterSpec`: one or
more statuses (`red`, `yellow`), an object kind (`all`, `host`, `vm`) and a
trigger kind (`all`, `event`, `metric`). It resets every triggered alarm in
the vCenter that matches. An alarm on the vCenter itself, a cluster or a
datastore needs object kind `all`.

**What the reset answers.** The handler reads the triggered alarms before
and after the call:

- `cleared`: the alarms with the asked status and object kind that were
  triggered before and are not now. This can include alarms the caller did
  not mean, so the guidance tells the agent to report them.
- `still_triggered`: matching alarms that are still triggered. Their cause
  is still there, or vCenter did not count them under this trigger kind.
- `unchanged`, and no call to vCenter, when no triggered alarm matches.
  `not_verified`, with an error issue, when the read after the call fails
  or vCenter cannot give the list.

**Acknowledge.** Takes `alarm_id` and `entity_id` from a list row. The
handler checks that the alarm is triggered on that object (else
`not_triggered`, with an error issue), does nothing for an acknowledged
alarm (`unchanged`), sends `AcknowledgeAlarm` with the two object
references, and reads again: `acknowledged` with who and when, or
`not_verified` with an error issue when the read fails or does not show it
acknowledged.

**Risk and approval.** Both changes are `caution` with
`requires_approval=True`, like every other VMware write composite; the
operation contract check requires the flag for every operation that is not
`safe`. A person's call waits for approval. The same person may approve a
`caution` request only where the install allows self-approval
(`APPROVAL_ALLOW_SELF_APPROVAL`); else a second person approves. An agent's
call waits too. Inside the composite, the one change goes through the shared
write seam (`_write_vmomi_sub_op`) like in the other composites, and the
child-op list is in `composites/_governed_subops.py`. The approval request
shows the alarm (acknowledge) or the triggered alarms the filter matches at
that time, at most 25 (reset): the preview builders read vCenter when the call
waits for approval. `preview_operation` (the CLI `meho operation preview`)
does not contact vCenter, so both builders decline there, and the preview
answers `plan_shown: false` with a note that the plan is shown on the
approval request.

**Not covered.** A standalone ESXi host target (not tested; the changes
answer `no_alarm_manager` when the ServiceContent has no alarm manager).
vCenter 7.0 (no `vmware-rest` catalog, and VI-JSON starts at 8.0 U1). Alarm
definitions, and reading or cancelling one task, are the rest of `#4383`.

**Tests.** `tests/test_connectors_vmware_rest_alarms.py` (a fake VI-JSON
seam: the read, both changes, an unreadable list before and after a
change, the previews, `preview_operation`, the rows on both catalogs, the
contract rules, and a reset that waits for approval and then runs once).
`tests/test_connectors_vmware_rest_alarms_reconcile.py` checks the methods,
the filter enum values and the object types against the pinned 9.0
`vi-json.yaml`; the source scan of
`test_connectors_vmware_rest_8_0_spec_reconcile.py` checks the methods
against 8.0 U1 to U3. `AlarmFilterSpec` is in the type-name list of
`test_connectors_vmware_rest_composites_write_body_reconcile.py`.

### Reads of many objects read every page (`vim_retrieve.py`, #4862)

A `PropertyCollector.RetrievePropertiesEx` call that asks for many objects
can come back split into pages: some objects and a `token` for the rest.
This can happen even when MEHO sets no page size (the pinned `vi-json.yaml`
says PropertyCollector policy may still limit the number of objects). Every
read of many objects in one call therefore goes through the shared helper
`vim_retrieve.retrieve_all_pages`. It sends the first `RetrievePropertiesEx`
with a `SessionTokenUsed` that records the vCenter session, then sends
`ContinueRetrievePropertiesEx` with each `token`, pinned to that session
(vCenter accepts a token only there and only once), until a page comes back
without a token. The caller gets one `{"objects": [...]}` answer with the
objects of all pages, so its parser does not change. A failed page fails the
whole read (no objects of the earlier pages are returned), and an answer
that has not ended after 100 pages raises `TooManyPagesError` (a
`RuntimeError`, so a caller that handles a failed call handles it too).
The reads that use it: the recent tasks (`vmware.tasks.recent`), the VM
placement of `datastore.usage`, the mounted-datastore check of
`host.datastore_mount_nfs`, the vApp clean-up of `vm.deploy_from_library`,
the names of blocking objects in the governed deletes (`read_names`), the
datastore read of the VM move preview, and the alarm reads. Each of these
operations lists `POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx`
on its sub-operation manifest where it has one. On a standalone ESXi host
the SOAP transport has no `ContinueRetrievePropertiesEx` builder, so a split
answer makes the read fail there; it never returns a short list. A read of
one object (one VM, one host, a task poll) fits on one page, because vCenter
splits an answer only between objects, so those reads do not use the helper.
`_retrieve_props_by_moid` and `read_many` read in groups of 50 and refuse an
answer that still carries a token, so they never return a short list either.

### Version check for typed operations (`_version_gate.py`, #4316)

Some vCenter calls exist only from a given version on. Examples: the
Supervisor backup and restore jobs, the 9.1.1 Supervisor paths, and the
vSphere 9 host settings. A typed operation that needs such a call uses
`connectors/vmware_rest/_version_gate.py`, so every operation stops the
same way and says the same thing.

**When to use it.** Use it in a typed operation whose vendor call is
missing on some vCenter versions that the connector serves. Do not use
it for a call that every served version has.

**In the handler.** Call `require_vcenter_version(target, op_id=...,
minimum=..., feature=...)` first, before any vendor call. It reads the
target's version with `resolve_target_version` (the probed version
first, then the version the operator set). When that version is lower
than `minimum`, it raises `NotImplementedError`. The dispatcher already
turns this error into `connector_unsupported` (cause
`unsupported_feature`), puts the message in `extras.detail`, and writes
the audit row (#1627). No vendor call runs.

When the version is unknown, or does not parse, the call goes on. The
vendor's own error is then classified as usual.

**The message shape.** Other connectors copy it in their own package:

```
<op_id>: <feature> needs vCenter <minimum> or newer; this target is <version>.
```

For example: `vmware.supervisor.backup.list: Supervisor backup jobs
needs vCenter 9.0 or newer; this target is 8.0.3.`

**In the approval request.** A call that waits for approval reaches the
approval queue before the handler runs. So the approver must see the
problem in the approval request:

- A version-limited change with no preview builder registers
  `version_gated_preview(minimum=..., feature=..., echo=(...))`. The
  preview shows the params named in `echo` (name only params that hold
  no secrets) and, on a target that lacks the call, a `version_problem`
  line.
- An operation that already has a preview builder (for example a
  `destructive` one) calls `add_version_problem(preview, ctx.target,
  op_id=..., minimum=..., feature=...)` on its own result.

The line has the same text as the error above. The handler still
refuses the call after approval.

Tests: `tests/test_connectors_vmware_rest_version_gate.py`.

### vSphere licences (`license.list`, `license.assigned.list`, `license.assign`)

Three operations for the licences of a vCenter (part 1 of #4384). A licence
key is a secret. **No result, log line, audit row, approval request or
flight-recorder span holds a full key.**

| Operation | What it does | Vendor call | Risk |
|---|---|---|---|
| `vmware.license.list` | The licences in the vCenter's licence inventory: name, edition, product, used and total units, cost unit, end date, labels, masked key. | `RetrievePropertiesEx` of `LicenseManager.licenses` | `safe` |
| `vmware.license.assigned.list` | Which licence each asset uses (the vCenter, every host, vSAN clusters), for every asset or one `entity_id`: asset id and name, product, licence name, masked key, evaluation or not, end date. | `LicenseAssignmentManager.QueryAssignedLicenses` | `safe` |
| `vmware.composite.license.assign` | Assigns one licence from the inventory to one asset: a host or the vCenter. | `LicenseAssignmentManager.UpdateAssignedLicense` | `dangerous`; waits until a second person approves |

| Versions | Catalog |
|---|---|
| vCenter 8.0 U1, 8.0 U2, 8.0 U3 | `vmware-rest-8.0` |
| vCenter 9.0, 9.1, 9.1.1 | `vmware-rest-9.0` |

- **How they call vCenter.** Through the VI-JSON path (`_post_vmomi_json`),
  like the other vim reads. VI-JSON exists since vCenter 8.0 U1, so vCenter
  8.0 GA and 7.0 are not served. The `LicenseManager` moId is the fixed
  vCenter singleton `LicenseManager` (like `TaskManager` in
  `tasks.recent`); the `LicenseAssignmentManager` moId comes from its
  property in the same read. So the operations are for vCenter targets only.
  On vCenter 9, licences are usually managed in VCF Operations
  (`vrops.licensing.*`). The guidance of the write says so; if vCenter 9
  refuses the change, the caller gets the vendor fault as an error.
- **The mask.** The shared `mask_license_key` in
  `connectors/_shared/license_key.py` keeps the last 5 characters and turns
  every other letter and digit into `*`; dashes stay, for example
  `*****-*****-*****-*****-AB12C`. A value of 10 characters or fewer is
  hidden in full. It is the same rule `vrops.license.info` and
  `sddc.license.list` use; the vSphere module has no copy of its own. The
  field is `key_masked`: a name with `license` and `key` next to each other
  would be replaced by the secret-name scrubs.
- **Vendor answers.** `LicenseManager.licenses`, `QueryAssignedLicenses` and
  the `UpdateAssignedLicense` answer hold full keys. The handlers build their
  rows with `license_row` / `assignment_row`, which mask the key; a raw
  vendor object never leaves a handler. So the result, a stored result
  handle and the audit row (which keeps the handler's own return) hold
  masked keys only.
- **Flight recorder.** All three operations carry the tag `license-key`. Its
  `key` token is in `SECRET_FAMILY_TAGS`, so `classify_body_exclusion` drops
  the request and answer bodies of every vCenter call they make (the
  `vrops.license.info` way). There is no `classify_op` pin: for a read, a pin
  would also run the secret-name scrub on the caller's copy, which the
  masked rows do not need.
- **The write.** The caller names the licence by `license_name` (letter case
  does not matter), by `key_suffix` (the last 5 characters of `key_masked`),
  or by both. The handler finds the full key in the inventory itself. The key
  is only in the `UpdateAssignedLicense` request body. The gate
  (`_write_vmomi_sub_op` params, and so any approval request of the child op)
  gets `entity_id`, `license_name` and `key_masked`. The park-time preview
  shows the asset's licence now and the one it would get, both masked, plus
  the `outcome`. The parameter schema has `additionalProperties: false`, so a
  key cannot be passed in.
- **Errors of the write.** A fault of `UpdateAssignedLicense` can echo the
  key (the vSphere `InvalidLicense` fault has a `licenseContent` field). The
  handler raises the error as it is, so the dispatcher sorts it as before:
  `connector_auth_failed`, a new login and one retry on a 401, the 403 and
  429 results, `connector_error` with `upstream_fault`. The tag
  `license-key` marks the operation as one that sends a secret, so the
  dispatcher's shared rule replaces the vendor's text in the error result
  and the audit row (#4854, `operations/_vendor_text.py`; see
  `docs/codebase/error-message-shape.md`). The fault type and the HTTP
  status stay. The handler has no masking code of its own.
- **Assign flow.** One property read (inventory and
  `LicenseAssignmentManager`); pick the licence (`license_not_found`, or
  `license_ambiguous` with the masked `candidates`); `QueryAssignedLicenses`
  for the asset (`asset_not_found`; `unchanged` when it already uses that
  licence); then the gated write. Those four statuses stop the call before
  any change and stay `ok` (soft refusals, no `error` issue).
  `UpdateAssignedLicense` is synchronous (no task). The result carries
  `before` and `license`, both masked. The governed child op is
  `POST:/LicenseAssignmentManager/{moId}/UpdateAssignedLicense`.
- **Not in this part.** Adding and removing licence keys (`AddLicense`,
  `RemoveLicense`), and the performance counters and statistics intervals of
  #4384, are still open.

Tests: `tests/test_connectors_vmware_rest_license.py` (fakes, and mocked HTTP
on the vCenter 8.0.3 and 9.0 paths; the dispatcher path from waiting for
approval to the assignment, with a check that no audit or approval row holds
a key; flight-recorder spans, with a control case without the tag),
`tests/test_connectors_vmware_rest_license_reconcile.py` (the pinned 9.0
`vi-json.yaml`) and the source scan in
`tests/test_connectors_vmware_rest_8_0_spec_reconcile.py` (8.0 U1, U2, U3).

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
- **`event.tail` always sends a filter (#3706)** — vCenter 8.0.x answers a
  `QueryEvents` call with no body with HTTP 500 (`InvalidArgument: Invalid
  JSON syntax`). So `event_tail_composite` always sends
  `{"filter": <EventFilterSpec>}`, with a `_typeName` tag on every data
  object. The body is the same on 8.0.x and 9.0; only the
  `/sdk/vim25/{release}` path differs (9.0's optional `eventViewSpec` is not
  sent). Optional params map onto the filter: `begin_time` / `end_time`
  (RFC 3339, checked by a schema pattern because the validator ignores
  `format`) → `time`, `event_type_ids` → `eventTypeId`, `entity`
  (`{moId, type, recursion?}`, recursion defaults to `all`) → `entity`, and
  `max_count` (default 100, max 1000) → `maxCount`. The old client-side
  `max_events` param is gone; the handler still cuts the list to
  `max_count` in case a server ignores `maxCount`. `disableFullMessage` is
  sent as `false`, because the spec says an unset flag drops
  `fullFormattedMessage`. Each event comes back as a short row
  `{createdTime, eventTypeId, fullFormattedMessage, entity}`; `eventTypeId`
  is the `EventEx` / `ExtendedEvent` type id or else the event class name,
  so it can be passed back as a filter value. `entity` is the object the
  event is about: an `EventEx` `objectId` or an `ExtendedEvent`
  `managedObject` wins, and only an event without one falls back to the
  inherited `Event` context members (`vm`, `ds`, `host`, `net`, `dvs`,
  `computeResource`, `datacenter`, most specific first). Rows keep the
  order vCenter returned. `QueryEvents` has no ESXi SOAP builder, so this
  composite does not run against a standalone ESXi host.
- **`performance.summary` request bodies (#4038)** — both vi-json calls
  send `entity` as an annotated `ManagedObjectReference`
  (`vim_body.vim_moref`), never a bare moid string: vCenter 8.0.3 answers
  the string with 500 `InvalidArgument: Unexpected string value`. The
  type comes from the optional `entity_type` input, or else from the moid
  prefix (`host-` HostSystem, `vm-` VirtualMachine, `domain-c`
  ClusterComputeResource); an unknown prefix without `entity_type` fails
  before any call. `QueryAvailablePerfMetric` gets `entity` + `intervalId`;
  `QueryPerf` gets `querySpec: [PerfQuerySpec{entity, intervalId,
  maxSample}]`, the shape the pinned `vi-json.yaml` declares.
- **VI-JSON mount for vmomi reads *and writes* — `/sdk/vim25/{release}`
  (#2466, mutating writes #2893)** —
  vmomi (VI-JSON) methods (`RetrievePropertiesEx`, `QueryEvents`,
  `QueryAvailablePerfMetric`, `QueryPerf`, and now the mutating
  `ReconfigVM_Task`; the vSAN methods have their own path, see "vSAN
  calls" below) are served under the
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
- **Ingested vi-json ops mount on `/sdk/vim25/{release}` too (#3534)** —
  the generic ops ingested from `vi-json.yaml`
  (`GET:/DistributedVirtualPortgroup/{moId}/config`,
  `POST:/PropertyCollector/{moId}/RetrievePropertiesEx`, ...) reach the
  wire through `mount_op_path`. It tells them apart from Automation paths
  by the PascalCase managed-object type in the first segment
  (`_mount.is_vmomi_path`). The type may sit behind one of the five
  service parts the vendor file uses, listed in
  `_mount.VI_JSON_SERVICE_PREFIXES`: `/vsan` (vSAN management), `/sms`
  (storage providers), `/pbm` (storage policies), `/eam` (ESX Agent
  Manager) and `/vslm` (First Class Disks). Before #4775 those paths
  went to `/api` and answered 404, because their first segment is lower
  case. On a modern vCenter session they mount on
  `/sdk/vim25/{release}` (release from the same cached `_about_version`).
  Before #3534 they mounted on `/api` and answered a bare 404 on vCenter
  8.0.x. Limits: there is no `/api` 404 fallback on this path (the hook
  returns one path); when the release can't be derived the op keeps the
  `/api` form; legacy `/rest` (vcsim) and ESXi sessions are unchanged.
  For managed-object property reads, `vmware.object.collect` stays the
  preferred op: it builds the PropertyCollector body for the caller.
- **vSAN calls — `VmwareRestConnector._post_vsan` (#4441)** — every vSAN
  operation sends its vSAN method through this one method. The method
  is a `VsanMethod` constant (`_mount.py`): the vSAN object type, its
  fixed id on vCenter (for example `vsan-cluster-health-system`), and
  the method name. How the call is sent depends on the vCenter version
  the connector detected (the same version that gives `{release}`):
  - **vCenter 9.x: VI-JSON.** `POST /sdk/vim25/{release}/vsan/{MoType}/{moId}/{method}`,
    through `_post_vmomi_json`. `_mount.vsan_method_path` builds the
    `/vsan/...` part; it is the path of the vendor VI/JSON files of 9.0,
    9.1.0 and 9.1.1. Before #4441 the vSAN health read left out `/vsan`,
    and vCenter 9.1 answered HTTP 500 `ManagedObjectNotFound` for the
    vSAN health object.
  - **vCenter 8.0: SOAP.** The vendor VI/JSON files of 8.0 U1, U2 and U3
    have no vSAN paths. So the method goes as SOAP to `/vsanHealth`
    (namespace `urn:vsan`, codec in `soap_vsan.py`). It is authenticated
    by the `vmware_soap_session` cookie of a vCenter SOAP login on `/sdk`
    (`_vcenter_soap_login`, the same login the PBM calls use), sent as
    the HTTP `Cookie`. The `SOAPAction` is `urn:vsan/<version>`, with the
    version from `/sdk/vsanServiceVersions.xml`. Source: the vendor
    sample `vsanapiutils.py` of the vSAN Management SDK for Python 8.0.
    The cookie is kept per target; a `NotAuthenticated` fault (vCenter
    dropped the idle session) makes the call log in again and retry once.
    SOAP has no list marker, so a list with one entry comes back as a
    single dict; the operation turns its list fields back into lists.
  - An unknown version stays on VI-JSON. A legacy `/rest` session
    (vcsim) mounts the same `/vsan/...` path on `/rest`.

  Errors are not swallowed. A vSAN fault raises with vCenter's fault
  type and text (`extras.upstream_fault` in the error answer). When the
  fault is `ManagedObjectNotFound` for an object the caller sent (such as
  the `cluster`), the call raises `ConnectorResourceNotFoundError`, so the
  answer is MEHO's normal `not_found`. A `ManagedObjectNotFound` for the
  vSAN object itself stays a normal error. The reconcile lanes collect
  every `VsanMethod` constant: the read lane checks each against the
  pinned 9.0 `vi-json.yaml`, and the 8.0 lane checks each against the
  method lists of the vendor's vSAN SDK of 8.0 U1 and U2.
- **All hand-authored composites shipped** — T5 (#508) ships 5 read;
  #2080 + #2135 add two more reads (`host.network_uplinks` /
  `host.vsan_health`, later re-shipped as typed ops in #2258); T6
  (#509) ships 8 write composites, #2301 adds a 9th (single-VM
  `vm.power`, incl. Tools soft shutdown), #2893 adds a 10th (the
  mutating VI-JSON `vm.disk.grow`), #2894 an 11th (the folder-template
  `vm.clone_from_template`), #2895 a 12th + 13th (the vim cluster /
  inventory writes `cluster.drs_rule.create` + `folder.create`), and
  #2891 a 14th / 15th / 16th (the post-clone hardware reconfigure trio
  `vm.resize` / `vm.nic.repoint` / `vm.device.cdrom`). Later issues
  added more; the Overview gives the current count. The
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
  step was dropped entirely.
  **#3802 update:** the audit now reads each portgroup's details from
  the portgroup managed object, not from the REST lists. The old
  per-portgroup `GET:/vcenter/vm?filter.networks=...` call was wrong:
  the VM list resource has no `networks` filter, so vCenter ignored it
  and every row got the whole VM inventory. After the
  `GET:/vcenter/network` listing, the composite makes three batched
  vim `RetrievePropertiesEx` reads:
  1. every `DistributedVirtualPortgroup`: `config.distributedVirtualSwitch`
     (the parent switch, gives `dvs`), the whole
     `config.defaultPortConfig` (gives `vlan`), and `vm` (the VMs with a
     network adapter on that portgroup);
  2. the parent switches' `name` (gives `dvs_name`), using the switch
     type from the returned reference (`VmwareDistributedVirtualSwitch`);
  3. the connected VMs' `name` + `runtime.powerState` (gives `vm_names`
     / `vm_count`; powered-on only unless `include_disconnected_vms`).
  Each read sends at most 50 objects per call, so a normal inventory
  is not paged. If vCenter still pages an answer (returns a `token`),
  the audit fails with an error instead of returning rows with missing
  VMs or portgroups. The same happens when a call succeeds but vCenter
  could not read a property on one object (a `missingSet` entry with a
  fault, for example no permission on a VM's `name`): the audit fails
  with an error that names the property path and the fault type name,
  instead of dropping that VM and reporting a wrong count. A property
  that is simply unset (a portgroup with no switch, VLAN or VMs) has no
  `missingSet` entry and gives a null or empty field.
  `vlan` is `{mode: "access", vlan_id}` or `{mode: "trunk", ranges}`
  (the same view `network.portgroup.vlan.set` returns), or `null` for a
  private VLAN. The port config is read whole, never as the deep
  `config.defaultPortConfig.vlan` path: that path is a subclass-only
  field and vCenter 8.0.x answers it with HTTP 500 (#3534 documents the
  same rule for `vmware.object.collect`). `filter_dvs` now works: it
  keeps the portgroups whose parent switch moid or name matches.
  A build-time guard
  (`tests/acceptance/test_portgroup_audit_op_id_reconcile.py`) parses
  the pinned `vcenter.yaml` and asserts every REST (`GET`) audit
  sub-op_id is emitted by the ingest; the vim leg is checked against
  `vi-json.yaml` by the exhaustive read lane. The vendor-licensed spec-shelf is not provisioned in
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
- **No property paths into a cluster's `configurationEx` (#3617)** —
  vCenter 8.0.x rejects `configurationEx.rule`, `configurationEx.group` and
  `configurationEx.drsConfig` in `RetrievePropertiesEx` with HTTP 500
  `InvalidProperty`. The whole `configurationEx` works. The likely reason:
  the vim API declares the property as the base type
  `ComputeResourceConfigInfo`, and `rule` / `group` / `drsConfig` exist only
  on the cluster type `ClusterConfigInfoEx`. So `cluster.drs_recommendations`,
  `cluster.drs_rule.create`, `cluster.drs_vm_host_rule.create` and
  `cluster.drs_rule.delete` read the whole object and pick the member in code
  (`_write._cluster_config_items`; an absent `rule` / `group` array is an
  empty list, because vim leaves empty arrays out). The unit-test fakes
  (`tests/_vmware_cluster_config_fake.py`) answer a dotted
  `configurationEx.<member>` path with the same HTTP 500, so a handler that
  goes back to the dotted path fails in the unit tests. `object.collect`
  passes caller paths through unchanged and documents the rule instead.
- **#2986 read-composite residual + exhaustive read lane** — #2970's
  sweep covered the write composites; the read side's
  `_OP_GET_CLUSTER_DRS = "GET:/vcenter/cluster/{cluster}/drs"` was
  recorded as an adjacent finding (unserved by the pinned
  `vcenter.yaml`, which has **no** cluster DRS REST resource — the
  `/vcenter/cluster` family is list/get/evc-mode only). #2986 switched
  the `cluster.drs_recommendations` DRS leg to vim: one
  `RetrievePropertiesEx` on the `propertyCollector` singleton reading
  the cluster's DRS config (`ClusterDrsConfigInfo`; since #3617 the read
  asks for the whole `configurationEx` and picks `drsConfig` in code) and
  — when
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
- **PBM SOAP path (`storage_policy.*`, #3494) is mock-validated only** —
  the `/pbm` envelope builders + parsers (`soap_pbm.py`) and the
  `_ensure_pbm` vim-SOAP-session-then-`/pbm` establish are unit-tested
  against synthetic `urn:pbm` envelopes grounded on the govmomi / govc /
  Ansible reference shapes, and the REST tag paths are spec-reconciled, but
  the SOAP wire has **not** been exercised against a live vCenter at build
  time (no lab access) — the mock-vs-hardware trap the ESXi SOAP codec
  (#3363) documents. Live-appliance validation (probe → `storage_policy.create`
  on the NFS WLD → Supervisor-enable consumes the id → `storage_policy.delete`)
  is deferred; treat the SOAPAction posture (empty, matching the bootstrap
  posts) and the `xsi:type` discriminators as the first things to re-check if
  a live PbmCreate faults.

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

# Migration assessment package (`connectors/migration`)

## Overview

This package holds the shapes and the operations of the migration
assessment work: MEHO reads the estate a team runs today and turns it
into an Azure migration assessment that people can check.

The first part, in this package today:

- **One inventory shape** for every source platform (`contracts.py`).
- **One assessment result shape** (`contracts.py`).
- **A loader** that reads stored inventories back by handle id and
  merges them (`inventory_load.py`).
- **The Azure Migrate CSV mapping** (`azure_migrate_csv.py`) and the
  read `migration.inventory.csv` (`ops.py`).
- **The source adapters**: one multi-step read per source connector that
  collects the estate into the inventory shape (`source_*.py`, shared parts
  in `adapter_kit.py`, registrar in `source_register.py`).

Like `connectors/audit/`, the package has no connector class. Its
operations are registered under the natural key
`(product="migration", version="1.x", impl_id="migration-assess")`, so the
connector id is `migration-assess-1.x`. The handlers are module-level
functions; the dispatcher calls them with `target=None`. The package
`__init__` queues `register_migration_operations` on the lifespan
registrar list (`register_typed_op_registrar`).

## Key types

### Inventory envelope (`InventoryEnvelope`)

One inventory from one source:

| Field | Meaning |
|---|---|
| `schema_version` | `"1"`. Readers refuse any other value. |
| `source` | `connector_id`, `target` name, `collected_at`. |
| `records` | Records of three kinds, told apart by `kind`. |
| `omissions` | Parts of the estate that were not collected, with a reason. |
| `counts` | Records per kind, plus `source_totals`: the source's own total per kind, for checking. |

The model checks that `counts` match the records and that no
`source_id` repeats.

Record kinds:

- `ServerRecord` (`kind="server"`): `source_id`, `identity_hints`
  (BIOS UUID, MAC addresses, host name, all lower case), `name` (or
  `null` with a reason),
  `server_type`, `hypervisor`, `cores`, `memory_configured_mb`,
  `memory_assigned_mb`, `disks` (size, used space, thin, shared, raw
  mapping, independent, and read/write speeds), `snapshot_count`, `os`
  (name, version, family, architecture), `boot_type` (`bios` or `uefi`),
  `secure_boot`, `power_state`, `nics`, `utilization` (CPU, memory and
  network as average, p95 and maximum, with the window, the number of
  samples and the source) or `null`, `collected_at`, `missing`.
- `ApplicationRecord` (`kind="application"`): name, version, vendor,
  category, and `runs_on` (server `source_id` values).
- `KubernetesWorkloadRecord` (`kind="kubernetes_workload"`): cluster,
  namespace, `workload_kind`, name, replicas, CPU and memory requests and
  limits, images, volumes and the nodes its pods run on.

Rules every record follows:

- **Units are in the field name**: `_mb`, `_gb`, `_mb_per_s`,
  `_millicores`, `_percent`.
- **Unknown is `null`, never `0`.** Every field that is `null` must be
  covered in `missing` with a reason (`not_reported_by_source`,
  `no_permission`, `powered_off`, `not_measured`, `collection_failed`,
  `not_supported`). This includes nested fields and list items: the key
  is the dotted path, for example `os.version`, `disks.0.used_gb` or
  `utilization.cpu_percent.average`. A parent path (`disks.0`, `disks`)
  covers every unknown field below it. The model refuses a record that
  breaks this rule.
- Unknown fields are refused (`extra="forbid"`), so a typo in a producer
  fails at once.

### Assessment result (`AssessmentResult`)

`calculation_version`, `inputs` (the inventory handles with their
connector, target and time, other targets read, and when it ran),
`assumptions` (region, currency, price date, hours per month, license,
reservation, comfort factor, and free `other` values), `items`,
`unknowns`, `omissions`.

Every number is a `Figure`: `value`, `unit`, `basis` (`measured` or
`allocated`; never "right-sized" without measurements), `inputs`, the
names of the `assumptions` it used, `calculation_version`, and
`unknown_reason` when `value` is `null`. The model refuses a figure that
cites an assumption that is not set.

`inventory_json_schema()` and `assessment_result_json_schema()` export the
two shapes as JSON Schema for the operations' response schemas.

## Control flow

### Stored inventories

An inventory read returns one envelope. The envelope has two lists
(`records` and `omissions`), so when it is over the inline size limit the
JSONFlux reducer stores it **whole** as a document record and the caller
gets a handle id (see `jsonflux.md`). `load_inventories(operator,
handle_ids)`:

1. Reads each handle from `get_result_handle_store()` with the caller's
   tenant id. The store also checks the caller, so another tenant's or
   another person's handle is a miss.
2. Fails with one clear message when a handle is unknown or expired (a
   stored result lasts one hour by default), when it holds rows instead of
   one inventory, when `schema_version` is not `"1"`, or when the envelope
   is not valid. Validation messages leave out the input values.
3. Decodes and checks each record in a worker thread
   (`asyncio.to_thread`), because a stored result can be several MB.
4. Merges the envelopes in handle order (`merge_inventories`). A handle
   given twice is read once. A repeated `source_id`, or a server whose BIOS
   UUID was already seen, stays in `records` and is listed in
   `duplicates`; the caller decides what that means. A repeated record's
   BIOS UUID still counts, so a machine whose first record had no BIOS
   UUID is still matched to a later record from another source.

### `migration.inventory.csv`

Group `inventory`, `safe`, `requires_approval: false`. It is a typed
operation, not a multi-step one: it runs no other operation, it only reads
stored results. In: `inventory_handles` (1 to 20). Out: `csv_lines` (the
file, one item per line without the line end, header first), `row_count`,
`columns`, `left_out`, `renamed`, `skipped_records`, `notes`, `sources`.
Join `csv_lines` with CRLF to get the file.

The file is a list of lines on purpose. A large result comes back as a
handle, and `result_query` returns an array in pages (`offset` and
`limit`, up to 500 items a page) but a string only whole, under one output
limit (256 KiB by default). A file of 10,000 servers is over that limit as
one string. Read the lines in pages with `result_query`, the path
`/csv_lines` and `offset` / `limit`; each page holds rows of
`{"value": <line>}`.

The mapping (`inventory_to_csv`):

- Column names are copied from Microsoft's import template
  (`AzureMigrateimporttemplate.csv`, linked from
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>).
  A `*` marks a required column, as in the template. The file has as many
  disk groups (five columns each) as its largest server needs: at least the
  template's two, at most twenty.
- Only servers go into the file. Other kinds are counted in
  `skipped_records`.
- Left out, with the reason: a server without name, cores, memory or OS
  name (`missing_required_field`, with the reasons from `missing`; a name
  or OS name that is empty after cleaning counts as missing); a
  server already in the file through another source, found by BIOS UUID
  (`same_machine_as`); servers past 20,000 (`over_file_limit`). Required
  values are checked first, so when one record of a machine lacks a value,
  a complete record of the same machine from another source goes in.
- Names must be unique, because Azure Migrate updates a server imported
  again with the same name. A name that another server has, or that an
  earlier server already got (`sample` renamed to `sample (x:1)` takes the
  name of a server called `sample (x:1)`), gets its `source_id` added, then
  a counter while it is still taken. Case does not count. `renamed` says so.
- Values are only rounded: the inventory units already match the template.
  Utilization columns take the p95 value. Unknown values stay empty.
- Text from source systems: line breaks become spaces, and a cell that
  starts with `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet
  does not read it as a formula. A changed name is listed in `renamed`
  with `cleaned_text`.
- Line ends are CRLF (RFC 4180); `LINE_END` in `azure_migrate_csv.py`.
  The file has no byte order mark.

### Source adapters

One multi-step read per source, registered on the source's own connector
(not on `migration-assess-1.x`), group `migration`, `safe`,
`requires_approval: false` on purpose:

| Op | Connector | Records | Module |
|---|---|---|---|
| `hyperv.migration.inventory` | `hyperv-ssh-2022.x` | one server per VM | `source_hyperv.py` |
| `proxmox.migration.inventory` | `proxmox-api-8.x` | one server per QEMU VM | `source_proxmox.py` |
| `linux.migration.inventory` | `linux-ssh-1.x` | one server: the host | `source_hosts.py` |
| `winsrv.migration.inventory` | `winsrv-ssh-2022.x` | one server: the host | `source_hosts.py` |
| `k8s.migration.inventory` | `k8s-1.x` | one server per node, one workload per Deployment / StatefulSet / DaemonSet | `source_k8s.py` |

How every adapter works (`adapter_kit.py`):

1. It reads the source only through its connector's own operations, as
   child calls (`dispatch_child`). Each child call has its own audit row,
   linked to the adapter's row. `read_child` takes the child's **full**
   answer from the dispatcher's capture (`full_result_capture_var`, the
   same seam the sensor runner uses), because a large child answer reaches
   the parent only as a summary plus a handle. At most four child reads run
   at a time.
2. A failed child read is an omission with a reason (`no_permission` when
   the child was denied, `collection_failed` otherwise). The item stays in
   the inventory with the affected fields unknown. Only an item that cannot
   be a record at all (a Proxmox template, a node without a name) is left
   out; it is counted in `left_out_items`.
3. `record_with_reasons` gives every null field without a special reason
   the default reason `not_reported_by_source`. Fields MEHO does not read
   for a source are `not_supported`; measurements are `not_measured`.
4. The inventory is **always stored**, also when it is small, as a
   document record of the result handle store (the record kind the
   JSONFlux reducer writes for a large single object, one hour). The answer
   is short: `inventory_handle`, `expires_at`, `connector_id`, `target`,
   `collected_at`, `record_counts`, `source_totals`, `left_out_items`,
   `counts_match`, `omission_count`, `omissions`. So every adapter answer
   has a handle `migration.inventory.csv` can read, and `result_query`
   reads the records (path `/records`). When the `omissions` list makes
   the answer larger than the inline limit (4 KB), the reducer stores the
   omissions as rows. The three count dicts are then kept by a
   `result_objects` hint: inline (every kind listed, a kind without a
   value is null) and whole with the rows, so `result_query` with path
   `/source_totals` reads them back.
5. `counts_match` is true only when the source's own total was read in
   full for every kind the source counts, and records plus left-out items
   equal it. A total that was not read, or was read only in part (an
   offline Proxmox node, a denied or failed workload list), gives `false`
   and no entry in `source_totals`: the inventory was not checked, which
   is not the same as a match.
6. A BIOS UUID is an identity hint only when it can name one machine.
   `usable_bios_uuid` drops a placeholder (all zero or all `f`, which
   firmware without a serial reports) and anything that is not a UUID.
   Two machines with the same placeholder stay two records.
7. Every adapter fills the disk flags the readiness check reads where the
   platform defines them. Hyper-V and Proxmox never leave a disk out of a
   snapshot, so `independent` is `false` on every disk there. The host
   adapters see a machine from inside its OS, where snapshots and the
   hypervisor's disk settings are not visible (`not_supported`); for a VM,
   read it through its hypervisor's adapter to get them.

Per source:

- **Hyper-V.** `hyperv.vms.list` (the source total), `hyperv.vms.guest.list`
  (BIOS GUID and guest OS from the integration services), and per VM
  `hyperv.vms.config`, `hyperv.disks.vm.list`, `hyperv.disks.vhd.get` per
  disk, `hyperv.checkpoints.list`. `source_id` is `hyperv:<VM id>`; the VM
  id stays the same when a VM moves between cluster nodes, so reading every
  node and merging finds each VM once. Generation 1 is BIOS, generation 2 is
  UEFI. Configured memory is the startup memory; assigned memory is what
  the VM has now (it differs with dynamic memory; unknown when off). Disk
  size is the VHDX size, used space is the file size on the host, a shared
  disk is `SupportPersistentReservations`, a pass-through physical disk is
  a raw mapping. Checkpoints count as snapshots; a checkpoint always takes
  every virtual hard disk, so no disk is `independent`.
  Hyper-V allows two VMs with the same name, and the per-VM reads find a VM
  by name, so for a repeated name those reads are skipped (`not_supported`).
- **Proxmox VE.** Only `proxmox.api.get`: `nodes`, `nodes/{node}/qemu`,
  each VM's `config` and `snapshot`, and for a running VM with the guest
  agent on, `agent/get-osinfo`. Cores are cores times sockets. `ovmf` is
  UEFI; Secure Boot is on when the EFI disk has `pre-enrolled-keys=1`.
  The SMBIOS `uuid` and the network cards' MAC addresses are identity
  hints. A snapshot always takes every disk, so no disk is `independent`.
  Templates are counted and left out. A node that is not online, or whose
  VM list fails, makes the VM total unknown (`counts_match` false).
  `source_id` is `proxmox:<target>:<vmid>`.
- **Linux host.** `linux.about` and `linux.file.read` of
  `/sys/devices/system/cpu/online` (cores), `/proc/meminfo` (`MemTotal`,
  a little less than the installed memory), `/proc/partitions` (whole
  disks), `/sys/firmware/efi` (exists only on UEFI),
  `/sys/class/dmi/id/product_uuid` (root only; empty without root, so
  `no_permission`), `sys_vendor` / `product_name` (a known hypervisor
  name makes it `virtual`), `/etc/machine-id` (the stable `source_id`).
- **Windows Server host.** `winsrv.system.os-info`,
  `winsrv.system.hardware`, `winsrv.storage.disk.list`. `source_id` is the
  product UUID, or the host name when the UUID is missing or a placeholder.
- **Kubernetes.** `k8s.node.list` (one server per node, with
  `system_uuid` as BIOS UUID and the node capacity), `k8s.workload.list`
  for each kind, all namespaces, page by page (at most 40 pages of 500),
  `k8s.persistentvolumeclaim.list` (claim sizes), and per workload
  `k8s.pod.list` with its `pod_selector`, page by page. A workload's
  `nodes` holds the `source_id` of its nodes' records, so workloads link
  to nodes. When a later page of a list fails, the rows of the earlier
  pages are kept and an omission names the rest ("... after the first
  500"); for a workload list the kind's total is then unknown. CPU and
  memory are sums over the containers per replica, given only when every
  container sets the value. A node that is a VM has the VM's BIOS UUID, so
  the merge lists it as a duplicate of the VM record. One UUID reported by
  two nodes of the cluster cannot name one machine, so both nodes get none
  and an omission says so.

## Dependencies

- `pydantic` v2 models (`model_validator`, discriminated union on `kind`).
- `connectors/result_handle_store.py` (`fetch_record`, `decode_document`).
- `operations/typed_register.py` (`register_typed_operation`,
  `register_typed_op_registrar`).
- Python's `csv` module.

## Tests

`backend/tests/test_migration_source_adapters.py` runs each adapter with a
fake `dispatch_child` and made-up child answers, stores the inventory in an
in-memory result store, and reads it back through the loader. It covers
failed and denied child reads, the counts check (also when a total could
not be read in full), a VM name used twice, a shortened child answer, a
Kubernetes node that matches a Hyper-V VM, placeholder and shared BIOS
UUIDs, a failed second page, pod paging, and the count dicts read back
from the handle of a long answer.
`backend/tests/test_migration_source_reads.py` covers the reads added for
the adapters (`hyperv.vms.guest.list`, `winsrv.system.hardware`,
`k8s.workload.list`, and the new `k8s.node.list` fields).


`backend/tests/test_migration_contracts.py` pins the shapes with made-up
fixtures: a Windows VM with three disks, a Linux VM with no utilization,
the same Linux machine read through its guest OS (same BIOS UUID), a
Kubernetes workload and an application. Inventories are stored through the
real JSONFlux reducer and read back through the loader. The golden file
`backend/tests/data/migration_azure_migrate_golden.csv` pins the CSV
output; the test also checks its header against Microsoft's template
header, column for column.

## Known issues

- Only handles are accepted. The source adapters always store their
  inventory, so this holds for them.
- Hyper-V reads one host. A cluster is read node by node; the VM id merges
  the results.
- A bare-metal Kubernetes node has no disk, firmware or boot data; only the
  OS and capacity from the node object.
- Disk speeds and utilization are not read by any adapter yet
  (`not_measured`), so sizing from these inventories is "allocated".
- Duplicate servers are matched by exact BIOS UUID only. Some guest
  systems report the BIOS UUID with the first three groups byte-swapped;
  the source adapters must normalise it before it reaches `identity_hints`.

## References

- Microsoft: Build a business case or assess servers using an imported
  CSV file,
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>.
- `docs/codebase/jsonflux.md` — stored results and handles.
- `docs/codebase/audit_query.md` — the same package pattern
  (`audit-integrity-1.x`).

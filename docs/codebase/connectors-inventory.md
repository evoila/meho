# Estate inventory package (`connectors/inventory`)

## Overview

This package holds the estate inventory and its exporter: MEHO reads the
systems a team runs today into one common inventory shape, and turns
stored inventories into files that other tools import.

In this package today:

- **One inventory shape** for every source platform (`contracts.py`).
- **One calculation result shape** (`contracts.py`): the form any tool
  uses to show how each number was made and what each check found.
- **Both shapes as versioned JSON Schema files** in `docs/schemas/`.
- **A loader** that reads stored inventories back by handle id, takes
  inventories given inline, and merges them (`inventory_load.py`).
- **The exporter read** `inventory.export` (`ops.py`), its answer shape
  (`export.py`) and its table of file formats (`formats.py`). The first
  format is the Azure Migrate CSV import file (`azure_migrate_csv.py`).
- **The source adapters**: one multi-step read per source connector that
  collects the estate into the inventory shape (`source_*.py`, shared parts
  in `adapter_kit.py`, registrar in `source_register.py`).

The VMware read `vmware.migration.inventory` is not in this repository.
It ships with the VMware connector package for the overlay image, and it
returns the same inventory shape. The adapters in this repository are in
this package (see "Source adapters" below).

Like `connectors/audit/`, the package has no connector class. Its
operations are registered under the natural key
`(product="inventory", version="1.x", impl_id="inventory")`, so the
connector id is `inventory-1.x`. No vendor is behind it. The handlers are
module-level functions; the dispatcher calls them with `target=None`. The
package `__init__` queues `register_inventory_operations` and
`register_source_adapters` on the lifespan registrar list
(`register_typed_op_registrar`).

## Key types

### Inventory envelope (`InventoryEnvelope`)

One inventory from one source:

| Field | Meaning |
|---|---|
| `schema_version` | `"1"`. Readers refuse any other value. |
| `source` | `connector_id`, `target` name, `collected_at`. |
| `records` | Records of three kinds, told apart by `kind`. |
| `omissions` | Parts of the estate that were not collected, with a reason. |
| `counts` | Records per kind, plus `source_totals`: the source's own total per kind, and `reconciliation`: the count check per kind. |
| `duplicates` | Records that look like a machine already in the list (same BIOS UUID or instance UUID, directly or through other records). Both records stay; later steps count the machine once. |

The model checks that `counts` match the records and that no
`source_id` repeats. It also checks that every `duplicates` entry names
records of the inventory, and that each `reconciliation` entry adds up:

- `source_total`: the total the source reports (equal to
  `source_totals[kind]`).
- `records`: the records of that kind.
- `omitted`: the omissions of that kind. An omission with `kind` and
  `source_id` set stands for one item of the source that has no record
  (one left-out VM). An omission without `kind` is a part of the work that
  failed as a whole (for example "utilization from VCF Operations").
- `difference` = `source_total` - `records` - `omitted`, and `matches` is
  true exactly when the difference is 0. A difference is reported, not
  hidden.

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
  samples, the minutes between two samples (`sample_interval_minutes`)
  and the source) or `null`, `collected_at`, `missing`, and
  `source_attributes` (more facts with fixed keys per source; for VMware:
  `vm_id`, `instance_uuid`, `hardware_version`, `esxi_host`, `cluster`).
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

### Calculation result (`CalculationResult`)

The form any tool uses to hand back what it computed from inventories. It
holds no word, rule or price of one target.

- `calculation_version`, `inputs` (the inventory handles with their
  connector, target and time, other targets read, and when it ran),
  `assumptions`, `items`, `unknowns`, `omissions`.
- `assumptions` is a list of named values (`Assumption`): `name`,
  `value`, and optionally `unit` and `source`. A name appears once.
- Each item (`ResultItem`) holds the numbers (`figures`) and the checked
  statements (`findings`) for one record.

Every number is a `Figure`: `value`, `unit`, `basis` (`measured` or
`allocated`; never "right-sized" without measurements), `inputs`, the
names of the `assumptions` it used, `calculation_version`, and
`unknown_reason` when `value` is `null`. The model refuses a figure that
cites an assumption that is not in the list.

Every check is a `Finding`: `check` (what was checked), `state` (`pass`,
`fail` or `unknown`), `reason`, `inputs`, `source` (a link to the rule
the check follows) and `source_checked_on` (the day the source was last
read). An input that is `null` means the data is missing; then the state
must be `unknown`, never `pass`. The shape ships no check.

Renamed from the first version: `AssessmentResult` is now
`CalculationResult`, `AssessmentInputs` is `CalculationInputs`,
`AssessmentItem` is `ResultItem` (its plain-text `flags` became
`findings`), and `assessment_result_json_schema()` is
`calculation_result_json_schema()`. The fixed `Assumptions` fields
(region, currency, price date, and so on) became the named-value list.

### JSON Schema files

`inventory_json_schema()` and `calculation_result_json_schema()` export
the two shapes as JSON Schema. The same schemas are checked in as
`docs/schemas/inventory-v1.schema.json` and
`docs/schemas/calculation-result-v1.schema.json`, so a tool outside the
backplane can check an inventory or a result without importing backplane
code. The number in the file name is the `schema_version`. The result
file also carries the two missing-data rules as `if`/`then`/`else`: a
figure with a `null` value needs an `unknown_reason`, a figure with a
known value has none, and a finding with a `null` input has the state
`unknown`. A tool that reads only the file rejects the same results as
the models.

- Make the files again after a model change:
  `cd backend && uv run python scripts/generate_inventory_schemas.py`.
- `backend/tests/test_inventory_shapes.py` fails when a file does not
  match the models.
- Version 1 shipped in 0.40.0. A breaking change raises `schema_version`
  and adds a new file.

### Sample interval of older inventories

`Utilization.sample_interval_minutes` came after the first inventories
were stored. A stored inventory without it still loads: the field is
`null`, and `missing` gets `utilization.sample_interval_minutes:
not_reported_by_source` (unless a parent path already gives a reason).
Only stored JSON gets this default. A producer that builds a record in
code sets the interval or gives its own reason.

## Control flow

### Stored and inline inventories

An inventory read returns one envelope. The envelope has two lists
(`records` and `omissions`), so when it is over the inline size limit the
JSONFlux reducer stores it **whole** as a document record and the caller
gets a handle id (see `jsonflux.md`). An inventory read also declares the
`result_handle` hint (`llm_instructions["result_handle"] =
{"always": true}`): then a small inventory (one VM is about
1.7 KB) comes back whole **and** with a handle, so it can be exported and
assessed too. No operation in this repository declares it. The VMware read
in the VMware connector package does. The adapters of this package store
their inventory themselves (`adapter_kit.py`).

An inventory can also be given inline in the call, for example one the
caller already has. `load_inventories(operator, handle_ids,
inventories=...)`:

1. Reads each handle from `get_result_handle_store()` with the caller's
   tenant id. The store also checks the caller, so another tenant's or
   another person's handle is a miss.
2. Fails with one clear message when a handle is unknown or expired (a
   stored result lasts one hour by default), when it holds rows instead of
   one inventory, when `schema_version` is not `"1"`, or when the envelope
   is not valid. Validation messages leave out the input values.
3. Decodes and checks each record in a worker thread
   (`asyncio.to_thread`), because a stored result can be several MB.
4. Checks each inline inventory with the same rules, in the same order:
   `schema_version` first, then the shape. The message names it
   `inline inventory <n>` where a stored one says `handle <id>`, and is
   otherwise the same.
5. Needs at least one handle or inline inventory, and at most 20 of both
   together.
6. Merges the envelopes: stored ones first, in handle order, then inline
   ones, in the given order (`merge_inventories`). A handle
   given twice is read once. A repeated `source_id`, or a server that
   shares a BIOS UUID or an instance UUID with another record, stays in
   `records` and is listed in `duplicates`; the caller decides what that
   means. The match comes from `IdentityIndex` in `contracts.py`, which
   the VMware adapter uses too, so `same_as` always names the first record
   of the machine, also through a chain of matches. A BIOS UUID matches
   in both forms a machine reports it: as the hypervisor shows it, and as
   the guest OS shows it with the bytes of the first three groups
   reversed (newer firmware tables keep them that way; a VMware VM from
   hardware version 13 on, for example). `bios_uuid_match_key` turns both
   forms into one key, so a Kubernetes node or a host read inside its OS
   still matches its VM record. A repeated record's BIOS UUID still
   counts, so a machine whose first record had no BIOS UUID is still
   matched to a later record from another source.

### `inventory.export`

Group `inventory`, `safe`, `requires_approval: false`. It is a typed
operation, not a multi-step one: it runs no other operation, it only reads
stored results and the inventories given in the call. The op id, the group
and the tags (`inventory`, `export`, `read`) name no target tool. Only the
format name does.

In:

- `format` (required): the name of a file format in `EXPORT_FORMATS`
  (`formats.py`). The parameter schema lists the names in an `enum`, with a
  short text per format. An unknown name fails with a message that lists
  the known formats, before any inventory is read.
- `inventory_handles` (handle ids), `inventories` (whole inventories given
  inline), or both; at least one of the two, and at most 20 together.

Out (`ExportResult` in `export.py`, plus `sources`):

- `format`: the format name from the call.
- `lines`: the file, one item per line without the line end, header first.
- `line_end`: join `lines` with it to get the file (CRLF for CSV).
- `row_count`, `columns` (the header row; empty when a format has none).
- `left_out`: records not in the file, each with its `reason`
  (`missing_required_field` with `missing`, `same_machine_as` with
  `same_as`, `over_file_limit`).
- `renamed`: records whose name in the file (`name_in_file`) differs from
  their name in the inventory, with the `reasons`.
- `skipped_records`: records of kinds the format has no place for, per kind.
- `notes`, and `sources`. Each entry of `sources` has `origin`: `"stored"`
  with its `handle_id`, or `"inline"` with no handle id, so a report shows
  which data MEHO stored itself (`LoadedInventory.sources()`).

The file is a list of lines on purpose. A large result comes back as a
handle, and `result_query` reads the list in pages of whole lines
(`offset` and `limit`, up to 500 items a page). Read the lines with the
path `/lines`; each page holds rows of `{"value": <line>}`. A long text
value can also be read in parts, but a part can end in the middle of a
line.

#### File formats (`formats.py`)

`EXPORT_FORMATS` maps a format name to an `ExportFormat`: the name, a
short text for callers, and the mapping function
`(records, duplicates) -> ExportResult`. A format is a plain mapping: the
columns, units, name rules and size limit of one file. It holds no price,
no sizing and no readiness check. There is no plug-in loading. A new
format is one new mapping module, one entry in the table and one golden
test file.

| Format | Module | File |
|---|---|---|
| `azure-migrate-csv` | `azure_migrate_csv.py` | Azure Migrate CSV import file (golden file `backend/tests/data/migration_azure_migrate_golden.csv`) |

#### Format `azure-migrate-csv`

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
(not on `inventory-1.x`), group `migration`, `safe`,
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
   has a handle `inventory.export` can read, and `result_query`
   reads the records (path `/records`). When the `omissions` list makes
   the answer larger than the inline limit (4 KB), the reducer stores the
   omissions as rows. The three count dicts are then kept by a
   `result_objects` hint: inline (every kind listed, a kind without a
   value is null) and whole with the rows, so `result_query` with path
   `/source_totals` reads them back.
5. `counts_match` is true only when the source's own total was read
   completely for every kind the source counts, and records plus left-out
   items equal it. A total that was not read, or was read only in part (an
   offline Proxmox node, a denied or failed workload list), gives `false`
   and no entry in `source_totals`: MEHO had no total to compare with,
   which is not the same as a match.
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
  disks), `/sys/firmware/efi` (exists only on UEFI; a BIOS host gets
  `secure_boot` `false`, a UEFI host `not_supported`),
  `/sys/class/dmi/id/product_uuid` (root only; empty without root, so
  `no_permission`), `sys_vendor` / `product_name` (a known hypervisor
  name makes it `virtual`), `/etc/machine-id` (the stable `source_id`).
- **Windows Server host.** `winsrv.system.os-info`,
  `winsrv.system.hardware`, `winsrv.storage.disk.list`. `source_id` is the
  product UUID, or the host name when the UUID is missing or a placeholder.
  BIOS firmware cannot have Secure Boot, so `secure_boot` is `false` there.
  On UEFI it is the read value, or unknown when the read gave none (it
  needs admin rights).
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
  container sets the value. The containers include the sidecars (init
  containers with `restartPolicy: Always`), because they run next to the
  app for the life of the pod. A node that is a VM has the VM's BIOS UUID, so
  the merge lists it as a duplicate of the VM record, also when the node
  shows the UUID with the bytes of the first three groups reversed. One
  UUID reported by two nodes of the cluster cannot name one machine, so
  both nodes get none and an omission says so.

### VMware source adapter

The VMware connector package brings `vmware.migration.inventory`. It
returns one inventory envelope with a handle, in the shape above. Its
code, tests and docs live with that package.

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
not be read completely), a VM name used twice, a shortened child answer, a
Kubernetes node that matches a Hyper-V VM, a node that matches a VMware VM
whose UUID it reports with reversed bytes, placeholder and shared BIOS
UUIDs, a failed second page, pod paging, and the count dicts read back
from the handle of a long answer.
`backend/tests/test_migration_source_reads.py` covers the reads added for
the adapters (`hyperv.vms.guest.list`, `winsrv.system.hardware`,
`k8s.workload.list`, and the new `k8s.node.list` fields).

`backend/tests/test_migration_contracts.py` pins the shapes with made-up
fixtures: a Windows VM with three disks, a Linux VM with no utilization,
the same Linux machine read through its guest OS (same BIOS UUID), a
Kubernetes workload and an application. Inventories are stored through the
real JSONFlux reducer and read back through the loader, and given inline
(the same checks and messages, `origin: "inline"` in `sources`). A one-VM
inventory reduced with the `result_handle` hint at the default limits gives
a handle the CSV operation accepts. The golden file
`backend/tests/data/migration_azure_migrate_golden.csv` pins the CSV
output; the test also checks its header against Microsoft's template
header, column for column.

`backend/tests/test_inventory_shapes.py` covers the calculation result
(named assumptions, a figure that cites an assumption that is not set, a
finding with and without data), the sample interval (set, missing in an
older inventory, missing from a producer), and the JSON Schema files
(they match the models, they are valid schemas, and the result file checks
a result with no backplane code, including the two missing-data rules).

## Known issues

- Hyper-V reads one host. A cluster is read node by node; the VM id merges
  the results.
- A bare-metal Kubernetes node has no disk, firmware or boot data; only the
  OS and capacity from the node object.
- Disk speeds and utilization are not read by the adapters of this package
  yet (`not_measured`), so sizing from these inventories is "allocated".
- Duplicate servers are matched by BIOS UUID, in its two byte orders, or by
  exact instance UUID. A record keeps the UUID text as its source reported
  it; only the match key is shared.

## References

- Microsoft: Build a business case or assess servers using an imported
  CSV file,
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>.
- `docs/codebase/jsonflux.md` — stored results and handles.
- `docs/codebase/audit_query.md` — the same package pattern
  (`audit-integrity-1.x`).

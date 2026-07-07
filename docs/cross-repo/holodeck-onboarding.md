<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Holodeck op surface onboarding — operator recipe

> Operator-facing recipe for the G3.8 `holodeck-ssh-9.0` op surface —
> the `meho holodeck …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/holodeck.sh` wrapper. The op
> handlers live in
> [`backend/src/meho_backplane/connectors/holodeck/`](../../backend/src/meho_backplane/connectors/holodeck/);
> the engineering-facing companion is
> [`docs/codebase/connectors-holodeck.md`](../codebase/connectors-holodeck.md)
> (if present). This doc is the cookbook every RDC operator reads when
> retiring the bash wrapper in favour of `meho holodeck …`.

## What this surface is

The `holodeck-ssh-9.0` connector is a **typed** connector: hand-coded
handlers over `asyncssh`, registered into the G0.6
`endpoint_descriptor` table at backplane startup. It dispatches under
the `(product="holodeck", version="9.0", impl_id="holodeck-ssh")`
registry triple — the connector id `holodeck-ssh-9.0`.

The `-ssh` discriminator in the impl_id leaves room for a future
`holodeck-rest-9.x` sibling without breaking the resolver's tie-break
ladder; **Holodeck has no REST API today**. Every Holodeck cmdlet runs
through `pwsh -EncodedCommand <b64-utf16le>` over the pooled SSH
connection; every shell command (`kubectl`, `vtysh`, `tail`,
`cat /var/lib/dhcp/dhcpd.leases`) runs over plain SSH on the same
appliance. This is the canonical SSH-only target in the inventory.

The v0.1 op surface (Initiative
[#371](https://github.com/evoila/meho/issues/371)) is the read-only
working set operators use for nested-lab inspection:

| Group | Ops | Class |
| --- | --- | --- |
| `identity` | `holodeck.about` | read-only fingerprint |
| `config` | `holodeck.config.show` | read-only config dict |
| `pod` | `holodeck.pod.list`, `holodeck.pod.info` | read-only |
| `service` | `holodeck.service.list` | read-only |
| `k8s` | `holodeck.k8s.exec` | read-only (verb safelisted) |
| `logs` | `holodeck.logs.tail` | read-only |
| `networking` | `holodeck.networking.show` | read-only composite |

Eight ops total. Every op dispatches through the same
`POST /api/v1/operations/call` route the agent surface uses — auth,
policy, audit, broadcast, and JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6. The CLI verb tree is operator
ergonomics over that one route; it is **not** a separate data path
and is **not** mirrored on the MCP surface (CLAUDE.md postulate 5 —
the agent reaches every Holodeck op via the narrow-waist meta-tools,
see the [agent meta-tool path](#the-agent-meta-tool-path) section).

## Pod-clone stays on the wrapper (deferred to G11)

This onboarding doc covers the **inspection** surface only —
`scripts/holodeck.sh` is fully replaced by `meho holodeck …`. The
**sister wrapper**
[`scripts/clone-holodeck-instance.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/clone-holodeck-instance.sh)
covers multi-step nested-lab provisioning end-to-end (validate templates
→ reserve resources → boot sequence → post-provision validation).

**Pod-clone explicitly stays in the wrapper for v0.2.** Per Initiative
#371 §6 ("Pod-clone op as a separate v0.2.next consideration"), the
multi-step orchestration belongs in a future **Runbooks** Initiative
under Goal G11 (per the v0.1 spec sequencing) once the runbook engine
ships. The decomposition runs roughly:

| Step in `clone-holodeck-instance.sh` | Future runbook composition |
| --- | --- |
| Validate source pod state | `holodeck.pod.info` (already shipped) |
| Reserve resources on appliance | New `holodeck.pod.reserve` op (G11) |
| Boot nested-lab sequence | Multi-step runbook + `holodeck.pod.clone` (G11) |
| Post-provision validation | `holodeck.networking.show` + `holodeck.service.list` (already shipped) |

The inspection ops shipped today (G3.8) are the **read primitives a
future runbook composes against**. The standalone wrapper stays in
place; the runbook lands when the engine ships. No further action is
required of operators today.

## Prerequisites

- **An SSH-reachable HoloRouter 9.0 appliance.** The connector talks
  Holodeck over SSH using `asyncssh`. The target must run Holodeck
  Toolkit 9.x on Photon OS 4.x / 5.x and expose port 22 with root SSH
  enabled.
- **Auth: password-default + key-fallback.** The HoloRouter OVA ships
  with `root` password auth enabled by default; the connector reads
  the password from Vault at `secret_ref.password`. If you've added an
  SSH key on the appliance and stored its private half in Vault at
  `secret_ref.ssh_private_key`, the connector prefers key auth
  (mirroring the wrapper's `PreferredAuthentications=publickey,password`
  behaviour). Either is supported; password is the v0.2 default.
- **`pwsh` installed on the appliance.** Holodeck cmdlets reach the
  appliance through `pwsh -EncodedCommand` so PowerShell 7+ must be on
  the appliance's `$PATH`. HoloRouter 9.0 OVAs ship `pwsh`
  pre-installed.
- **The bundled Holodeck services running.** The probe asserts
  `Get-Service | Where-Object { $_.Name -like 'Holo*' }` returns each
  service in `Running` state. A `holodeck_services_down` probe means
  one of DHCP / DNS / NTP / FRR-BGP / Webtop / K8s-in-appliance is
  Stopped.
- **A registered Holodeck target.** The CLI verbs take `--target <slug>`.
  The slug resolves server-side to a row in the `targets` table. The
  target carries `product="holodeck"`, `host`, `port` (optional,
  defaults to 22), `secret_ref` (Vault path), and `auth_model`.
- **`auth_model = shared_service_account`** — the only auth model v0.2
  ships. The connector reads the SSH credential out of `secret_ref`
  (resolved from Vault); both password auth (default) and key auth
  (fallback) are supported.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb.

## Target + auth model — Vault credential setup

The shipped connector's auth model is `shared_service_account` over
either SSH password auth (the HoloRouter OVA default) or SSH key auth.
The credential is stored in Vault at a per-host path under the
KV-v2 mount; the target row's `secret_ref` column carries that **path
string** (never the credential itself — the connector resolves the
path under the calling operator's Vault identity on every SSH
connect).

Two constraints on the path (both break credential resolution when
violated):

1. **Mount-relative — no `secret/` prefix.** `secret_ref` is the
   logical KV-v2 path relative to the mount. Vault's KV-v2 API
   inserts the `/data/` segment itself, so a value that embeds the
   mount (`secret/…`) double-resolves to `secret/data/secret/…` and
   404s.
2. **Inside the meho-readable subtree.** The backplane's Vault policy
   grants operators read on `secret/meho/*` only (see
   [vault-provisioning.md](vault-provisioning.md)) — stage the secret
   under `meho/…` or the resolution fails with a Vault permission
   error.

### Password auth (the v0.2 default)

The HoloRouter OVA ships root password auth enabled; operators
typically have not installed SSH keys on the appliance.

```console
$ meho vault kv put --target rdc-vault secret \
    meho/rdc-hetzner-dc/holodeck/holorouter-01 \
    --data @holodeck_secret_ref.json
```

`holodeck_secret_ref.json` shape:

```json
{
  "username": "root",
  "password": "<HoloRouter OVA root password>"
}
```

### Key auth (fallback when SSH keys are installed)

If you've added the operator's public key to `/root/.ssh/authorized_keys`
on the appliance and want to retire password auth, swap in:

```json
{
  "username": "root",
  "ssh_private_key": "<PEM-encoded OpenSSH private key>"
}
```

The `ssh_private_key` value is the contents of the private-key file as
a single JSON string with literal `\n` newlines, `BEGIN` / `END`
headers, and the trailing newline. `asyncssh`'s `import_private_key`
parses Ed25519, ECDSA, RSA, and OpenSSH-format PEM keys. When both
fields are present in the Vault secret, the connector prefers the key
(matching the wrapper's `PreferredAuthentications=publickey,password`
order).

### Registering the target

```yaml
# targets.yaml
targets:
  - name: holorouter-hetzner-dc
    product: holodeck
    host: 10.5.20.1
    port: 22
    # Mount-relative KV-v2 path inside the meho-readable subtree —
    # no `secret/` prefix (see "Target + auth model" above).
    secret_ref: meho/rdc-hetzner-dc/holodeck/holorouter-01
    auth_model: shared_service_account
```

```console
$ meho targets import targets.yaml
```

Verify the target is reachable:

```console
$ meho targets probe holorouter-hetzner-dc
ok — holodeck 9.0.0 reachable; Photon 5.0 healthy; Holo* services Running

$ meho holodeck about --target holorouter-hetzner-dc
holodeck-ssh-9.0 holodeck.about — status=ok (124ms)
  vendor:          vmware
  product:         holodeck
  version:         9.0.0
  build:           VMware Photon Linux 5.0
  photon_version:  5.0
  pod_id:          HoloPod-Alpha
```

`probe` exercises the four-bucket failure ladder per Initiative #371:

| `reason` | Meaning | Operator remediation |
| --- | --- | --- |
| `tcp_unreachable` | SSH socket can't connect | check host, firewall, port 22 |
| `ssh_auth_failed` | credentials rejected or handshake failed | check `secret_ref` password / key in Vault |
| `photon_unhealthy` | SSH ok but `/etc/photon-release` empty / non-zero exit | non-Photon target or corrupt appliance image |
| `holodeck_services_down` | Photon ok but `Get-Service Holo*` shows a non-Running service | log onto the appliance, `Restart-Service` the affected unit |

## Known gotchas

### Holodeck has no REST API — every op goes over SSH

Unlike the rest of the v0.3 connector inventory (vSphere REST, NSX,
SDDC Manager, vCF, …) the Holodeck appliance exposes no HTTP control
surface. Every op in this connector dispatches through SSH:

- **Cmdlet ops** (`about`, `config show`, `pod list/info`, `service list`)
  go through `pwsh -NoProfile -NonInteractive -EncodedCommand <b64>`
  where the base64 payload is the UTF-16LE bytes of the PowerShell
  script. Output is piped through `ConvertTo-Json` and parsed by the
  backend with stdlib `json`. There is no CliXml dependency — the
  Initiative #371 design correction (2026-05-21) supersedes the
  original CliXml note in favour of `ConvertTo-Json`.
- **Shell ops** (`k8s exec`, `logs tail`, `networking show`'s
  `vtysh` / `cat` sub-commands) go over plain SSH with no `pwsh`
  indirection.

The agent-facing `llm_instructions` on every op spells this out so an
LLM doesn't compose against a non-existent REST surface.

### `holodeck.k8s.exec` is read-only — verbs are safelisted

The `kubectl` command operators pass to `meho holodeck k8s exec` is
forwarded **verbatim** to the backend. The backend handler
(`parse_kubectl_command` in `ops_read.py`) is the authoritative gate
and enforces two complementary defences:

1. **Verb safelist.** Only `get`, `describe`, `logs`, `top`, `explain`,
   `api-resources`, `api-versions`, `cluster-info`, and `version` are
   accepted. Mutating verbs (`create`, `apply`, `delete`, `edit`,
   `replace`, `patch`, `scale`, `rollout`, `label`, `annotate`,
   `cp`, `exec`, `port-forward`, `proxy`, `drain`, `cordon`) are
   refused with `result_connector_error`.
2. **Shell-metacharacter reject.** Any command containing `;`, `&&`,
   `||`, `|`, `$(...)`, backticks, `>`, `<`, newline, or line
   continuation is refused **before** any SSH traffic happens. The
   reject is load-bearing because `shlex.split` in POSIX mode does
   not treat these as token boundaries, so `kubectl get pods; rm -rf /`
   would otherwise tokenise to `['kubectl', 'get', 'pods;', ...]` —
   the verb-safelist check would see `'get'` and approve, and the raw
   string would land at the appliance shell. The metachar reject
   closes that hole.

If you need a verb that's not on the safelist, file a follow-up — do
not work around the safety check at the CLI layer. The CLI is
deliberately a pure forwarder so the safety gate has one location.

### Holodeck cmdlets need `Get-HoloDeckModule` to be loaded

The appliance's `pwsh` profile imports the Holodeck cmdlet module on
session start. If the bundled module is uninstalled or its path is
wrong, every cmdlet op will return `pwsh -EncodedCommand exited with
status 1` and the operator-facing surface will fail with a connector
error.

Log onto the appliance and confirm `Get-Module -ListAvailable Holodeck`
shows the module; reinstall via the Holodeck Toolkit OVA refresh if
missing.

### `holodeck.networking.show` per-section `ok` flag

The networking composite runs four sub-commands and folds each into a
`{ok, …}` sub-section. A `bgp.ok=false` means the `vtysh` sub-command
returned empty or failed — not that BGP is broken. Use
`meho holodeck logs tail frr` to drill into the FRR log if BGP shows
`ok=false`.

### Pod-clone is NOT covered by `meho holodeck`

If you need to spin up a fresh nested lab, **continue to use
`scripts/clone-holodeck-instance.sh`** for now. This is the explicit
deferral in Initiative #371 §6 — multi-step orchestration belongs in a
Runbooks Initiative (future Goal G11) once the runbook engine ships.
The standalone wrapper stays in place. Inspect with `meho holodeck`;
provision with the wrapper.

## The CLI verb surface

Every verb pre-bakes `connector_id="holodeck-ssh-9.0"` so operators
never type the connector id. All verbs accept `--target <slug>`
(required), `--json` (emit the full `OperationResult` envelope for
`jq`), and `--backplane <url>` (override the URL from the last
`meho login`). Exit codes mirror `meho operation call` (0=ok,
1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected).

### Identity — `meho holodeck about`

```console
$ meho holodeck about --target holorouter-hetzner-dc
$ meho holodeck about --target holorouter-hetzner-dc --json | jq .result
```

Returns vendor / product / version / build / photon_version / pod_id
from `/etc/photon-release` + `Get-HoloDeckConfig`. Use before issuing
higher-level ops to confirm reachability + version.

### Config — `meho holodeck config show`

```console
$ meho holodeck config show --target holorouter-hetzner-dc
$ meho holodeck config show --target holorouter-hetzner-dc --json | jq .result.config
```

Returns the full `Get-HoloDeckConfig` dict (vendor + product + pod ID
+ services block). For just the identifying fields, prefer
`meho holodeck about` (fewer fields, slightly faster).

### Pod — `meho holodeck pod list` / `info <id>`

```console
# 1:1 replacement for ./scripts/holodeck.sh --target holorouter
#   'pwsh -c "Get-HoloDeckPod | Format-Table"'
$ meho holodeck pod list --target holorouter-hetzner-dc

# Drill into a single pod
$ meho holodeck pod info HoloPod-001 --target holorouter-hetzner-dc
$ meho holodeck pod info HoloPod-001 --target holorouter-hetzner-dc --json | jq .result.pod
```

`list` runs `Get-HoloDeckPod | ConvertTo-Json -Depth 4` and surfaces
the JSONFlux-shaped `{rows, total}` envelope (rows inline today; a
future JSONFlux reducer will spill large pod lists via the standard
`result_describe` / `result_query` flow).

`info` runs `Get-HoloDeckPod -Id '<id>' | ConvertTo-Json -Depth 4` and
returns the single-pod detail dict (state, networking, VMs).

### Service — `meho holodeck service list`

```console
$ meho holodeck service list --target holorouter-hetzner-dc
$ meho holodeck service list --target holorouter-hetzner-dc --json \
    | jq '.result.rows[] | select(.Status != "Running")'
```

Runs `Get-Service | Where-Object { $_.Name -like 'Holo*' } |
Select-Object Name,Status,DisplayName | ConvertTo-Json -Depth 4`.
Pair with `meho holodeck logs tail <component>` for drill-in.

### K8s — `meho holodeck k8s exec '<cmd>'`

```console
$ meho holodeck k8s exec 'kubectl get pods -A' --target holorouter-hetzner-dc
$ meho holodeck k8s exec 'kubectl describe node holorouter-node-1' --target holorouter-hetzner-dc
$ meho holodeck k8s exec 'kubectl logs -n kube-system <pod>' --target holorouter-hetzner-dc --json
```

Forwards the supplied `kubectl` invocation verbatim to the in-appliance
K8s cluster via plain SSH. **Read-only** — verbs and shell metacharacters
are policed on the backend; see [Known gotchas](#holodeckk8sexec-is-read-only--verbs-are-safelisted).
Always single-quote the kubectl command so it lands in `meho`'s argv as
one element.

### Logs — `meho holodeck logs tail <component>`

```console
$ meho holodeck logs tail dhcp --target holorouter-hetzner-dc
$ meho holodeck logs tail frr --lines 500 --target holorouter-hetzner-dc
$ meho holodeck logs tail dns --target holorouter-hetzner-dc --json | jq -r '.result.files[].lines'
```

Runs `tail -n <lines> /holodeck-runtime/logs/<component>*.log` over
plain SSH. Component slugs map to the bundled services: `dhcp`, `dns`,
`frr`, `webtop`, `k8s`. Allowed chars: `[A-Za-z0-9._-]+` (backend
rejects anything else). `--lines` defaults to 200; backend clamps to
[1, 5000].

### Networking — `meho holodeck networking show`

```console
$ meho holodeck networking show --target holorouter-hetzner-dc
$ meho holodeck networking show --target holorouter-hetzner-dc --json | jq .result.bgp
```

Composes FRR/BGP peer summary (`vtysh -c 'show bgp summary'`) + kernel
routes (`vtysh -c 'show ip route'`) + DNS zone summary
(`Get-DnsServerZone | ConvertTo-Json`) + DHCP leases
(`cat /var/lib/dhcp/dhcpd.leases`) into one envelope with per-section
`ok` flags. Each sub-command's failure is isolated — a `vtysh` crash on
the BGP summary path doesn't blank the DNS or DHCP sub-sections.

## The agent meta-tool path

Per CLAUDE.md postulate 5, these CLI verbs are **operator-only
ergonomics** — they are not mirrored on the MCP surface. Agents reach
Holodeck ops via the same narrow-waist `search_operations` +
`call_operation` meta-tools used for every other connector:

```
search_operations(connector_id="holodeck-ssh-9.0", query="pod list")
→ [{"op_id": "holodeck.pod.list", "summary": "List the active Holodeck nested pods.", ...}]

call_operation(op_id="holodeck.pod.list", target={"name": "holorouter-hetzner-dc"}, params={})
→ OperationResult{status="ok", result={"rows": [...], "total": 3}}
```

The `llm_instructions` on each op provide agent-readable guidance.
Every op's `when_to_use` carries the canonical SSH-only transport note
so agents don't compose against a non-existent REST surface:

> Holodeck has no REST API; the underlying transport is
> PowerShell-over-SSH (pwsh -EncodedCommand routed through asyncssh)
> for cmdlet ops, plain SSH for kubectl / shell-pipeline ops.

| Op | `when_to_use` summary |
| --- | --- |
| `holodeck.about` | Identify Holodeck version + confirm SSH/pwsh reachability |
| `holodeck.config.show` | Full Get-HoloDeckConfig snapshot (vendor + pod ID + services block) |
| `holodeck.pod.list` | Inventory the active nested pods on the appliance |
| `holodeck.pod.info` | Per-pod detail (state, networking, VMs) |
| `holodeck.service.list` | Bundled Holodeck Photon services + their Status |
| `holodeck.k8s.exec` | Read-only kubectl inspection of the in-appliance K8s cluster |
| `holodeck.logs.tail` | Tail `/holodeck-runtime/logs/<component>*.log` per service |
| `holodeck.networking.show` | Composite FRR/BGP + DNS + DHCP snapshot |

## Wrapper-flip recipe — retiring `scripts/holodeck.sh`

The consumer's `scripts/holodeck.sh` wrapper calls `pwsh` cmdlets and
plain shell commands over SSH with hard-coded credentials. Replace each
invocation with the `meho holodeck` verb:

| Old `holodeck.sh` invocation | New `meho holodeck` equivalent |
| --- | --- |
| `./scripts/holodeck.sh --target T 'pwsh -c "Get-HoloDeckConfig"'` | `meho holodeck config show --target T` |
| `./scripts/holodeck.sh --target T 'pwsh -c "Get-HoloDeckPod \| Format-Table"'` | `meho holodeck pod list --target T` |
| `./scripts/holodeck.sh --target T "pwsh -c 'Get-HoloDeckPod -Id <id>'"` | `meho holodeck pod info <id> --target T` |
| `./scripts/holodeck.sh --target T 'pwsh -c "Get-Service Holo*"'` | `meho holodeck service list --target T` |
| `./scripts/holodeck.sh --target T 'kubectl get pods -A'` | `meho holodeck k8s exec 'kubectl get pods -A' --target T` |
| `./scripts/holodeck.sh --target T 'tail -n 200 /holodeck-runtime/logs/dhcp*.log'` | `meho holodeck logs tail dhcp --target T` |
| `./scripts/holodeck.sh --target T 'vtysh -c "show bgp summary"'` | `meho holodeck networking show --target T` (composite) |

Once every calling site in `evoila-bosnia/claude-rdc-hetzner-dc` is
migrated:

1. Add the Holodeck target with `meho targets import` (see above).
2. Store the SSH credential in Vault with `meho vault kv put`.
3. Run `meho targets probe holorouter-hetzner-dc` to confirm
   end-to-end connectivity.
4. Remove `scripts/holodeck.sh` from the consumer repo and update any
   CI or runbook references.
5. **Leave `scripts/clone-holodeck-instance.sh` in place** — pod-clone
   is the deferred multi-step Runbook (Goal G11; see
   [Pod-clone stays on the wrapper](#pod-clone-stays-on-the-wrapper-deferred-to-g11)).

## Goal #214 G3.8 Holodeck checklist

- [x] G3.8-T1 (#853) — Holodeck connector skeleton + `_pwsh.py` (ConvertTo-Json) + registry v2
- [x] G3.8-T2 (#854) — Holodeck 7 typed read ops + read-only kubectl with two-layer shell-injection defence
- [x] G3.8-T3 (#855) — Holodeck CLI verbs + MCP review (SSH-only transport copy) + recorded-fixture/asyncssh E2E + this onboarding doc

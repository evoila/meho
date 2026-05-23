<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Hetzner Robot op surface onboarding — operator recipe

> Operator-facing recipe for the G3.7 `hetzner-rest-2026.04` op surface —
> the `meho hetzner-robot …` verb tree, the agent meta-tool path,
> `targets.yaml` registration, and the migration off the consumer's
> `scripts/hetzner-robot.sh` wrapper. The connector implementation lives in
> [`backend/src/meho_backplane/connectors/hetzner_robot/`](../../backend/src/meho_backplane/connectors/hetzner_robot/);
> the engineering canary runbook is
> [`docs/cross-repo/g37-hetzner-canary.md`](./g37-hetzner-canary.md).
> This doc is the cookbook every RDC operator reads when onboarding a
> Hetzner Robot account or retiring `scripts/hetzner-robot.sh`.

---

## WARNING — 401 IP-block gotcha (READ THIS FIRST)

> **This is the most important section in this document.
> Read it before you touch any credentials.**

Hetzner Robot has an unusual shared-egress IP protection policy:

**Three consecutive 401 responses from the same source IP trigger a
10-minute block on that IP.**

MEHO operates on a **shared egress IP**. If one operator misconfigures
their Webservice-user credentials, all operators on the shared egress are
locked out of the Hetzner Robot API for 10 minutes — no exceptions.

The connector's response to this risk:

- On the **first** 401 response, the connector raises `auth_failed`
  immediately. It does **not** retry. It does **not** attempt a second
  call. It consumes exactly one of the three allowed failures.
- The base `HttpConnector._retryable` predicate already excludes 4xx
  from tenacity's retry policy. The connector adds an explicit 401-check
  before `raise_for_status()` so the operator sees a clear human-readable
  message rather than a generic HTTP error.
- The `llm_instructions` for the two most-called ops (`hetzner-robot.about`
  and `hetzner-robot.server.list`) contain the same warning verbatim so
  agents see it before composing a call.

**What to do when you see `auth_failed`:**

1. Stop. Do not retry.
2. Open the Robot portal → Account → Settings → Webservice.
3. Verify the Webservice user exists and is active.
4. Update the credentials at the target's Vault path
   (`secret_ref`, see [Credentials in Vault](#credentials-in-vault)).
5. Test with `meho hetzner-robot about --target <slug>` exactly once.
6. If it still fails, check the Webservice user's password again before
   trying a second time.

---

## What this surface is

The `hetzner-rest-2026.04` connector is a **generic-ingested** connector:
all 10 curated read-only ops are loaded from the Hetzner Robot Webservice
OpenAPI spec via the G0.7 ingestion pipeline, stored as `EndpointDescriptor`
rows with `source_kind='ingested'`, and enabled via
[`apply_robot_core_curation`](../../backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py).

The hand-rolled
[`HetznerRobotConnector`](../../backend/src/meho_backplane/connectors/hetzner_robot/connector.py)
class provides HTTP Basic auth (Webservice user, distinct from the Robot
portal login) and the 401-IP-block protection logic. Generic ops dispatch
through the httpx client the connector manages; no typed handlers exist in
v0.2 (write ops are deferred to v0.2.next per Initiative #370).

All ops dispatch through the same `POST /api/v1/operations/call` route the
agent surface uses — auth, policy, audit, broadcast, and JSONFlux all run as
documented in [CLAUDE.md](../../CLAUDE.md) §6.

The v0.2 op surface (Initiative [#370](https://github.com/evoila/meho/issues/370)):

| Group | CLI verb | `op_id` | Notes |
| --- | --- | --- | --- |
| robot-about | `meho hetzner-robot about` | `GET:/query` | API version + account summary |
| robot-servers | `meho hetzner-robot server list` | `GET:/server` | Dedicated-server inventory |
| robot-servers | `meho hetzner-robot server info <ip>` | `GET:/server/{server-ip}` | Single server detail |
| robot-networking | `meho hetzner-robot ip list` | `GET:/ip` | All IPs with lock + traffic status |
| robot-networking | `meho hetzner-robot subnet list` | `GET:/subnet` | All subnets with gateway + IP version |
| robot-networking | `meho hetzner-robot vswitch list` | `GET:/vswitch` | All vSwitches with VLAN + server members |
| robot-networking | `meho hetzner-robot vswitch info <id>` | `GET:/vswitch/{id}` | Single vSwitch detail |
| robot-networking | `meho hetzner-robot failover list` | `GET:/failover` | All failover IPs with routing targets |
| robot-networking | `meho hetzner-robot rdns list` | `GET:/rdns` | All reverse DNS (PTR) entries |
| robot-ssh-keys | `meho hetzner-robot ssh-key list` | `GET:/key` | SSH public keys in the Robot portal |

The CLI verb tree is **operator ergonomics** over those dispatch routes;
it is **not** a separate data path and is **not** mirrored on the MCP
surface (CLAUDE.md postulate 5). Agents reach every Hetzner Robot op via
the narrow-waist meta-tools (`list_operation_groups`, `search_operations`,
`call_operation`).

## Prerequisites

- **A Hetzner Robot account** with dedicated servers.
- **A Webservice user** (see [Webservice user setup](#webservice-user-setup-distinct-from-robot-login)).
- **Vault access** to store the Webservice-user credentials (see [Credentials in Vault](#credentials-in-vault)).
- **A registered Robot target** in the MEHO targets registry (see [targets.yaml entry](#targetsyaml-entry)).
- **The `hetzner-rest-2026.04` connector ingested** and the 10 core ops
  curated (see [G3.7 Hetzner canary runbook](./g37-hetzner-canary.md)).

## Webservice user setup (distinct from Robot login)

> This is the most common setup error. The Robot API requires a
> **Webservice user** — a separate account from your Robot portal login.

The Hetzner Robot Webservice API authenticates with **HTTP Basic auth**
using a **Webservice user**. This is a distinct account from the user you
log into the Robot portal with. To create one:

1. Log in to [robot.hetzner.com](https://robot.hetzner.com).
2. Navigate to **Account → Settings → Webservice and application**.
3. Click **Create Webservice user**.
4. Choose a username (e.g. `meho-rdc`) and a strong password.
5. Note the username and password — you will not see the password again.
6. Store them in Vault immediately (see [Credentials in Vault](#credentials-in-vault)).

The Webservice user can be deleted and re-created if you lose the password.
**There is no "change password" flow** — delete and recreate.

## Credentials in Vault

Store the Webservice-user credentials at the target's `secret_ref` path in Vault:

```bash
# Replace <path> with the path recorded in targets.yaml secret_ref.
# Example: kv/data/hetzner/rdc-robot
vault kv put <path> username="<webservice-username>" password="<webservice-password>"
```

The dict shape the connector expects:

```json
{
  "username": "meho-rdc",
  "password": "<strong-password>"
}
```

Both keys are required. A missing key surfaces as a `RuntimeError` naming
the target and the missing key — check the Vault path first.

## targets.yaml entry

Add an entry to `targets.yaml` in `claude-rdc-hetzner-dc`:

```yaml
- name: rdc-robot          # stable slug used in --target flags
  product: hetzner-robot
  host: robot.hetzner.com
  port: 443                # HTTPS, optional (443 is the default)
  secret_ref: kv/data/hetzner/rdc-robot
  auth_model: shared_service_account
  vpn_required: false
  notes: "Hetzner Robot Webservice for the RDC production account."
```

Import the target:

```bash
meho targets import targets.yaml
```

Probe it (one TCP+TLS+auth call — uses one of your three 401 budget):

```bash
meho targets probe rdc-robot
```

Expected output:

```
rdc-robot  ok  GET /server  hetzner/robot-webservice (3 servers)
```

If you see `auth_failed`, stop — fix credentials before probing again.

## Available ops — CLI usage examples

### About (API version + account summary)

```bash
meho hetzner-robot about --target rdc-robot
```

Expected output:

```
hetzner-rest-2026.04 GET:/query — status=ok (42ms)
  api_version: 1.0
  account_id:  robot-account-001
```

### Dedicated-server inventory

```bash
# List all servers
meho hetzner-robot server list --target rdc-robot

# Show full detail for one server
meho hetzner-robot server info 1.2.3.4 --target rdc-robot
```

Expected output (server list):

```
hetzner-rest-2026.04 GET:/server — status=ok (87ms)
number       ip                 product              dc              status
100001       1.2.3.4            AX41-NVMe            FSN1-DC14       ready
100002       5.6.7.8            AX51-NVMe            HEL1-DC2        ready
```

### IP addresses

```bash
meho hetzner-robot ip list --target rdc-robot
```

### Subnets

```bash
meho hetzner-robot subnet list --target rdc-robot
```

### vSwitches

```bash
# List vSwitches
meho hetzner-robot vswitch list --target rdc-robot

# Inspect a vSwitch by numeric ID
meho hetzner-robot vswitch info 4321 --target rdc-robot
```

### Failover IPs

```bash
meho hetzner-robot failover list --target rdc-robot
```

Output highlights active routing:

```
failover_ip        server_ip          active_server_ip
1.2.3.10           1.2.3.1            5.6.7.8  [ROUTED AWAY]
```

`[ROUTED AWAY]` indicates a failover IP currently routed to a non-primary
server — useful for diagnosing active failover states.

### Reverse DNS (PTR records)

```bash
meho hetzner-robot rdns list --target rdc-robot
```

### SSH keys in the Robot portal

```bash
meho hetzner-robot ssh-key list --target rdc-robot
```

### Raw operation call (escape hatch)

```bash
# Use any op_id directly — useful for ops without alias verbs
meho hetzner-robot operation call GET:/server --target rdc-robot
meho hetzner-robot operation search "ip list" --target rdc-robot
```

## The agent meta-tool path

Agents **do not** use the `meho hetzner-robot` CLI verbs. They use the
narrow-waist meta-tool contract defined in CLAUDE.md §5:

```
list_operation_groups(connector_id="hetzner-rest-2026.04")
→ pick group (e.g. "robot-servers")

search_operations(connector_id="hetzner-rest-2026.04", query="list servers", group="robot-servers")
→ hit: GET:/server

call_operation(connector_id="hetzner-rest-2026.04", op_id="GET:/server", target={"name": "rdc-robot"}, params={})
```

The `llm_instructions` attached to each op guide the agent on when to call,
what the output shape is, and what to do next. The 401-IP-block warning is
embedded in the `when_to_call` field of the two most critical ops so agents
do not retry on auth failures.

## JSON output + JSONFlux handles

Every CLI verb accepts `--json` to emit the full `OperationResult` envelope:

```bash
meho hetzner-robot server list --target rdc-robot --json | jq '.result[] | .server.server_ip'
```

Large server lists (>~50 rows / 4 KB) return a JSONFlux handle instead of a
raw array. The handle's `handle_id` is used to drill in:

```bash
meho operation result-query <handle_id> '.[] | .server.server_ip'
meho operation result-aggregate <handle_id> --group dc
```

In v0.2 the production reducer ships as `PassThroughReducer` — handles are
only produced by the test-only `ForceHandleReducer` in acceptance tests.
Real JSONFlux reduction (MinIO/S3 spill, `result_query`) is a v0.2.next
concern per Goal #214.

## Write ops — form-encoded bodies (future reference)

The Hetzner Robot Webservice API requires
`application/x-www-form-urlencoded` bodies for all write verbs. It
**rejects** `application/json`. The connector ships a `_post_form` helper
(wrapping httpx's `data=` parameter) for v0.2.next write readiness.

Write ops (server reset, boot, reinstall, rDNS update, vSwitch modification)
are out of scope for v0.2 and deferred per Initiative #370. When they land,
every write verb must use `_post_form` — never `json=` — against this API.

## Retiring `scripts/hetzner-robot.sh`

The consumer's `scripts/hetzner-robot.sh` wrapper in
`evoila-bosnia/claude-rdc-hetzner-dc` is the predecessor surface this op
layer replaces. The migration:

| Shell wrapper | MEHO verb |
| --- | --- |
| `./hetzner-robot.sh server list` | `meho hetzner-robot server list --target rdc-robot` |
| `./hetzner-robot.sh server get <ip>` | `meho hetzner-robot server info <ip> --target rdc-robot` |
| `./hetzner-robot.sh ip list` | `meho hetzner-robot ip list --target rdc-robot` |
| `./hetzner-robot.sh subnet list` | `meho hetzner-robot subnet list --target rdc-robot` |
| `./hetzner-robot.sh vswitch list` | `meho hetzner-robot vswitch list --target rdc-robot` |
| `./hetzner-robot.sh failover list` | `meho hetzner-robot failover list --target rdc-robot` |
| `./hetzner-robot.sh rdns list` | `meho hetzner-robot rdns list --target rdc-robot` |
| `./hetzner-robot.sh ssh-key list` | `meho hetzner-robot ssh-key list --target rdc-robot` |

**Wrapper retirement checklist** (file in `claude-rdc-hetzner-dc`):

- [ ] `secret_ref` in `targets.yaml` points to the Webservice-user credentials in Vault.
- [ ] `meho targets probe rdc-robot` returns `ok`.
- [ ] `meho hetzner-robot server list --target rdc-robot` returns the expected inventory.
- [ ] `meho hetzner-robot about --target rdc-robot` returns `status=ok`.
- [ ] All CI scripts using `scripts/hetzner-robot.sh` updated to `meho hetzner-robot …`.
- [ ] `scripts/hetzner-robot.sh` deleted or marked deprecated.

## G3.7 Hetzner checklist (Goal #214)

- [x] G3.7-T6 (#846): `HetznerRobotConnector` skeleton (HTTP Basic auth + 401-IP-block protection)
- [x] G3.7-T7 (#847): G0.7 spec ingestion against `robot-api.yaml`
- [x] G3.7-T8 (#849): 10 core ops curated + enabled + `apply_robot_core_curation`
- [x] G3.7-T9 (#852): CLI verbs + MCP review (401-IP-block warning) + dispatch smoke E2E + this doc

## References

- Hetzner Robot Webservice API docs: https://robot.hetzner.com/doc/webservice/en.html
- Consumer wrapper (being retired): `scripts/hetzner-robot.sh` in `evoila-bosnia/claude-rdc-hetzner-dc`
- Engineering canary runbook: [`docs/cross-repo/g37-hetzner-canary.md`](./g37-hetzner-canary.md)
- Connector source: [`backend/src/meho_backplane/connectors/hetzner_robot/`](../../backend/src/meho_backplane/connectors/hetzner_robot/)
- Core ops + llm_instructions: [`core_ops.py`](../../backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py)
- CLI verb tree: [`cli/internal/cmd/hetzner-robot/`](../../cli/internal/cmd/hetzner-robot/)

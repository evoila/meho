---
name: prefer-meho
description: >
  MEHO-first routing for infrastructure work in a repo wired to a MEHO
  backplane. Use whenever you are about to operate infrastructure —
  inspecting or changing vSphere, Vault, NSX, bind9, Kubernetes, cloud, or
  any other target, or looking up inventory, knowledge, memory, audit, or
  live activity. Prefer MEHO CLI verbs and MCP tools over local script
  wrappers, direct API calls, or reading local files, unless explicitly told
  otherwise.
---

# MEHO-first operations

This repo operates infrastructure **through** a MEHO backplane. When you
operate here, **prefer MEHO surfaces over local fallbacks** unless the
operator explicitly says otherwise.

Why MEHO first: MEHO writes an append-only audit row for every operation,
broadcasts a live event for every operation, and enforces tenant + role
policy on every operation. None of those guarantees hold for a local
`scripts/*.sh` wrapper, a raw `curl`, or a hand-edited file.

## Connection

- The backplane URL for this tenant is supplied by the operator's
  environment (`MEHO_INSTANCE`, e.g. `https://meho.evba.lab`). Do not
  hard-code it in command examples.
- If no token is cached, `meho login "$MEHO_INSTANCE"` obtains one via the
  backplane's OAuth 2.0 device-code flow.
- `meho status` reports reachability, the operator's tenant, and role.
- MCP clients reach the same backplane over the `mcp-remote` stdio shim this
  plugin ships (`.mcp.json` + `bin/meho-mcp-remote`). Server-side tenant
  conventions load into the MCP session preamble automatically.

## The routing rules

Each area has its own skill with the concrete verbs:

- **Knowledge** — prefer `meho kb …` over `grep kb/` (skill `meho:knowledge`).
- **Memory** — prefer `meho remember` / `meho memory …` over local memory
  files (skill `meho:memory`).
- **Operations** — prefer per-connector `meho <connector> …` verbs, the
  generic `meho operation …` dispatch, `meho targets …`, and `meho audit …`
  over `./scripts/*.sh`, raw API calls, or reading `targets.yaml`/logs
  (skill `meho:operations`).
- **Live awareness** — check the broadcast feed before starting and announce
  intent so other operators see your work (skill `meho:broadcast`).

## What stays local

- Repo-discipline rules (PR cadence, ticket + PR workflow) — they apply to
  repo work, not infra ops.
- Per-machine credentials — Vault is canonical for shared secrets; the
  operator's MEHO token lives in the local keyring /
  `~/.config/meho/credentials.json`.
- Repo-internal generators and sidecar conventions.

## When MEHO surfaces are unavailable

If `$MEHO_INSTANCE` is unreachable, you may fall back to local scripts.
Document the fallback in the ticket so operators with MEHO work in flight
know one operator is running without audit / broadcast / policy enforcement
until MEHO is back.

## Versioning

This plugin's version rides MEHO releases (see
`.claude-plugin/plugin.json`). After a MEHO upgrade, re-install the plugin
(`/plugin install meho@meho`) to pick up refreshed routing rules — this
replaces the copy-and-merge template refresh for Claude Code consumers.

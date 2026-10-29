# Solutions for one target live outside the backplane (decision)

**Status:** decided by the program owner on 27 October 2026, confirmed on 28 October 2026
**Date:** 2026-10-27
**Goal:** [#221](https://github.com/evoila/meho/issues/221)
**Surfaced by:** [#5078](https://github.com/evoila/meho/issues/5078), the migration assessment for Azure
**Applies to:** [#5077](https://github.com/evoila/meho/issues/5077) and the estate inventory Initiative [#5217](https://github.com/evoila/meho/issues/5217)

## The decision in short

The backplane takes no one-off work for a single target platform. Each piece goes to one of three places:

1. **The backplane, if it is the same for every target.** Examples: one inventory shape for servers,
   applications and Kubernetes workloads from any source; a result shape that shows how each number
   was made; an exporter seam for files other tools import. The Azure Migrate CSV file is the first
   exporter format. A format is a plain mapping, with no rule and no price.
2. **A connector, if it calls a vendor API.** Each call is a typed operation of that vendor's
   connector. Examples: the Azure Migrate import, the assessment create, the result reads, the price read.
3. **Outside the backplane, if it holds one target's rules, limits, prices, sizing, report, runbook
   template or guidance.** It lives in a separate repository of skills and scripts. That repository
   uses MEHO only through the CLI and MCP, like any other client.

## Why

- The backplane stays small and the same for every customer. Agents keep the same narrow set of tools.
- A target's rules and prices change often. Outside the backplane they get their own versions and releases.
- The facts a solution needs still come through MEHO, so every call is checked, audited and stored as before.

## What this changes

- #5078 is split. Its inventory Tasks (#5099, #5100, #5101) move to the estate inventory Initiative
  (#5217). Its sizing, cost, readiness, report and runbook Tasks (#5102 to #5108) leave the backplane.
- #5077 keeps every Azure read and write, and no estimate.
- A solution's results come back to MEHO as knowledge entries, so `search_knowledge` finds them.
  The knowledge entry is the lasting record of a report. MEHO gets no new feature for this.

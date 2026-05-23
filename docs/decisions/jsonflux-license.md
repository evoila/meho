# JSONFlux vendoring — the license path for the vendored reducer (decision)

**Status:** decided — Option B (Apache-2.0 relicensing)
**Date:** 2026-05-22
**Initiative:** [#750](https://github.com/evoila/meho/issues/750) — Real JSONFlux reducer, vendor from MEHO.X
**Task:** [#751](https://github.com/evoila/meho/issues/751) (this ADR)
**Blocks:** T2 ([#752](https://github.com/evoila/meho/issues/752)) vendor + headers, and transitively T3/T4/T5

## The decision this resolves

Initiative [#750](https://github.com/evoila/meho/issues/750) vendors the real
JSONFlux reducer into `backend/src/meho_backplane/jsonflux/`, sourced from the
MEHO.X-adjusted copy at `meho_app/jsonflux/` rather than the `ikaric/jsonflux`
MIT upstream — because MEHO.X has shipped 22 jsonflux-touching commits since the
fork, including the only behaviorally-different change (smart `register()` with
`unwrap`/`append`) plus the mypy/ruff/SonarQube hygiene the backplane would
otherwise regress on.

That source carries an incompatible license chain end-to-end:

- `ikaric/jsonflux` — **MIT** (`Copyright (c) 2026 Ilhan Karić`), single commit
  [`da85962`](https://github.com/ikaric/jsonflux/blob/main/LICENSE), no tags.
- MEHO.X copy — **AGPL-3.0-only**, relicensed via per-file SPDX headers
  (`# SPDX-License-Identifier: AGPL-3.0-only`) in MEHO.X commits `84-01` /
  `100-01`.
- `evoila/meho` — **Apache-2.0**, every file ships
  `# SPDX-License-Identifier: Apache-2.0`.

Apache-2.0 cannot ship AGPL-3.0 code: copyleft contamination would spread from
the vendored tree to the whole backplane. So the MEHO.X SPDX headers MUST NOT be
copied as-is. Before any file is vendored, one question must be answered because
it shapes the per-file SPDX header T2 writes, whether the repo grows a `LICENSES/`
carve-out, and how many signoffs the import needs:

> **Under what license does the MEHO.X-adjusted jsonflux code ship inside the
> Apache-2.0 `evoila/meho` repo, and from whom does that require signoff?**

## Options

### Option A — Re-import from MIT upstream, re-apply MEHO.X adjustments

Re-import the tree from `ikaric/jsonflux` [`da85962`](https://github.com/ikaric/jsonflux)
directly (clean MIT origin), then re-author the six MEHO.X-local change
categories from #750 on top — smart `register()`, mypy zero-error pass, ruff
zero-error pass, SonarQube cognitive-complexity suppressions, module-level
string-constant extraction — crediting the MEHO.X commits via `Co-Authored-By`.

- **Per-file SPDX:** `MIT` (third-party vendored carve-out) or `Apache-2.0`
  (relicense — still needs @ikaric signoff, since MIT permits sublicensing but
  the relicense should be on record).
- **LICENSE machinery:** add `LICENSES/MIT.txt` (REUSE spec 3.3 layout) if
  keeping MIT; nothing extra if relicensing to Apache-2.0.
- **Signoff:** ADR approval; @ikaric signoff only if relicensing.
- **Cost:** re-apply ~22 commits of MEHO.X work by hand against a different
  base — ~3355 LOC tree, ~500 net line-changes per the +/− table in #750. This
  is the slowest path and the one most likely to silently drop a hygiene fix.

### Option B — @ikaric signs off Apache-2.0 on the MEHO.X-adjusted code ✅ decided

Vendor the MEHO.X `meho_app/jsonflux/` tree at a pinned SHA, replace every
`# SPDX-License-Identifier: AGPL-3.0-only` header with
`# SPDX-License-Identifier: Apache-2.0`, and record @ikaric's relicensing
signoff plus a `NOTICE` attribution entry.

- **Per-file SPDX:** `Apache-2.0` — uniform with the rest of `evoila/meho`.
- **LICENSE machinery:** none. The whole repo is Apache-2.0; no `LICENSES/`
  carve-out, no REUSE per-file dual-license bookkeeping.
- **Signoff:** one — @ikaric, who is both the original `ikaric/jsonflux` author
  *and* the author of the MEHO.X `meho_app/jsonflux/` fork, and is on the MEHO
  team. As the copyright holder of the MIT origin and the AGPL fork, @ikaric can
  relicense to Apache-2.0; no other author's consent is required for the
  jsonflux tree itself.
- **Cost:** one signoff cycle plus a `NOTICE` update — paid once, no recurring
  license-management overhead.

### Option C — Vendored MIT carve-out inside the Apache-2.0 repo

Vendor the MEHO.X tree at the same pin, replace the AGPL headers with
`# SPDX-License-Identifier: MIT`, and keep the vendored tree under MIT as a
documented third-party carve-out inside the otherwise-Apache-2.0 repo.

- **Per-file SPDX:** `MIT` on every vendored file — a license island inside the
  repo.
- **LICENSE machinery:** add `LICENSES/MIT.txt` per REUSE spec 3.3; the
  `docs/architecture/jsonflux.md` runbook (T5) must document the carve-out so
  future contributors don't "fix" the headers to Apache-2.0.
- **Signoff:** the worst of the three. The MEHO.X tree is AGPL and some of the
  22 commits are authored by `@kr3s0` / `@ddzafic` / `@zdamir`, not only
  @ikaric. Shipping their AGPL-relicensed contributions back out under MIT
  requires a **per-commit author-chain audit and per-author consent**, because
  MIT-out is not something @ikaric alone can grant for code others wrote.
- **Cost:** REUSE machinery (forever) **and** a multi-author signoff chain
  (up front). Strictly dominated by the other two options.

## Recommendation: Option B (Apache-2.0 relicensing)

Decided. Four reasons, in priority order:

1. **Cleanest legal posture.** A single license — Apache-2.0 — across the entire
   repo. No license island, no per-file dual-licensing, no future contributor
   tripping over an MIT header in an Apache-2.0 tree.
2. **Lowest ongoing maintenance burden.** No `LICENSES/` directory, no REUSE
   per-file bookkeeping to keep in sync as the vendored tree drifts on each
   upstream-sync. Option A (if it keeps MIT) and Option C both carry this forever;
   Option B carries it never.
3. **Shortest critical path.** One signoff from @ikaric — who holds the copyright
   on both the MIT origin and the AGPL fork — versus Option C's multi-author
   consent chain (other MEHO.X authors must agree to MIT-out their AGPL work) or
   Option A's 22-commit manual re-application against a different base.
4. **No regression risk on the hygiene MEHO.X already paid for.** Option B
   vendors the MEHO.X tree as-is (headers aside), so the smart `register()`
   behavior and the mypy/ruff/Sonar cleanups arrive intact. Option A re-applies
   them by hand and is the most likely to drop one silently.

Option A is the fallback **only** if @ikaric declines to relicense. Option C is
not a fallback — it is strictly worse than both (it needs the carve-out machinery
*and* a multi-author chain).

## Signoff of record

@ikaric (Ilhan Karić) — original author of `ikaric/jsonflux` and of the MEHO.X
`meho_app/jsonflux/` fork — has approved **Option B**, relicensing the
MEHO.X-adjusted jsonflux code to Apache-2.0 for vendoring into `evoila/meho`.

The maintainer attestation captured on 2026-05-22 is the signoff of record:

- <https://github.com/evoila/meho/issues/751#issuecomment-4522532344>

## What T2 vendors (the verbatim handoff)

**SPDX header — exact text T2 applies to every vendored file**, replacing the
MEHO.X `AGPL-3.0-only` header:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
```

This matches the header convention already used across `evoila/meho` source
(verified against `backend/src/meho_backplane/version.py`, `audit.py`,
`logging.py`, `metrics.py`, and 231 other files carrying the identical two-line
form). T2 swaps only line 1 (`AGPL-3.0-only` → `Apache-2.0`); the
`# Copyright (c) 2026 evoila Group` line is already present on the MEHO.X files
and stays as-is.

**Source pin — the MEHO.X commit T2 vendors from:**

- `fc82cf93` — `fix(types): remove 65 unused type: ignore comments across 38
  files` (2026-04-01), the latest commit touching `meho_app/jsonflux/` and the
  latest mypy-clean state as of 2026-05-20.

This pin is a **recommendation, not a freeze.** Per #751 "Out of scope", T2 may
refresh to a newer mypy-clean MEHO.X HEAD if one exists at T2 start. **T2 MUST
record whatever SHA it actually pins** — both in the #750 Initiative body (the
canonical source-pin field) and in the `docs/architecture/jsonflux.md`
provenance section that T5 ships.

**Upstream provenance for the license chain (record only — not the vendor
source):**

- `da85962` — `ikaric/jsonflux` "Initial commit" (MIT). Single commit, no tags;
  `__version__ = "1.0.0"` in code with no matching git tag. MEHO.X downgraded its
  fork's `__version__` to `"0.1.0"` to signal it is a fork. Recorded so the
  MIT → AGPL → (relicensed) Apache-2.0 chain is auditable.

**NOTICE attribution (T2 adds it).** Option B requires a `NOTICE` entry
attributing the vendored code to its original author. The repo already has a
`NOTICE` at root (`MEHO / Copyright 2026 evoila Group / ...`); T2 appends an
attribution for the vendored jsonflux tree crediting @ikaric / `ikaric/jsonflux`
as the upstream origin of the relicensed code.

## Consequences

- T2 ([#752](https://github.com/evoila/meho/issues/752)) is unblocked: it
  vendors the MEHO.X tree, applies the Apache-2.0 SPDX header above to every
  file, adds the `NOTICE` attribution entry, and adds `duckdb` + `pyarrow` to
  `backend/pyproject.toml`. No `LICENSES/` directory and no REUSE machinery are
  introduced.
- The vendored tree is Apache-2.0 like the rest of the repo — no license island,
  no carve-out documentation burden, no per-file dual-license to maintain on
  future upstream-syncs.
- The `da85962` (MIT) → MEHO.X (AGPL) → `evoila/meho` (Apache-2.0) chain is
  documented here and again in `docs/architecture/jsonflux.md` (T5), so the
  relicensing is auditable and the AGPL-header gotcha (#750 §"License chain is
  INCOMPATIBLE end-to-end") cannot silently recur on the next sync.

## References

- Parent Initiative [#750](https://github.com/evoila/meho/issues/750) —
  §"License chain is INCOMPATIBLE end-to-end".
- Signoff of record:
  <https://github.com/evoila/meho/issues/751#issuecomment-4522532344>
- REUSE specification 3.3 (SPDX header conventions for vendored third-party
  code): <https://reuse.software/spec-3.3/>
- SPDX license list: <https://spdx.org/licenses/>
- `ikaric/jsonflux` LICENSE (MIT, `Copyright (c) 2026 Ilhan Karić`):
  <https://github.com/ikaric/jsonflux/blob/main/LICENSE>
- Apache-2.0 / FSF license compatibility:
  <https://www.gnu.org/licenses/license-list.en.html>
- Header-convention reference in this repo: `backend/src/meho_backplane/version.py`.

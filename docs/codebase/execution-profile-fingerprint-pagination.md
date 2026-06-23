# Profile-driven fingerprint / probe + named pagination

> Durable map of the declarative fingerprint/probe and pagination half of
> the `ExecutionProfile` (G0.28-T6 #1972, Initiative #1965, Goal #1964).
> Update in lock-step with code changes; stale entries are bugs.

## Overview

An ingested REST connector becomes dispatchable by attaching a reviewed
`ExecutionProfile`
(`backend/src/meho_backplane/connectors/profile.py`). T3 (#1969) defined
the schema + the named auth catalog; T6 (#1972) adds the declarative
`fingerprint` / `probe` recipes and a named pagination strategy so a
profiled connector reports a real fingerprint/probe (not the auto-shim's
`reachable=False` placeholder) and follows a cursor for list ops — all
from reviewed data, no hand-coded Python and **no path/expression DSL**.

The load-bearing constraint is the #1177 substrate-minimalism line:
**response-field selection is always a single literal top-level key.** A
recipe names `response[key]`, never a dotted path / JSONPath / wildcard /
filter. The schema rejects a key containing `.` `[` `]` or `*` at the
Pydantic boundary, so the "no dotted paths" rule is enforced
mechanically, not just by review.

## Key types

All in `connectors/profile.py`, all frozen + `extra="forbid"`:

- **`FingerprintSpec`** — `path` (GET), `authenticated` (bool),
  `version_key` (literal top-level key carrying the version string),
  `version_splitter` (named enum). Drives
  `ProfiledRestConnector.fingerprint`.
- **`ProbeSpec`** — `path` (GET health), `ok_field` (literal top-level
  key), `ok_value`. `ExecutionProfile.probe` is either the `'delegate'`
  sentinel (probe via the fingerprint round-trip — the SDDC/NSX
  precedent) or a `ProbeSpec`.
- **`PaginationSpec`** — `strategy` (`none` | `cursor_token`), `items_key`
  (literal top-level key under which each page's rows live), `cursor`
  (`CursorTokenPagination`, required iff `strategy='cursor_token'`).
- **`CursorTokenPagination`** — `req_param` (query param the next cursor
  is sent under) + `resp_field` (literal top-level key carrying the next
  cursor).
- **`VersionSplitter`** — the closed splitter enum (see below).
- **`split_version(splitter, raw) -> (version, build)`** — the single
  dispatch point over the splitter enum.

### Named version splitters (closed enum, not a format string)

| value | shape | grounded in |
|-------|-------|-------------|
| `none` | version verbatim, no build | clean `MAJOR.MINOR.PATCH` endpoints |
| `dash` | split on first `-`: `v2.11.0-abc1234` → `(v2.11.0, abc1234)` | harbor `_parse_harbor_version` |
| `vrli_five_part` | dot-split `9.0.0.0.21761695` → `(9.0.0, 21761695)` (`parts[0:3]` joined, `parts[4]`) | vcf_logs `_parse_vrli_version` |

Adding a value is a deliberate act backed by a real connector's parse
shape. There is no `regex` / `custom` escape hatch — that would re-open
the rejected-DSL door (#1177).

## Control flow

- **`ProfiledRestConnector.fingerprint`** (`connectors/profiled.py`) GETs
  `FingerprintSpec.path` (authenticated via `_get_json` when
  `authenticated=True`, else via the no-auth `_get_unauthenticated_json`
  seam), reads `response[version_key]`, and renders `(version, build)`
  via `split_version`. Transport/status failure → `reachable=False` with
  `extras["error"]` (the harbor/SDDC/NSX failure shape).
- **`ProfiledRestConnector.probe`** — `'delegate'` → run the fingerprint
  and report `ok = reachable`; a `ProbeSpec` → GET the health path
  (unauthenticated) and compare `response[ok_field]` against `ok_value`.
- **`dispatch_ingested`** (`operations/_branches.py`) — for an idempotent
  (GET) ingested op whose connector carries a profile with
  `strategy='cursor_token'`, `_dispatch_ingested_cursor_token` loops: each
  page merges the next cursor under `cursor.req_param`, concatenates
  `response[items_key]`, and reads the next cursor from
  `response[cursor.resp_field]`; the loop stops when that field is falsy.
  Returns the assembled `{items_key: [...], total: N}` — the same
  unwrapped shape the hand-coded gcloud paginators return. `strategy='none'`
  and a profile-less connector both take the single-request path.

## Out of scope

- **Link-header / offset pagination** — net-new; file a separate task
  when a vendor needs it (#1972 out-of-scope note).
- **Migrating typed paginators** (gcloud's hand-rolled `nextPageToken`
  loops) — typed ops don't dispatch through `dispatch_ingested`.
- **Profile auth wiring** — `auth_headers` still raises until T4 (#1970).

## Dependencies

- `ExecutionProfile` schema + auth catalog (#1969), `ProfiledRestConnector`
  + tri-state classifier (#1967).
- The profile is stamped onto the synthesised connector class at review
  time (`record_profile_stamp`, #1971); the base class carries
  `profile = None`, and a registered profiled class always carries a
  concrete profile.

## Known issues

- `profile.py` is ~640 lines (over the 600-line code-quality soft limit).
  Splitting the fingerprint/probe/pagination sub-models into a sibling
  module is a candidate follow-up; kept together here because they are
  one cohesive schema surface.

## References

- `connectors/profile.py`, `connectors/profiled.py`,
  `operations/_branches.py`.
- Grounding: `docs/codebase/connector-auth-coverage.md`,
  `connectors/harbor/connector.py` (`_parse_harbor_version`),
  `connectors/vcf_logs/connector.py` (`_parse_vrli_version`),
  `connectors/gcloud/connector.py` (cursor-token paginators).
- Parent #1964 / #1965. The literal-key constraint is the #1177
  "no JSONPath" line.

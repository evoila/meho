# Governed ISO path: content-library import-from-URL + iso.image mount (vmware-rest)

## Overview

The governed way to get a bootable ISO from an HTTP depot into a
vCenter content library and mounted on a VM — entirely on **raw
ingested ops** of the `vmware-rest` connector (`vmware-rest-9.0`),
dispatched through the ordinary
`call_operation(connector_id, operation_id, target, params)` path with
policy, audit, broadcast, and (when tightened) approvals applied per
call. No composite exists and none is needed: every step is a single
`call_operation`, the multi-step choreography is four writes plus one
poll read, and each response is small and self-describing (#3086).

First consumer: the governed nested-ESXi substrate build — the ESX
installer ISO sits on an HTTP depot and must reach a VM's CD-ROM
without out-of-band `scp`/`govc`.

Verification basis (no live vCenter): the pinned
`vcenter-9.0/vcenter.yaml` on the spec shelf (parsed with the real
ingest parser), the generic dispatcher's request construction
(`operations/_branches.py`), and respx full-dispatch unit tests with
mocked vendor responses. Live-appliance verification is deferred (see
Known gaps).

## The recipe

All op_ids below are byte-for-byte the strings the G0.7 ingest of
`vcenter.yaml` emits (and therefore what `search_operations` /
`call_operation` resolve). Prerequisite: the vcenter spec is ingested
and the carrying groups are enabled — these are ingested rows, so they
do not exist on a fresh boot the way the hand-coded composites do.

Import-from-URL (server-side **PULL** — vCenter fetches the file
itself; no file bytes transit MEHO):

| # | Step | op_id | Params (shape) | Result envelope |
|---|------|-------|----------------|-----------------|
| 0a | List library ids | `GET:/content/library` | — | `list[str]` |
| 0b | List item ids in a library | `GET:/content/library/item?library_id` | `{library_id}` | `list[str]` |
| 1 | Create the item | `POST:/content/library/item` | `{body: {library_id, name, type: "iso", description?}}` | `{"value": "<item-id>"}` |
| 2 | Create the update session | `POST:/content/library/item/update-session` | `{body: {library_item_id}}` | `{"value": "<session-id>"}` |
| 3 | Add the PULL file | `POST:/content/library/item/update-session/{updateSessionId}/file` | `{updateSessionId, body: {name, source_type: "PULL", source_endpoint: {uri: "http://depot/…"}}}` | file-info object (`status: WAITING_FOR_TRANSFER → …`) |
| 4 | Complete the session | `POST:/content/library/item/update-session/{updateSessionId}?action=complete` | `{updateSessionId}` (bodyless) | `{}` (204) |
| 5 | Poll to terminal state | `GET:/content/library/item/update-session/{updateSessionId}` | `{updateSessionId}` | session object — `state: ACTIVE \| DONE \| ERROR \| CANCELED`, `client_progress`, `error_message` |

Failure/liveness companions (same family, same dispatch shape):
`?action=cancel` (abort), `?action=fail` (body
`{client_error_message}`), `?action=keep-alive` (optional body
`{client_progress}` — call it during a long pull; sessions carry an
`expiration_time`), and
`GET:/content/library/item/update-session/{updateSessionId}/file` for
per-file transfer status.

Mount / unmount (Iso.Image API — creates/removes a VM CD-ROM device
backed by the library item's ISO):

| # | Step | op_id | Params (shape) | Result envelope |
|---|------|-------|----------------|-----------------|
| 6 | Mount | `POST:/vcenter/iso/image?action=mount` | `{body: {library_item, vm}}` (both required) | `{"value": "<cdrom-id>"}` |
| 7 | Unmount | `POST:/vcenter/iso/image?action=unmount` | `{body: {vm, cdrom}}` (both required) | `{}` (204) |

The `cdrom` argument of unmount is the string mount returned. `type:
"iso"` on step 1 selects the ISO type-adapter plugin so the item
publishes as a mountable ISO.

The canonical machine-readable form of this table is
`backend/tests/_governed_iso_recipe.py` — both test lanes consume it,
so the doc, the wire pins, and the spec grounding cannot fork
silently.

## Control flow — why the raw ingested path just works

The #3071/#2973 `/rest`-vs-`/api` body-envelope bug class lives in
**hand-coded composite** body construction. The generic ingested path
is envelope-clean by construction, which is why this recipe needs no
connector code:

- **Body**: the ingest parser folds each op's `requestBody` into a
  single container param named `body`
  (`x-meho-param-loc: "body"`); at dispatch,
  `_unwrap_body` (`operations/_branches.py`) sends that param's
  **value** as the JSON body — top-level, verbatim, no wrapper. The
  pinned spec declares every body in this recipe as the flat `/api`
  shape (ItemModel / UpdateSessionModel / AddSpec / mount + unmount
  field pairs at the top level), so caller-supplied `body` ==
  wire body.
- **`?action=` discriminators**: vCenter's OpenAPI keys these
  endpoints with the `?action=<verb>` suffix *in the path key itself*;
  the parser passes it through into `op_id` and `path`, and httpx
  carries it onto the wire. The same applies to the required-query
  marker form `GET:/content/library/item?library_id` — the query
  bucket merges into the marker, yielding exactly one
  `library_id=<value>` pair.
- **Mount prefix**: `VmwareRestConnector.mount_op_path` prefixes the
  spec-relative path with `/api` (modern) or `/rest` (legacy fallback)
  per the established session (`connectors/vmware_rest/_mount.py`).
  Note the legacy `/rest` mount would *not* accept these flat bodies —
  the recipe assumes the modern `/api` mount, which every supported
  target (vSphere 8.5+/9.0) serves.
- **Response envelopes**: create/mount ack with a **bare JSON string**
  (item id / session id / cdrom id); `wrap_ok_result`
  (`operations/_errors.py`) wraps scalars as `{"value": …}` so
  `OperationResult.result` stays dict/list-shaped. The bodyless acks
  (`?action=complete`, unmount) return `204 No Content` →
  `{}` (#3082). Dict/list payloads (session state, id lists) pass
  through; they are far below the JSONFlux thresholds, so no result
  handle is produced.

## Governance metadata

As ingested from the pinned spec (method-based heuristic in
`operations/ingest/openapi.py`):

- Every write in the recipe: `safety_level="caution"`,
  `requires_approval=False` — a human/service operator auto-executes
  (with the synchronous audit row + broadcast per dispatch); agent
  principals resolve through the per-(principal, op, target) grant
  model, with `requires_approval` as a needs-approval floor.
- The reads: `safety_level="safe"`.
- An operator can tighten any of these per op in ingest review;
  `requires_approval=True` parks the dispatch in the approval queue
  *before* any vendor call fires (pinned in the dispatch lane).
- Mount/unmount are VM writes (they add/remove a CD-ROM device);
  vCenter additionally enforces its own privilege set
  (`ContentLibrary.DownloadSession` / VM device privileges) on the
  service account.

## Known gaps

- **PUSH upload cannot ride the generic dispatcher.** An update
  session with `source_type: "PUSH"` expects the client to stream file
  bytes to the returned `upload_endpoint` via raw HTTP PUT — there is
  no ingested-op transport for raw byte streams. The governed answer
  is PULL (this recipe); a push-style import would need a typed op.
  Symmetrically: the PULL source URL must be reachable **from the
  vCenter appliance** (schemes `http`, `https`, `file`, `ds`), not
  from MEHO.
- **No task-shaped completion.** `?action=complete` acks immediately;
  the transfer itself is asynchronous. The caller owns the poll loop
  (step 5) until `state` reaches `DONE`/`ERROR` — there is no
  `vmw-task` variant in this API family to lean on.
- **Session expiry.** Update sessions expire (`expiration_time`); a
  slow depot pull may need `?action=keep-alive` calls. The recipe
  keeps that on the caller.
- **Ingested-op prerequisite.** Unlike the write composites (which
  work on a fresh boot), these rows exist only after the operator
  ingests `vcenter.yaml` and enables the groups carrying
  `Content.Library.Item*` and `Vcenter.Iso.Image`.
- **Live-appliance verification deferred.** Everything here is
  grounded on the pinned spec + dispatch-path unit tests with mocked
  vendor responses; the first live run (real depot, real vCenter)
  should confirm transfer-state transitions and the mount's returned
  cdrom id round-trip. (Generic-dispatcher adjacent finding, not a
  recipe blocker: ingested **non-idempotent** ops drop query-located
  params — `dispatch_ingested` does not forward `request.query` to
  `_post_json`. No op in this recipe carries one; every POST
  discriminator here is embedded in the path key.)

## References

- Tests: `backend/tests/test_connectors_vmware_rest_governed_iso_dispatch.py`
  (always-on wire + envelope pins),
  `backend/tests/test_connectors_vmware_rest_governed_iso_reconcile.py`
  (shelf-gated path + body-shape + required-field grounding),
  `backend/tests/_governed_iso_recipe.py` (canonical op table).
- Precedents: #2973 / #3071 (`/rest`-vs-`/api` envelope class),
  #3082/#3083 (204/empty-body acks), #3084/#3085 (scalar preservation),
  `docs/decisions/spec-reconcile-guards-standard.md` (#2979/#2980).
- Related connector doc: `docs/codebase/connectors-vmware-rest.md`
  (composites, mounts, session handling); the `vm.device.cdrom`
  composite covers datastore-path ISO pinning — a *different* flow
  from the library-backed Iso.Image mount documented here.
- Issue: evoila/meho#3086 (nested-substrate prerequisite).

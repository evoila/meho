// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
)

// moidPatterns lists the per-kind regular expressions used to detect
// "input is already a moid; skip the resolve round-trip" cases.
// Operators paste moids when they have them (from the vSphere UI's
// URL bar, from `govc` output, etc.); the helper transparently
// passes those through. The patterns mirror vSphere's documented
// moid grammar (datacenter-N, folder-N, group-NN, host-N, etc.).
//
// The vSphere REST API itself doesn't pin a strict moid grammar —
// the strings happen to land in shapes vCenter generates internally,
// and a typo'd "vm-X" with a non-numeric tail would surface as a
// not-found at the dispatcher rather than here, which is the right
// failure mode (we don't want to second-guess the backplane).
var moidPatterns = map[string]*regexp.Regexp{
	"vm":         regexp.MustCompile(`^vm-\d+$`),
	"host":       regexp.MustCompile(`^host-\d+$`),
	"cluster":    regexp.MustCompile(`^domain-c\d+$`),
	"datacenter": regexp.MustCompile(`^datacenter-\d+$`),
	"datastore":  regexp.MustCompile(`^datastore-\d+$`),
	"network":    regexp.MustCompile(`^network-\d+$`),
}

// isMoid returns true when name matches the moid grammar for kind.
// Falls back to false for unknown kinds — name-resolution then runs
// against the filter API which produces a clearer not-found message
// than client-side guessing would.
func isMoid(kind, name string) bool {
	pat, ok := moidPatterns[kind]
	if !ok {
		return false
	}
	return pat.MatchString(name)
}

// listEntry is the per-row shape vSphere's GET /vcenter/<kind>
// endpoints return inside `value`. v0.2 only needs the moid field
// (named per the vSphere docs: `vm`, `host`, `cluster`, etc.) and
// the human-readable `name`; everything else is decoded into
// json.RawMessage so the resolver doesn't have to track schema
// drift on unused fields.
//
// The moid field name differs by kind. The decoder pulls both the
// canonical name field (`name`) and the kind-specific moid field;
// resolveName picks the right moid field at call time so this struct
// can stay generic.
type listEntry map[string]any

// moidFieldForKind returns the per-kind moid field name in the
// vSphere REST response. The vSphere documentation pins these:
//   - /vcenter/vm         → object has `vm` field
//   - /vcenter/host       → object has `host` field
//   - /vcenter/cluster    → object has `cluster` field
//   - /vcenter/datacenter → object has `datacenter` field
//   - /vcenter/datastore  → object has `datastore` field
//   - /vcenter/network    → object has `network` field
func moidFieldForKind(kind string) string {
	return kind
}

// resolveName resolves a human-readable name to a vSphere moid by
// calling GET /vcenter/<kind>?filter.names=<name> through the
// backplane dispatcher and inspecting the returned list. Three
// outcomes:
//
//   - input matches the moid grammar for kind → returned verbatim
//     (no round-trip). Operators with moids in hand skip the call.
//   - input is a name + exactly one match → moid returned.
//   - input is a name + zero matches → error "no <kind> named …".
//   - input is a name + multiple matches → error listing all
//     candidate moids with their folder/cluster context so the
//     operator can re-invoke with the moid directly.
//
// The vSphere REST API's filter.names parameter is documented as
// "list of names to filter by"; passing a single value returns
// zero-or-more matches under the same code path as a multi-value
// query. The dispatcher's status==error path surfaces as a returned
// error from this helper so per-verb runners can translate it into
// the right exit code (1, treating bad input the same as a
// dispatcher-reported failure).
func resolveName(
	ctx context.Context,
	backplaneURL, targetSlug, kind, name string,
) (string, error) {
	if name == "" {
		return "", fmt.Errorf("empty %s name", kind)
	}
	if isMoid(kind, name) {
		return name, nil
	}
	// Build the GET op_id matching how the backplane registers
	// vSphere REST GET endpoints: `GET:/vcenter/<kind>`. The dispatcher
	// applies query params from the params map; vSphere's filter syntax
	// is `filter.names=<comma-separated>` (or, for some 9.0 endpoints,
	// a `names` field on a filter object — the backplane normalises
	// both shapes through the same endpoint_descriptor).
	opID := fmt.Sprintf("GET:/vcenter/%s", kind)
	params := map[string]any{
		"filter.names": name,
	}
	r, err := dispatchOp(ctx, backplaneURL, opID, targetSlug, params)
	if err != nil {
		return "", err
	}
	if r.Status != "ok" {
		if r.Error != nil && *r.Error != "" {
			return "", fmt.Errorf("resolve %s %q failed: %s", kind, name, *r.Error)
		}
		return "", fmt.Errorf("resolve %s %q failed: dispatcher status %s", kind, name, r.Status)
	}
	matches, err := decodeListResult(r.Result)
	if err != nil {
		return "", fmt.Errorf("resolve %s %q: %w", kind, name, err)
	}
	switch len(matches) {
	case 0:
		return "", fmt.Errorf("no %s named %q on target %q; check the name or pass the vSphere moid directly", kind, name, targetSlug)
	case 1:
		moid, ok := matches[0][moidFieldForKind(kind)].(string)
		if !ok || moid == "" {
			return "", fmt.Errorf("resolve %s %q: vSphere response missing %q field", kind, name, moidFieldForKind(kind))
		}
		return moid, nil
	default:
		// Ambiguous: pull every candidate moid into the error message
		// so the operator can re-invoke with the moid directly. Folder /
		// cluster context fields are surfaced when present (vSphere
		// returns these for vm + host queries) but stay best-effort —
		// the moids themselves are the load-bearing disambiguator.
		var detail []string
		moidField := moidFieldForKind(kind)
		for _, m := range matches {
			candidate := fmt.Sprintf("%v", m[moidField])
			if pathCtx, ok := contextHint(kind, m); ok {
				candidate = fmt.Sprintf("%s (%s)", candidate, pathCtx)
			}
			detail = append(detail, candidate)
		}
		return "", fmt.Errorf("name %q resolved to %d candidates: %s; pass the moid directly", name, len(matches), joinComma(detail))
	}
}

// decodeListResult handles both vSphere response shapes the
// dispatcher can pass through:
//
//   - `{"value": [{...}, {...}]}` — the v0.1 wrapping shape (vSphere
//     6.x / 7.x compatibility envelope; still emitted by some 9.0
//     endpoints when an `Accept` header asks for it).
//   - `[{...}, {...}]` — the 9.0 bare-array shape (the dispatcher's
//     pre-T2 unwrap was load-bearing for the test fixtures).
//
// Both shapes decode into the same `[]listEntry` so the resolver
// caller doesn't have to branch on response shape.
func decodeListResult(raw json.RawMessage) ([]listEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	// Try the bare-array shape first; it's the canonical vSphere 9.0
	// response. A failed decode falls through to the value-wrapped
	// shape rather than erroring (UnmarshalTypeError on object-vs-
	// array kicks in cleanly).
	var arr []listEntry
	if err := json.Unmarshal(raw, &arr); err == nil {
		return arr, nil
	}
	var wrapped struct {
		Value []listEntry `json:"value"`
	}
	if err := json.Unmarshal(raw, &wrapped); err != nil {
		return nil, fmt.Errorf("decode list result: %w", err)
	}
	return wrapped.Value, nil
}

// contextHint pulls a kind-specific path hint out of a vSphere list
// entry so the ambiguous-name error message can surface
// folder/cluster context alongside the bare moid. Best-effort —
// returns ok=false when the entry has no such field, and the caller
// renders the candidate moid alone.
func contextHint(kind string, entry listEntry) (string, bool) {
	switch kind {
	case "vm":
		// vm entries on 9.0 carry `name` + `power_state` + the moid;
		// folder context is not in the default response. Skip.
		return "", false
	case "host":
		// host entries carry `connection_state` which doubles as a
		// useful disambiguator alongside the moid.
		if v, ok := entry["connection_state"].(string); ok && v != "" {
			return "state=" + v, true
		}
		return "", false
	case "cluster":
		if v, ok := entry["name"].(string); ok && v != "" {
			return "name=" + v, true
		}
		return "", false
	default:
		return "", false
	}
}

// joinComma joins strings with ", " separators. Trivial helper kept
// inline because strings.Join would suffice but the linter prefers
// a single allocation site for sequence-aware formatting.
func joinComma(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	out := parts[0]
	for _, p := range parts[1:] {
		out += ", " + p
	}
	return out
}

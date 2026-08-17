// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/evoila/meho/cli/internal/typedops"
)

// typedDispatchCases pins every Fleet verb whose backend read is typed
// to the dotted typed op_id it must dispatch (#2266 repoint sweep
// #2355). Both are no-param reads, so each swap was a pure op_id
// change.
var typedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	{"about", []string{"about"}, "fleet.about"},
	{"environment list", []string{"environment", "list"}, "fleet.environment.list"},
}

// ingestedDispatchCases pins the Fleet verbs that legitimately
// dispatch ingested METHOD:/path op_ids — each entry records why no
// typed op_id exists, so the intent is pinned rather than silently
// unguarded (#2942). When the backend ships a typed counterpart, move
// the verb to typedDispatchCases and reclassify it in
// typedOpCLICoverage.
var ingestedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	// No typed datacenter/vcenter/product reads.
	{"datacenter list", []string{"datacenter", "list"}, "GET:/lcm/lcops/api/v2/datacenters"},
	{"vcenter list", []string{"vcenter", "list", "dc-1"}, "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters"},
	{"product list", []string{"product", "list", "env-1"}, "GET:/lcm/lcops/api/v2/environments/{environmentId}/products"},
	// Detail read with no typed counterpart (fleet.environment.list is
	// list-only).
	{"environment info", []string{"environment", "info", "env-1"}, "GET:/lcm/lcops/api/v2/environments/{environmentId}"},
	// No typed request reads (initiative #2833 ranks them medium tier).
	{"request list", []string{"request", "list"}, "GET:/lcm/request/api/v2/requests"},
	{"request info", []string{"request", "info", "req-1"}, "GET:/lcm/request/api/v2/requests/{requestId}"},
}

// typedOpCLICoverage classifies every typed op_id the backend registers
// for vcf_fleet: the CLI verb that dispatches it, or "" when the op is
// agent-surface-only (no CLI verb wraps it).
// TestBackendTypedOpsAreClassified fails when the backend gains a
// typed op that is not classified here — the "typed op ⇒ repoint the
// CLI verb ⇒ pin it" contract (#2942).
var typedOpCLICoverage = map[string]string{
	"fleet.about":            "about",
	"fleet.environment.list": "environment list",
}

// TestRepointedVerbsDispatchTypedOpIDs pins that the repointed Fleet
// verbs dispatch their typed op_ids.
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	for _, tc := range typedDispatchCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			assertVerbDispatchesOpID(t, tc.args, tc.wantOp)
		})
	}
}

// TestIngestedVerbsDispatchPinnedOpIDs pins the verbs that stay on
// ingested op_ids on purpose (see ingestedDispatchCases for the
// per-verb reasons).
func TestIngestedVerbsDispatchPinnedOpIDs(t *testing.T) {
	for _, tc := range ingestedDispatchCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			assertVerbDispatchesOpID(t, tc.args, tc.wantOp)
		})
	}
}

// TestBackendTypedOpsAreClassified reconciles the backend's typed-op
// registry against typedOpCLICoverage so a typed op shipped without a
// CLI-repoint decision fails this test instead of leaving the verb a
// zero-catalog dead end (#2942).
func TestBackendTypedOpsAreClassified(t *testing.T) {
	inventory, err := typedops.BackendOpIDs("vcf_fleet")
	if err != nil {
		t.Fatalf("read backend typed-op inventory: %v", err)
	}
	invSet := make(map[string]bool, len(inventory))
	for _, id := range inventory {
		invSet[id] = true
	}
	for _, id := range inventory {
		if _, ok := typedOpCLICoverage[id]; !ok {
			t.Errorf("backend typed op %q is unclassified — repoint the CLI verb "+
				"covering this read (and add it to typedDispatchCases) or record it "+
				"as agent-surface-only in typedOpCLICoverage (#2942 contract)", id)
		}
	}
	for id := range typedOpCLICoverage {
		if !invSet[id] {
			t.Errorf("typedOpCLICoverage entry %q no longer exists in the backend "+
				"registry — drop or rename it", id)
		}
	}
	for _, tc := range typedDispatchCases {
		if !invSet[tc.wantOp] {
			t.Errorf("typed dispatch case %q asserts op_id %q that the backend "+
				"does not register", tc.name, tc.wantOp)
		}
	}
}

// assertVerbDispatchesOpID executes the verb against a mock backplane
// and asserts the op_id it puts on the wire.
func assertVerbDispatchesOpID(t *testing.T, args []string, wantOp string) {
	t.Helper()
	var gotOp string
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			gotOp = body.OpID
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs(append(args, "--target", "rdc-fleet", "--backplane", srv.URL))
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("execute %v: %v", args, err)
	}
	if gotOp != wantOp {
		t.Errorf("op_id: got %q want %q", gotOp, wantOp)
	}
}

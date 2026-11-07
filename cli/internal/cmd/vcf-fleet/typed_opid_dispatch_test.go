// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// typedDispatchCases pins every Fleet verb whose backend read is typed
// to the dotted typed op_id it must dispatch (#2266 repoint sweep
// #2355). environment info, request list, request info and datacenter
// list moved here when their typed reads shipped; the two with an id
// send it as environment_id / request_id.
var typedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	{"about", []string{"about"}, "fleet.about"},
	{"environment list", []string{"environment", "list"}, "fleet.environment.list"},
	{"environment info", []string{"environment", "info", "env-1"}, "fleet.environment.get"},
	{"request list", []string{"request", "list"}, "fleet.request.list"},
	{"request info", []string{"request", "info", "req-1"}, "fleet.request.get"},
	{"datacenter list", []string{"datacenter", "list"}, "fleet.datacenter.list"},
}

// ingestedDispatchCases pins the Fleet verbs that legitimately
// dispatch ingested METHOD:/path op_ids — each entry records why no
// typed op_id exists, so the intent is pinned rather than silently
// unguarded (#2942). When the backend ships a typed counterpart, move
// the verb to typedDispatchCases.
var ingestedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	// No typed per-datacenter vCenter list or product list
	// (fleet.datacenter.list covers all datacenters at once).
	{"vcenter list", []string{"vcenter", "list", "dc-1"}, "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters"},
	{"product list", []string{"product", "list", "env-1"}, "GET:/lcm/lcops/api/v2/environments/{environmentId}/products"},
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

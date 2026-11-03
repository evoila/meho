// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// typedDispatchCases pins every VCFA verb whose backend read is typed
// to the dotted typed op_id it must dispatch (#2266 repoint sweep
// #2355; deployment list repointed by #2942 once #2839 shipped the
// typed op; deployment get repointed by #2960 once the typed detail
// op shipped). The dual-plane `about` verb is pinned separately by
// TestAboutVerbDispatchesPerPlane / TestAboutOpForPlane.
var typedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	{"org list", []string{"org", "list"}, "vcfa.provider.org.list"},
	{"region list", []string{"region", "list"}, "vcfa.provider.region.list"},
	{"project list", []string{"project", "list"}, "vcfa.tenant.project.list"},
	{"deployment list", []string{"deployment", "list"}, "vcfa.tenant.deployment.list"},
	{"deployment get", []string{"deployment", "get", "dep-1"}, "vcfa.tenant.deployment.get"},
}

// ingestedDispatchCases pins the VCFA verbs that legitimately dispatch
// ingested METHOD:/path op_ids — each entry records why no typed
// op_id exists, so the intent is pinned rather than silently
// unguarded (#2942). When the backend ships a typed counterpart, move
// the verb to typedDispatchCases.
var ingestedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	// Provider-plane detail reads with no typed counterpart (the typed
	// provider surface ships list ops only).
	{"org get", []string{"org", "get", "org-1"}, "GET:/cloudapi/1.0.0/orgs/{id}"},
	{"region get", []string{"region", "get", "reg-1"}, "GET:/cloudapi/1.0.0/regions/{id}"},
	// No typed blueprint/user reads (initiative #2833 ranks them low
	// tier, not planned).
	{"blueprint list", []string{"blueprint", "list"}, "GET:/iaas/api/blueprints"},
	{"user list", []string{"user", "list"}, "GET:/cloudapi/1.0.0/users"},
}

// TestRepointedVerbsDispatchTypedOpIDs pins that the repointed VCFA
// verbs dispatch their typed op_ids at the command-constructor site.
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
	root.SetArgs(append(args, "--target", "rdc-vcfa", "--backplane", srv.URL))
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("execute %v: %v", args, err)
	}
	if gotOp != wantOp {
		t.Errorf("op_id: got %q want %q", gotOp, wantOp)
	}
}

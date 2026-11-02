// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// typedDispatchCases pins every vROps verb whose backend read is typed
// to the dotted typed op_id it must dispatch (#2266 repoint sweep
// #2355). The param-bearing `alert list` also asserts its --params
// payload is forwarded verbatim — the keys must satisfy
// vrops.alert.list's closed parameter_schema.
var typedDispatchCases = []struct {
	name       string
	args       []string
	wantOp     string
	checkParam func(*testing.T, map[string]any)
}{
	{name: "about", args: []string{"about"}, wantOp: "vrops.liveness"},
	{
		name:   "alert list",
		args:   []string{"alert", "list", "--params", `{"activeOnly":true}`},
		wantOp: "vrops.alert.list",
		checkParam: func(t *testing.T, p map[string]any) {
			if v, ok := p["activeOnly"].(bool); !ok || !v {
				t.Errorf("alert list: activeOnly param not forwarded to typed op; got %v", p)
			}
		},
	},
}

// ingestedDispatchCases pins the vROps verbs that legitimately
// dispatch ingested METHOD:/path op_ids — each entry records why no
// typed op_id exists, so the intent is pinned rather than silently
// unguarded (#2942). When the backend ships a typed counterpart, move
// the verb to typedDispatchCases.
var ingestedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	// The GET inventory list; the typed vrops.resource.query is the
	// POST body-query surface — a deliberately different, richer read,
	// not a counterpart for this plain list.
	{"resource list", []string{"resource", "list"}, "GET:/suite-api/api/resources"},
	// Detail read with no typed counterpart.
	{"resource get", []string{"resource", "get", "r-1"}, "GET:/suite-api/api/resources/{id}"},
	// No typed alert-definition / symptom / recommendation /
	// supermetric reads.
	{"alertdefinition list", []string{"alertdefinition", "list"}, "GET:/suite-api/api/alertdefinitions"},
	{"symptom list", []string{"symptom", "list"}, "GET:/suite-api/api/symptoms"},
	{"recommendation list", []string{"recommendation", "list"}, "GET:/suite-api/api/recommendations"},
	{"supermetric list", []string{"supermetric", "list"}, "GET:/suite-api/api/supermetrics"},
}

// TestRepointedVerbsDispatchTypedOpIDs pins that the repointed vROps
// verbs dispatch their typed op_ids (not the retired METHOD:/path
// op_ids that no longer resolve on a zero-catalog boot).
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	for _, tc := range typedDispatchCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			assertVerbDispatchesOpID(t, tc.args, tc.wantOp, tc.checkParam)
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
			assertVerbDispatchesOpID(t, tc.args, tc.wantOp, nil)
		})
	}
}

// assertVerbDispatchesOpID executes the verb against a mock backplane
// and asserts the op_id (and optionally the params) it puts on the
// wire.
func assertVerbDispatchesOpID(
	t *testing.T,
	args []string,
	wantOp string,
	checkParam func(*testing.T, map[string]any),
) {
	t.Helper()
	var gotOp string
	var gotParams map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			gotOp = body.OpID
			gotParams = body.Params
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs(append(args, "--target", "rdc-vrops", "--backplane", srv.URL))
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("execute %v: %v", args, err)
	}
	if gotOp != wantOp {
		t.Errorf("op_id: got %q want %q", gotOp, wantOp)
	}
	if checkParam != nil {
		checkParam(t, gotParams)
	}
}

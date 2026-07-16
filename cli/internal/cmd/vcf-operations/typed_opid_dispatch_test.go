// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestRepointedVerbsDispatchTypedOpIDs pins that the #2266-repointed
// vROps verbs dispatch their typed op_ids (not the retired
// METHOD:/path op_ids that no longer resolve on a zero-catalog boot).
// The param-bearing `alert list` also asserts its --params payload is
// forwarded verbatim — the keys must satisfy vrops.alert.list's closed
// parameter_schema.
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	cases := []struct {
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
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
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
			root.SetArgs(append(tc.args, "--target", "rdc-vrops", "--backplane", srv.URL))
			root.SetOut(&bytes.Buffer{})
			root.SetErr(&bytes.Buffer{})
			if err := root.Execute(); err != nil {
				t.Fatalf("execute %v: %v", tc.args, err)
			}
			if gotOp != tc.wantOp {
				t.Errorf("op_id: got %q want %q", gotOp, tc.wantOp)
			}
			if tc.checkParam != nil {
				tc.checkParam(t, gotParams)
			}
		})
	}
}

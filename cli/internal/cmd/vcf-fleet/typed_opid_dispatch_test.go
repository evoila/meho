// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestRepointedVerbsDispatchTypedOpIDs pins that the #2266-repointed
// Fleet verbs dispatch their typed op_ids. Both are no-param reads, so
// the swap is a pure op_id change (only `about` and `environment list`
// were converted; environment info keeps its legacy op_id).
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	cases := []struct {
		name   string
		args   []string
		wantOp string
	}{
		{"about", []string{"about"}, "fleet.about"},
		{"environment list", []string{"environment", "list"}, "fleet.environment.list"},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
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
			root.SetArgs(append(tc.args, "--target", "rdc-fleet", "--backplane", srv.URL))
			root.SetOut(&bytes.Buffer{})
			root.SetErr(&bytes.Buffer{})
			if err := root.Execute(); err != nil {
				t.Fatalf("execute %v: %v", tc.args, err)
			}
			if gotOp != tc.wantOp {
				t.Errorf("op_id: got %q want %q", gotOp, tc.wantOp)
			}
		})
	}
}

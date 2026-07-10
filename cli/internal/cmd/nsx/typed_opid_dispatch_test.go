// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestRepointedVerbsDispatchTypedOpIDs pins that the #2266-repointed
// NSX verbs dispatch their typed op_ids (the legacy METHOD:/path op_ids
// no longer resolve on a zero-catalog boot). All four are GET-list /
// status reads with no params, so the swap is a pure op_id change.
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	cases := []struct {
		name   string
		args   []string
		wantOp string
	}{
		{"about", []string{"about"}, "nsx.node.status"},
		{"cluster status", []string{"cluster", "status"}, "nsx.cluster.status"},
		{"transport-zone list", []string{"transport-zone", "list"}, "nsx.transport_zone.list"},
		{"tier1 list", []string{"tier1", "list"}, "nsx.tier1.list"},
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
			root.SetArgs(append(tc.args, "--target", "rdc-nsx", "--backplane", srv.URL))
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

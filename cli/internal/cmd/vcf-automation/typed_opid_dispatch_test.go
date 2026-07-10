// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestRepointedListVerbsDispatchTypedOpIDs pins that the
// #2266-repointed VCFA list verbs dispatch their typed op_ids at the
// command-constructor site. (The dual-plane `about` verb is covered by
// TestAboutVerbDispatchesPerPlane / TestAboutOpForPlane.) The get-by-id
// verbs keep their legacy op_ids and are deliberately not asserted here.
func TestRepointedListVerbsDispatchTypedOpIDs(t *testing.T) {
	cases := []struct {
		name   string
		args   []string
		wantOp string
	}{
		{"org list", []string{"org", "list"}, "vcfa.provider.org.list"},
		{"region list", []string{"region", "list"}, "vcfa.provider.region.list"},
		{"project list", []string{"project", "list"}, "vcfa.tenant.project.list"},
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
			root.SetArgs(append(tc.args, "--target", "rdc-vcfa", "--backplane", srv.URL))
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

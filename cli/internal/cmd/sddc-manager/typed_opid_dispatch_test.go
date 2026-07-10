// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestRepointedVerbsDispatchTypedOpIDs pins that the #2266-repointed
// SDDC Manager verbs dispatch their typed op_ids. The param-bearing
// verbs (cluster/host/workflow) also assert their filter params carry
// the exact keys the typed parameter_schemas accept (domainId /
// clusterId / status) — a closed schema rejects any other key.
func TestRepointedVerbsDispatchTypedOpIDs(t *testing.T) {
	cases := []struct {
		name       string
		args       []string
		wantOp     string
		checkParam func(*testing.T, map[string]any)
	}{
		{name: "domain list", args: []string{"domain", "list"}, wantOp: "sddc.domain.list"},
		{name: "manager list", args: []string{"manager", "list"}, wantOp: "sddc.manager.list"},
		{
			name:   "cluster list",
			args:   []string{"cluster", "list", "--domain", "domain-mgmt"},
			wantOp: "sddc.cluster.list",
			checkParam: func(t *testing.T, p map[string]any) {
				if p["domainId"] != "domain-mgmt" {
					t.Errorf("cluster list: domainId param not forwarded; got %v", p)
				}
			},
		},
		{
			name:   "host list",
			args:   []string{"host", "list", "--domain", "domain-wld01", "--cluster", "cluster-1"},
			wantOp: "sddc.host.list",
			checkParam: func(t *testing.T, p map[string]any) {
				if p["domainId"] != "domain-wld01" || p["clusterId"] != "cluster-1" {
					t.Errorf("host list: domainId/clusterId params not forwarded; got %v", p)
				}
			},
		},
		{
			name:   "workflow list",
			args:   []string{"workflow", "list", "--status", "In_Progress"},
			wantOp: "sddc.task.list",
			checkParam: func(t *testing.T, p map[string]any) {
				if p["status"] != "In_Progress" {
					t.Errorf("workflow list: status param not forwarded; got %v", p)
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
			root.SetArgs(append(tc.args, "--target", "rdc-sddc-manager", "--backplane", srv.URL))
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

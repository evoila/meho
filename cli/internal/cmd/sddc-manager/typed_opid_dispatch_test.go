// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/evoila/meho/cli/internal/typedops"
)

// typedDispatchCases pins every SDDC Manager verb whose backend read
// is typed to the dotted typed op_id it must dispatch (#2266 repoint
// sweep #2355; network-pool via #2837). The param-bearing verbs
// (cluster/host/workflow) also assert their filter params carry the
// exact keys the typed parameter_schemas accept (domainId / clusterId
// / status) — a closed schema rejects any other key.
var typedDispatchCases = []struct {
	name       string
	args       []string
	wantOp     string
	checkParam func(*testing.T, map[string]any)
}{
	{name: "domain list", args: []string{"domain", "list"}, wantOp: "sddc.domain.list"},
	{name: "manager list", args: []string{"manager", "list"}, wantOp: "sddc.manager.list"},
	{name: "network-pool list", args: []string{"network-pool", "list"}, wantOp: "sddc.network_pool.list"},
	{
		name:   "network-pool get",
		args:   []string{"network-pool", "get", "np-01"},
		wantOp: "sddc.network_pool.get",
		checkParam: func(t *testing.T, p map[string]any) {
			if p["id"] != "np-01" {
				t.Errorf("network-pool get: id param not forwarded; got %v", p)
			}
		},
	},
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

// ingestedDispatchCases pins the SDDC Manager verbs that legitimately
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
	// Release-train read; the typed sddc.system.info reads GET /v1/system
	// (appliance settings), a different surface — no typed counterpart
	// for /v1/releases/system.
	{"about", []string{"about"}, "GET:/v1/releases/system"},
	// No typed bundle read.
	{"bundle list", []string{"bundle", "list"}, "GET:/v1/bundles"},
	// Genuine ingested detail read (#2942 marks it correct): the typed
	// surface ships sddc.domain.list only, no per-id detail op.
	{"domain info", []string{"domain", "info", "d-1"}, "GET:/v1/domains/{id}"},
}

// typedOpCLICoverage classifies every typed op_id the backend registers
// for sddc_manager: the CLI verb that dispatches it, or "" when the op
// is agent-surface-only (no CLI verb wraps it).
// TestBackendTypedOpsAreClassified fails when the backend gains a
// typed op that is not classified here — the "typed op ⇒ repoint the
// CLI verb ⇒ pin it" contract (#2942).
var typedOpCLICoverage = map[string]string{
	"sddc.domain.list":       "domain list",
	"sddc.manager.list":      "manager list",
	"sddc.network_pool.list": "network-pool list",
	"sddc.network_pool.get":  "network-pool get",
	"sddc.cluster.list":      "cluster list",
	"sddc.host.list":         "host list",
	"sddc.task.list":         "workflow list",
	"sddc.credential.list":   "", // agent surface only
	"sddc.license.list":      "", // agent surface only
	"sddc.nsxt_cluster.list": "", // agent surface only
	"sddc.vcenter.list":      "", // agent surface only
	"sddc.vcf_service.list":  "", // agent surface only
	"sddc.domain.status":     "", // agent surface only
	"sddc.system.info":       "", // agent surface only (reads /v1/system, not the about verb's /v1/releases/system)
}

// TestRepointedVerbsDispatchTypedOpIDs pins that the repointed SDDC
// Manager verbs dispatch their typed op_ids.
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

// TestBackendTypedOpsAreClassified reconciles the backend's typed-op
// registry against typedOpCLICoverage so a typed op shipped without a
// CLI-repoint decision fails this test instead of leaving the verb a
// zero-catalog dead end (#2942).
func TestBackendTypedOpsAreClassified(t *testing.T) {
	inventory, err := typedops.BackendOpIDs("sddc_manager")
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
	root.SetArgs(append(args, "--target", "rdc-sddc-manager", "--backplane", srv.URL))
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

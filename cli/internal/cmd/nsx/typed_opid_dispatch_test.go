// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// typedDispatchCases pins every NSX verb whose backend read is typed
// to the dotted typed op_id it must dispatch (#2266 repoint sweep
// #2355; segment list / node list repointed by #2942 once #2835 /
// #2836 shipped the typed reads). All are GET-list / status reads
// with no params, so each swap is a pure op_id change.
var typedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	{"about", []string{"about"}, "nsx.node.status"},
	{"cluster status", []string{"cluster", "status"}, "nsx.cluster.status"},
	{"transport-zone list", []string{"transport-zone", "list"}, "nsx.transport_zone.list"},
	{"tier1 list", []string{"tier1", "list"}, "nsx.tier1.list"},
	{"segment list", []string{"segment", "list"}, "nsx.segment.list"},
	{"node list", []string{"node", "list"}, "nsx.transport_node.list"},
}

// ingestedDispatchCases pins the NSX verbs that legitimately dispatch
// ingested METHOD:/path op_ids — each entry records why no typed
// op_id exists, so the intent is pinned rather than silently
// unguarded (#2942). When the backend ships a typed counterpart, move
// the verb to typedDispatchCases and reclassify it in
// typedOpCLICoverage.
var ingestedDispatchCases = []struct {
	name   string
	args   []string
	wantOp string
}{
	// No typed tier-0 read (initiative #2833 ranks it medium tier).
	{"tier0 list", []string{"tier0", "list"}, "GET:/policy/api/v1/infra/tier-0s"},
	// No typed firewall/security-policy reads.
	{
		"firewall policy list",
		[]string{"firewall", "policy", "list"},
		"GET:/policy/api/v1/infra/domains/{domain-id}/security-policies",
	},
	{
		"firewall rule list",
		[]string{"firewall", "rule", "list", "pol-1"},
		"GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules",
	},
}

// typedOpCLICoverage classifies every typed op_id the backend registers
// for nsx: the CLI verb that dispatches it, or "" when the op is
// agent-surface-only (no CLI verb wraps it).
// TestBackendTypedOpsAreClassified fails when the backend gains a
// typed op that is not classified here — the "typed op ⇒ repoint the
// CLI verb ⇒ pin it" contract (#2942).
var typedOpCLICoverage = map[string]string{
	"nsx.node.status":          "about",
	"nsx.cluster.status":       "cluster status",
	"nsx.transport_zone.list":  "transport-zone list",
	"nsx.tier1.list":           "tier1 list",
	"nsx.segment.list":         "segment list",
	"nsx.transport_node.list":  "node list",
	"nsx.transport_node.state": "", // per-node state detail — agent surface only
	"nsx.alarm.list":           "", // agent surface only
	"nsx.backup.config":        "", // agent surface only
	"nsx.backup.status":        "", // agent surface only
}

// TestRepointedVerbsDispatchTypedOpIDs pins that the repointed NSX
// verbs dispatch their typed op_ids (the legacy METHOD:/path op_ids
// no longer resolve on a zero-catalog boot).
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
	root.SetArgs(append(args, "--target", "rdc-nsx", "--backplane", srv.URL))
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("execute %v: %v", args, err)
	}
	if gotOp != wantOp {
		t.Errorf("op_id: got %q want %q", gotOp, wantOp)
	}
}

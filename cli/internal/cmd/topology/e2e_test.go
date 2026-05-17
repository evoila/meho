// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestRefreshHappyPath drives runRefresh end-to-end through the auth
// + transport stack against an httptest server, asserting the POST
// verb + path and the rendered count summary.
func TestRefreshHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/refresh/rdc-vcenter", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RefreshResult{
			TargetID: "abc", AddedNodes: 3, RemovedNodes: 1, UpdatedNodes: 2,
			AddedEdges: 4, RemovedEdges: 0, UpdatedEdges: 1,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runRefresh(cmd, refreshOptions{Target: "rdc-vcenter", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRefresh: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"rdc-vcenter", "+3", "-1", "~2", "+4"} {
		if !strings.Contains(out, want) {
			t.Errorf("refresh summary missing %q in %q", want, out)
		}
	}
}

// TestRefreshJSON — --json round-trips the raw RefreshResult shape.
func TestRefreshJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/refresh/t", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RefreshResult{TargetID: "z", AddedNodes: 1, DurationMs: 42.5})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runRefresh(cmd, refreshOptions{Target: "t", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRefresh --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded RefreshResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.TargetID != "z" || decoded.AddedNodes != 1 {
		t.Errorf("--json decode produced %+v", decoded)
	}
	// duration_ms is part of the T5 RefreshResult contract; --json
	// must round-trip it, not silently drop it.
	if decoded.DurationMs != 42.5 {
		t.Errorf("--json dropped duration_ms: got %v, want 42.5", decoded.DurationMs)
	}
}

// TestRefreshCrossTenant404 — a target in another tenant resolves to
// the resolver's no_target 404; the CLI surfaces it as
// unexpected_response (exit 4) with the near-miss hint. This is the
// tenant-boundary acceptance criterion for refresh.
func TestRefreshCrossTenant404(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/refresh/tenant-b-target", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":{"error":"no_target","query":"tenant-b-target","matches":[]}}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runRefresh(cmd, refreshOptions{Target: "tenant-b-target", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error for cross-tenant target")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "not found") {
		t.Errorf("expected not-found detail; got %q", stderr.String())
	}
}

// TestDependentsHappyPathFlagsPassThrough — the closure verb sends
// --depth / --kind on the wire and renders the depth-ordered table.
func TestDependentsHappyPathFlagsPassThrough(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependents/customer-a-prod-foo", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("depth"); got != "5" {
			t.Errorf("depth param: got %q; want 5", got)
		}
		if got := r.URL.Query().Get("kind_filter"); got != "routes-through" {
			t.Errorf("kind_filter param: got %q; want routes-through", got)
		}
		via := "routes-through"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]TopologyNode{
			{ID: "1", Kind: "service", Name: "customer-a-prod-foo", Depth: 0},
			{ID: "2", Kind: "ingress", Name: "ing-1", Depth: 1, ViaEdgeKind: &via},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{
		Verb: "dependents", Name: "customer-a-prod-foo",
		Depth: 5, EdgeKind: "routes-through", BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runClosure: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"DEPTH", "customer-a-prod-foo", "ing-1", "routes-through"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("dependents table missing %q in %q", want, stdout.String())
		}
	}
}

// TestDependenciesJSON — --json round-trips the []TopologyNode shape.
func TestDependenciesJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependencies/web", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]TopologyNode{{ID: "1", Kind: "vm", Name: "web", Depth: 0}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{
		Verb: "dependencies", Name: "web", JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runClosure --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded []TopologyNode
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded) != 1 || decoded[0].Name != "web" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestDependentsAmbiguousNode409 — a bare ambiguous name returns the
// query layer's 409; the CLI renders the colliding kinds + remedy.
func TestDependentsAmbiguousNode409(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependents/prod", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"detail":{"error":"ambiguous_node","name":"prod","kinds":["host","vm"]}}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{Verb: "dependents", Name: "prod", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error for ambiguous node")
	}
	for _, want := range []string{"unexpected_response", "ambiguous", "host", "vm"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("ambiguous render missing %q in %q", want, stderr.String())
		}
	}
}

// TestDependentsCrossTenantEmpty — a node name that exists only in
// another tenant returns an empty list (200), rendered as the
// not-found line, never the other tenant's node. Tenant-boundary
// acceptance criterion for the read verbs.
func TestDependentsCrossTenantEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependents/tenant-b-node", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[]`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{Verb: "dependents", Name: "tenant-b-node", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runClosure: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no node named") {
		t.Errorf("cross-tenant query should render not-found; got %q", stdout.String())
	}
}

// TestDependentsRejectsOutOfRangeDepth — a --depth past the API
// ceiling fails fast client-side (no 422 round-trip).
func TestDependentsRejectsOutOfRangeDepth(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{Verb: "dependents", Name: "x", Depth: 65})
	if err == nil {
		t.Fatalf("expected error for over-budget --depth")
	}
	if !strings.Contains(stderr.String(), "between 1 and 64") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestPathReachable — a reachable pair renders the hop chain.
func TestPathReachable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/path", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("from"); got != "web" {
			t.Errorf("from param: got %q", got)
		}
		if got := r.URL.Query().Get("to"); got != "ds" {
			t.Errorf("to param: got %q", got)
		}
		if got := r.URL.Query().Get("max_hops"); got != "5" {
			t.Errorf("max_hops param: got %q; want 5", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(TopologyPath{
			Nodes: []TopologyNode{
				{Kind: "vm", Name: "web"},
				{Kind: "datastore", Name: "ds"},
			},
			TotalHops: 1,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runPath(cmd, pathOptions{From: "web", To: "ds", MaxHops: 5, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runPath: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "vm/web -> datastore/ds") {
		t.Errorf("path chain missing; got %q", stdout.String())
	}
}

// TestPathUnreachableNull — the route returns literal JSON null when
// unreachable; the CLI renders the no-path line (exit 0, not an
// error) and --json emits `null` verbatim.
func TestPathUnreachableNull(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/path", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`null`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runPath(cmd, pathOptions{From: "a", To: "b", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runPath null: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no path from") {
		t.Errorf("unreachable should render no-path; got %q", stdout.String())
	}

	cmd2, stdout2, _ := newRunCmd(t)
	if err := runPath(cmd2, pathOptions{From: "a", To: "b", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runPath --json null: %v", err)
	}
	if strings.TrimSpace(stdout2.String()) != "null" {
		t.Errorf("--json unreachable should emit null; got %q", stdout2.String())
	}
}

// TestPathRejectsOutOfRangeMaxHops — over-budget --max-hops fails
// fast client-side.
func TestPathRejectsOutOfRangeMaxHops(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runPath(cmd, pathOptions{From: "a", To: "b", MaxHops: 33})
	if err == nil {
		t.Fatalf("expected error for over-budget --max-hops")
	}
	if !strings.Contains(stderr.String(), "between 1 and 32") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestClosure403InsufficientRole — RBAC denial renders the backend's
// required-role string with exit class insufficient_role.
func TestClosure403InsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependents/x", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runClosure(cmd, closureOptions{Verb: "dependents", Name: "x", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "operator required") {
		t.Errorf("expected backend role hint passed through; got %q", stderr.String())
	}
}

// TestRefresh401AuthExpired — exhausting the refresh budget renders
// auth_expired with the `meho login` hint.
func TestRefresh401AuthExpired(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/refresh/x", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"detail":"token expired"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL) // no refresh_token present

	cmd, _, stderr := newRunCmd(t)
	err := runRefresh(cmd, refreshOptions{Target: "x", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected meho login hint; got %q", stderr.String())
	}
}

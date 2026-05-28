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

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/api"
)

// fakeTargetUUID is a stable canonical UUID used as the typed
// `target_id` field on the substrate's RefreshResult contract. The
// pre-migration shape carried this as a free-form string ("abc", "z");
// the typed `api.RefreshResult.TargetId` is `openapi_types.UUID` so
// the test fixture must round-trip a real UUID through the wire.
const fakeTargetUUID = "11111111-2222-3333-4444-555555555555"

func mustUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	parsed := openapi_types.UUID{}
	if err := parsed.UnmarshalText([]byte(s)); err != nil {
		t.Fatalf("UnmarshalText(%q): %v", s, err)
	}
	return parsed
}

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
		_ = json.NewEncoder(w).Encode(api.RefreshResult{
			TargetId:   mustUUID(t, fakeTargetUUID),
			AddedNodes: 3, RemovedNodes: 1, UpdatedNodes: 2,
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
	const altUUID = "22222222-3333-4444-5555-666666666666"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/refresh/t", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RefreshResult{
			TargetId: mustUUID(t, altUUID), AddedNodes: 1, DurationMs: 42.5,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runRefresh(cmd, refreshOptions{Target: "t", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRefresh --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded api.RefreshResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.TargetId.String() != altUUID || decoded.AddedNodes != 1 {
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
		_ = json.NewEncoder(w).Encode([]api.TopologyNode{
			{Id: mustUUID(t, "10000000-0000-0000-0000-000000000001"), Kind: "service", Name: "customer-a-prod-foo", Depth: 0},
			{Id: mustUUID(t, "10000000-0000-0000-0000-000000000002"), Kind: "ingress", Name: "ing-1", Depth: 1, ViaEdgeKind: &via},
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

// TestDependenciesJSON — --json round-trips the []api.TopologyNode shape.
func TestDependenciesJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/dependencies/web", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]api.TopologyNode{{
			Id:   mustUUID(t, "10000000-0000-0000-0000-000000000003"),
			Kind: "vm", Name: "web", Depth: 0,
		}})
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
	var decoded []api.TopologyNode
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
		_ = json.NewEncoder(w).Encode(api.TopologyPath{
			Nodes: []api.TopologyNode{
				{Id: mustUUID(t, "10000000-0000-0000-0000-000000000004"), Kind: "vm", Name: "web"},
				{Id: mustUUID(t, "10000000-0000-0000-0000-000000000005"), Kind: "datastore", Name: "ds"},
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

// TestAnnotateRoundTripVisibleViaListEdgesThenUnannotate — the
// acceptance criterion for #599: annotate → list-edges shows it →
// unannotate removes it, against an httptest backplane that mirrors
// the T5 wire shape (POST /edges → 201 TopologyEdge, GET /edges →
// list of TopologyEdge, DELETE /edges/{id} → 204).
func TestAnnotateRoundTripVisibleViaListEdgesThenUnannotate(t *testing.T) {
	const edgeID = "11111111-2222-3333-4444-555555555555"
	annotated := false
	deleted := false
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			var body api.UnderscoreAnnotateEdgeRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode POST body: %v", err)
			}
			if body.From.Name != "service-x" || string(body.Kind) != "depends-on" || body.To.Name != "database-y" {
				t.Errorf("POST body mismatch: %+v", body)
			}
			if body.EvidenceUrl == nil || *body.EvidenceUrl != "https://docs/example" {
				t.Errorf("evidence_url not propagated: %v", body.EvidenceUrl)
			}
			annotated = true
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(api.TopologyEdge{
				Id:   mustUUID(t, edgeID),
				From: api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000001"), Kind: "service", Name: "service-x"},
				To:   api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000002"), Kind: "database", Name: "database-y"},
				Kind: "depends-on", Source: "curated",
			})
		case http.MethodGet:
			// Honour the source filter — annotate test issues
			// source=curated when resolving the tuple form.
			if got := r.URL.Query().Get("source"); got != "" && got != "curated" {
				t.Errorf("unexpected source filter %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			if !annotated || deleted {
				_, _ = w.Write([]byte(`[]`))
				return
			}
			_ = json.NewEncoder(w).Encode([]api.TopologyEdge{{
				Id:   mustUUID(t, edgeID),
				From: api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000001"), Kind: "service", Name: "service-x"},
				To:   api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000002"), Kind: "database", Name: "database-y"},
				Kind: "depends-on", Source: "curated",
			}})
		default:
			t.Errorf("unexpected method on /edges: %s", r.Method)
		}
	})
	mux.HandleFunc("/api/v1/topology/edges/"+edgeID, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("expected DELETE; got %s", r.Method)
		}
		deleted = true
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	// 1. annotate
	cmd, stdout, stderr := newRunCmd(t)
	if err := runAnnotate(cmd, annotateOptions{
		From: "service-x", Kind: "depends-on", To: "database-y",
		EvidenceURL:       "https://docs/example",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAnnotate: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "annotated edge") {
		t.Errorf("annotate summary missing; got %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), edgeID) {
		t.Errorf("annotate summary missing edge_id; got %q", stdout.String())
	}

	// 2. list-edges — sees the new edge
	cmd2, stdout2, stderr2 := newRunCmd(t)
	if err := runListEdges(cmd2, listEdgesOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runListEdges: %v; stderr=%s", err, stderr2.String())
	}
	for _, want := range []string{"KIND", "depends-on", "service-x", "database-y"} {
		if !strings.Contains(stdout2.String(), want) {
			t.Errorf("list-edges missing %q in %q", want, stdout2.String())
		}
	}

	// 3. unannotate (tuple form) — resolves client-side then DELETEs by id
	cmd3, stdout3, stderr3 := newRunCmd(t)
	if err := runUnannotate(cmd3, unannotateOptions{
		From: "service-x", Kind: "depends-on", To: "database-y",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runUnannotate tuple: %v; stderr=%s", err, stderr3.String())
	}
	if !strings.Contains(stdout3.String(), "deleted edge "+edgeID) {
		t.Errorf("unannotate output wrong; got %q", stdout3.String())
	}
	if !deleted {
		t.Errorf("DELETE never reached the server")
	}

	// 4. list-edges — now empty
	cmd4, stdout4, _ := newRunCmd(t)
	if err := runListEdges(cmd4, listEdgesOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("final list: %v", err)
	}
	if !strings.Contains(stdout4.String(), "no edges matched") {
		t.Errorf("expected empty listing after delete; got %q", stdout4.String())
	}
}

// TestAnnotateJSONPassesThroughRawEdge — --json emits the raw
// TopologyEdge envelope unchanged so a consumer (jq, MCP shim, etc.)
// can pipe the response into a follow-up call.
func TestAnnotateJSONPassesThroughRawEdge(t *testing.T) {
	const edgeID = "33333333-4444-5555-6666-777777777777"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(api.TopologyEdge{
			Id:   mustUUID(t, edgeID),
			From: api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000003"), Kind: "vm", Name: "a"},
			To:   api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000004"), Kind: "vm", Name: "b"},
			Kind: "depends-on", Source: "curated",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runAnnotate(cmd, annotateOptions{
		From: "a", Kind: "depends-on", To: "b",
		JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAnnotate --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded api.TopologyEdge
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v\n%s", err, stdout.String())
	}
	if decoded.Id.String() != edgeID || decoded.Kind != "depends-on" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestAnnotate403TenantAdminRequired — a 403 from the route renders
// the backend's role hint with exit class insufficient_role so the
// operator sees "annotation requires tenant_admin", not a raw HTTP dump.
func TestAnnotate403TenantAdminRequired(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runAnnotate(cmd, annotateOptions{
		From: "a", Kind: "depends-on", To: "b", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for 403")
	}
	for _, want := range []string{"insufficient_role", "tenant_admin"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("expected %q; got %q", want, stderr.String())
		}
	}
}

// TestUnannotateIDFormHappyPath — `unannotate <edge-id>` skips the
// client-side resolve and DELETEs directly.
func TestUnannotateIDFormHappyPath(t *testing.T) {
	const edgeID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
	deleted := false
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges/"+edgeID, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("expected DELETE; got %s", r.Method)
		}
		deleted = true
		w.WriteHeader(http.StatusNoContent)
	})
	// A GET handler exists so the test fails loud if the id-form
	// accidentally resolves via the list helper.
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		t.Errorf("id form should not list-edges")
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runUnannotate(cmd, unannotateOptions{
		EdgeID: edgeID, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runUnannotate id: %v; stderr=%s", err, stderr.String())
	}
	if !deleted {
		t.Fatalf("DELETE never reached the server")
	}
	if !strings.Contains(stdout.String(), "deleted edge "+edgeID) {
		t.Errorf("output wrong; got %q", stdout.String())
	}
}

// TestUnannotateRejectsNonUUIDIDForm — single-arg form requires a
// valid UUID; a misshapen id fails fast client-side (no DELETE round-
// trip).
func TestUnannotateRejectsNonUUIDIDForm(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("network call not expected; got %s %s", r.Method, r.URL)
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUnannotate(cmd, unannotateOptions{
		EdgeID: "not-a-uuid", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for non-UUID edge-id")
	}
	if !strings.Contains(stderr.String(), "not a UUID") {
		t.Errorf("expected UUID-shape hint; got %q", stderr.String())
	}
}

// TestUnannotateAutoEdge409RendersServerDetail — DELETE on an auto-row
// returns the route's typed 409 envelope; the CLI surfaces the
// server's `detail.message` verbatim (the annotate-over-auto guidance)
// instead of dumping the raw HTTP body.
func TestUnannotateAutoEdge409RendersServerDetail(t *testing.T) {
	const edgeID = "11111111-1111-1111-1111-111111111111"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges/"+edgeID, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"detail":{"error":"auto_edge_deletion","edge_id":"`+edgeID+`",`+
			`"message":"graph_edge has source='auto'; auto edges resurrect on the next refresh, `+
			`so manual deletion is a no-op. Annotate over the auto edge first, then unannotate the curated row."}}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUnannotate(cmd, unannotateOptions{
		EdgeID: edgeID, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for 409")
	}
	for _, want := range []string{"auto edges resurrect", "Annotate over"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("expected server detail %q in stderr; got %q", want, stderr.String())
		}
	}
	if strings.Contains(stderr.String(), "HTTP 409") {
		t.Errorf("expected the auto-row message, not a raw HTTP 409 dump; got %q", stderr.String())
	}
}

// TestUnannotateTupleAmbiguous — two curated edges match the same
// tuple → the CLI surfaces an ambiguous-tuple error with the candidate
// ids so the operator can re-run with `unannotate <edge-id>`.
func TestUnannotateTupleAmbiguous(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]api.TopologyEdge{
			{Id: mustUUID(t, "40000000-0000-0000-0000-000000000001"), From: api.TopologyEdgeEndpoint{Kind: "vm", Name: "a"}, To: api.TopologyEdgeEndpoint{Kind: "host", Name: "b"}, Kind: "runs-on"},
			{Id: mustUUID(t, "40000000-0000-0000-0000-000000000002"), From: api.TopologyEdgeEndpoint{Kind: "vm", Name: "a"}, To: api.TopologyEdgeEndpoint{Kind: "host", Name: "b"}, Kind: "runs-on"},
		})
	})
	mux.HandleFunc("/api/v1/topology/edges/", func(_ http.ResponseWriter, _ *http.Request) {
		t.Errorf("DELETE must not fire on ambiguous tuple")
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUnannotate(cmd, unannotateOptions{
		From: "a", Kind: "runs-on", To: "b", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for ambiguous tuple")
	}
	for _, want := range []string{"matches 2 curated edges", "40000000-0000-0000-0000-000000000001", "40000000-0000-0000-0000-000000000002"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("ambiguous render missing %q in %q", want, stderr.String())
		}
	}
}

// TestUnannotateTupleNotFound — empty list → the CLI surfaces a
// not-found line that names the queried triple (and never another
// tenant's row, since the list helper is server-side tenant-scoped).
func TestUnannotateTupleNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[]`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUnannotate(cmd, unannotateOptions{
		From: "ghost", Kind: "depends-on", To: "phantom", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for not-found tuple")
	}
	if !strings.Contains(stderr.String(), "no curated edge matches") {
		t.Errorf("expected not-found hint; got %q", stderr.String())
	}
}

// TestListEdgesJSONRoundTrip — --json emits the raw []TopologyEdge envelope so
// a consumer can pipe the response into the unannotate id form.
func TestListEdgesJSONRoundTrip(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]api.TopologyEdge{{
			Id:   mustUUID(t, "50000000-0000-0000-0000-000000000001"),
			From: api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000005"), Kind: "vm", Name: "web"},
			To:   api.TopologyEdgeEndpoint{Id: mustUUID(t, "20000000-0000-0000-0000-000000000006"), Kind: "host", Name: "esxi-1"},
			Kind: "runs-on", Source: "auto",
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runListEdges(cmd, listEdgesOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListEdges --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded []api.TopologyEdge
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded) != 1 || decoded[0].Id.String() != "50000000-0000-0000-0000-000000000001" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestListEdgesFlagsMapToQueryString — --kind / --source / --from /
// --to / --conflicts / --limit / --offset land on the wire under the
// names the route documents (no kebab→snake mismatch).
func TestListEdgesFlagsMapToQueryString(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/topology/edges", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		want := map[string]string{
			"kind":      "depends-on",
			"source":    "curated",
			"from":      "svc-x",
			"to":        "db-y",
			"conflicts": "true",
			"limit":     "50",
			"offset":    "10",
		}
		for k, v := range want {
			if got := q.Get(k); got != v {
				t.Errorf("query %s: got %q; want %q", k, got, v)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[]`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runListEdges(cmd, listEdgesOptions{
		Kind: "depends-on", Source: "curated",
		From: "svc-x", To: "db-y",
		Conflicts: true, Limit: 50, Offset: 10,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runListEdges: %v; stderr=%s", err, stderr.String())
	}
}

// TestListEdgesRejectsInvalidSource — --source must be curated or
// auto; anything else fails fast client-side.
func TestListEdgesRejectsInvalidSource(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runListEdges(cmd, listEdgesOptions{Source: "manual"})
	if err == nil {
		t.Fatalf("expected error for invalid --source")
	}
	if !strings.Contains(stderr.String(), "curated") || !strings.Contains(stderr.String(), "auto") {
		t.Errorf("expected source hint; got %q", stderr.String())
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

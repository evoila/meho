// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildDiscoverPathRequiresProduct — product always rides the
// query string; seed_target is omitted when unset.
func TestBuildDiscoverPathRequiresProduct(t *testing.T) {
	got := buildDiscoverPath(discoverOptions{Product: "vmware"})
	if got != "/api/v1/targets/discover?product=vmware" {
		t.Fatalf("default path: got %q", got)
	}
}

// TestBuildDiscoverPathSetsSeed — --seed-target lands as seed_target.
func TestBuildDiscoverPathSetsSeed(t *testing.T) {
	got := buildDiscoverPath(discoverOptions{Product: "k8s", SeedTarget: "rke2-meho"})
	for _, want := range []string{"product=k8s", "seed_target=rke2-meho"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildDiscoverPath missing %q in %q", want, got)
		}
	}
}

// TestPrintDiscoverTablesEmpty — zero candidates render the
// no-candidates line (operationally meaningful) without a header.
func TestPrintDiscoverTablesEmpty(t *testing.T) {
	var buf bytes.Buffer
	printDiscoverTables(&buf, &DiscoverResult{})
	out := buf.String()
	if !strings.Contains(out, "no candidate targets discovered") {
		t.Errorf("empty render missing no-candidates hint; got %q", out)
	}
	if strings.Contains(out, "CONFIDENCE") {
		t.Errorf("empty render should skip header; got %q", out)
	}
}

// TestPrintDiscoverTablesRendersBoth — candidates table + skipped
// table both render.
func TestPrintDiscoverTablesRendersBoth(t *testing.T) {
	port := 443
	r := &DiscoverResult{
		Discovered: []CandidateHint{
			{Name: "esxi-2", Host: "esxi-2.lab", Port: &port, Confidence: "high"},
		},
		Skipped: []SkippedConnector{
			{Name: "vmware-pyvmomi-7.0", Reason: "no candidates"},
		},
	}
	var buf bytes.Buffer
	printDiscoverTables(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"NAME", "HOST", "PORT", "CONFIDENCE", "esxi-2", "443", "high",
		"SKIPPED", "REASON", "vmware-pyvmomi-7.0", "no candidates",
		"meho targets create",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDiscoverTables missing %q in %q", want, out)
		}
	}
}

// TestRunDiscoverHappyPath — `targets discover --product vmware`
// lists candidate targets from the registered vmware connectors
// (acceptance criterion 4).
func TestRunDiscoverHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/discover", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("product"); got != "vmware" {
			t.Errorf("product param: got %q; want vmware", got)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		port := 443
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(DiscoverResult{
			Discovered: []CandidateHint{
				{Name: "esxi-2", Host: "esxi-2.lab", Port: &port, Confidence: "high"},
			},
			Skipped: []SkippedConnector{},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDiscover(cmd, discoverOptions{Product: "vmware", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runDiscover: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"NAME", "esxi-2", "esxi-2.lab", "443", "high"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("discover table missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunDiscoverJSON — --json round-trips the aggregate shape.
func TestRunDiscoverJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/discover", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(DiscoverResult{
			Discovered: []CandidateHint{{Name: "c1", Host: "h1", Confidence: "low"}},
			Skipped:    []SkippedConnector{},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDiscover(cmd, discoverOptions{Product: "k8s", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runDiscover --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded DiscoverResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded.Discovered) != 1 || decoded.Discovered[0].Name != "c1" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestRunDiscoverCrossTenantSeed404 — a --seed-target in another
// tenant resolves to the resolver's no_target 404; the CLI surfaces
// it as unexpected_response with near-misses (tenant boundary).
func TestRunDiscoverCrossTenantSeed404(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/discover", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":{"error":"no_target","query":"tenant-b-seed","matches":[]}}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDiscover(cmd, discoverOptions{
		Product: "vmware", SeedTarget: "tenant-b-seed", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for cross-tenant seed")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response; got %q", stderr.String())
	}
}

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

	"github.com/evoila/meho/cli/internal/api"
)

// TestBuildDiscoverParamsRequiresProduct — product always lands in
// the typed params struct; seed_target is omitted (nil) when unset.
func TestBuildDiscoverParamsRequiresProduct(t *testing.T) {
	p := buildDiscoverParams(discoverOptions{Product: "vmware"})
	if p.Product != "vmware" {
		t.Fatalf("Product: got %q; want %q", p.Product, "vmware")
	}
	if p.SeedTarget != nil {
		t.Errorf("unset SeedTarget should marshal as nil pointer; got %q", *p.SeedTarget)
	}
}

// TestBuildDiscoverParamsSetsSeed — --seed-target lands as *SeedTarget.
func TestBuildDiscoverParamsSetsSeed(t *testing.T) {
	p := buildDiscoverParams(discoverOptions{Product: "k8s", SeedTarget: "rke2-meho"})
	if p.Product != "k8s" {
		t.Errorf("Product: got %q", p.Product)
	}
	if p.SeedTarget == nil || *p.SeedTarget != "rke2-meho" {
		t.Errorf("SeedTarget: got %+v; want pointer to %q", p.SeedTarget, "rke2-meho")
	}
}

// TestPrintDiscoverTablesEmpty — zero candidates render the
// no-candidates line (operationally meaningful) without a header.
func TestPrintDiscoverTablesEmpty(t *testing.T) {
	var buf bytes.Buffer
	printDiscoverTables(&buf, &api.TargetsDiscoverResult{})
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
	r := &api.TargetsDiscoverResult{
		Discovered: []api.CandidateHint{
			{Name: "esxi-2", Host: "esxi-2.lab", Port: &port, Confidence: api.CandidateHintConfidence("high")},
		},
		Skipped: []api.SkippedConnector{
			{Name: "vmware-pyvmomi-7.0", Reason: "no candidates"},
		},
	}
	var buf bytes.Buffer
	printDiscoverTables(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"NAME", "HOST", "PORT", "CONFIDENCE", "esxi-2", "443", "high",
		"SKIPPED", "REASON", "vmware-pyvmomi-7.0", "no candidates",
		"meho targets import",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDiscoverTables missing %q in %q", want, out)
		}
	}
}

// TestRunDiscoverHappyPath — `targets discover --product vmware`
// lists candidate targets from the registered vmware connectors
// (acceptance criterion 4). Asserts the typed `product` query param
// round-trips on the wire.
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
		_ = json.NewEncoder(w).Encode(api.TargetsDiscoverResult{
			Discovered: []api.CandidateHint{
				{Name: "esxi-2", Host: "esxi-2.lab", Port: &port, Confidence: api.CandidateHintConfidence("high")},
			},
			Skipped: []api.SkippedConnector{},
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
		_ = json.NewEncoder(w).Encode(api.TargetsDiscoverResult{
			Discovered: []api.CandidateHint{
				{Name: "c1", Host: "h1", Confidence: api.CandidateHintConfidence("low")},
			},
			Skipped: []api.SkippedConnector{},
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
	var decoded api.TargetsDiscoverResult
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

// azureCandidateResult is one candidate as the Azure connector returns it,
// plus one skipped connector. Every value is made up.
func azureCandidateResult() api.TargetsDiscoverResult {
	port := 443
	secretRef := "tenants/example/azure-login-a"
	extras := map[string]interface{}{
		"subscription_id": "11111111-0000-0000-0000-00000000e003",
		"tenant_id":       "00000000-0000-0000-0000-00000000a001",
		"cloud":           "AzureCloud",
	}
	return api.TargetsDiscoverResult{
		Discovered: []api.CandidateHint{{
			Name:       "azure-example-11111111",
			Host:       "management.azure.com",
			Port:       &port,
			SecretRef:  &secretRef,
			Extras:     &extras,
			Confidence: api.CandidateHintConfidence("high"),
		}},
		Skipped: []api.SkippedConnector{{Name: "other-impl", Reason: "no candidates"}},
	}
}

// TestPrintImportYAMLIsAnImportFile — the --import-yaml output is a file
// `meho targets import` reads: it parses, and each candidate maps to a
// create body with the product, host, port, secret_ref and extras.
func TestPrintImportYAMLIsAnImportFile(t *testing.T) {
	result := azureCandidateResult()
	var out, errOut bytes.Buffer
	if err := printImportYAML(&out, &errOut, "azure", &result); err != nil {
		t.Fatalf("printImportYAML: %v", err)
	}
	entries, err := parseTargetsYAML(out.Bytes())
	if err != nil {
		t.Fatalf("output is not an import file: %v\n%s", err, out.String())
	}
	if len(entries) != 1 {
		t.Fatalf("want 1 entry, got %d", len(entries))
	}
	body, warnings := entryToCreateBody(entries[0])
	if len(warnings) != 0 {
		t.Errorf("unexpected warnings: %v", warnings)
	}
	for key, want := range map[string]interface{}{
		"name":       "azure-example-11111111",
		"product":    "azure",
		"host":       "management.azure.com",
		"port":       443,
		"secret_ref": "tenants/example/azure-login-a",
	} {
		if body[key] != want {
			t.Errorf("body[%q] = %v; want %v", key, body[key], want)
		}
	}
	extras, ok := body["extras"].(map[string]any)
	if !ok || extras["subscription_id"] != "11111111-0000-0000-0000-00000000e003" ||
		extras["cloud"] != "AzureCloud" {
		t.Errorf("extras not carried over: %#v", body["extras"])
	}
	if !strings.Contains(errOut.String(), "skipped other-impl: no candidates") {
		t.Errorf("skipped connector should go to stderr; got %q", errOut.String())
	}
}

// TestRunDiscoverImportYAML — --import-yaml through the verb: stdout is the
// import file, stderr carries the skipped connectors.
func TestRunDiscoverImportYAML(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/discover", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("seed_target"); got != "azure-sub-a" {
			t.Errorf("seed_target param: got %q; want azure-sub-a", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(azureCandidateResult())
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDiscover(cmd, discoverOptions{
		Product: "azure", SeedTarget: "azure-sub-a", ImportYAML: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDiscover --import-yaml: %v; stderr=%s", err, stderr.String())
	}
	entries, err := parseTargetsYAML(stdout.Bytes())
	if err != nil || len(entries) != 1 || entries[0]["name"] != "azure-example-11111111" {
		t.Fatalf("stdout is not the import file (%v): %s", err, stdout.String())
	}
	if strings.Contains(stdout.String(), "skipped") {
		t.Errorf("stdout should hold only the file; got %q", stdout.String())
	}
	if !strings.Contains(stderr.String(), "skipped other-impl") {
		t.Errorf("stderr should name the skipped connector; got %q", stderr.String())
	}
}

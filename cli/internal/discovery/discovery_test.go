// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package discovery

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// TestFetch_HappyPath drives discovery against an httptest server
// serving a non-empty manifest. Confirms the typed shape decodes
// and the response Commands slice surfaces verbatim.
func TestFetch_HappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(Endpoint, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
            "commands": [
                {"name": "k8s", "short": "Kubernetes operations",
                 "subcommands": [
                    {"name": "deployment", "short": "Manage deployments",
                     "maturity": "beta"}
                 ]}
            ]
        }`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	manifest, err := Fetch(context.Background(), srv.Client(), srv.URL)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(manifest.Commands) != 1 {
		t.Fatalf("expected 1 command, got %d", len(manifest.Commands))
	}
	k8s := manifest.Commands[0]
	if k8s.Name != "k8s" || k8s.Short != "Kubernetes operations" {
		t.Errorf("unexpected k8s command shape: %+v", k8s)
	}
	if len(k8s.Subcommands) != 1 || k8s.Subcommands[0].Name != "deployment" {
		t.Errorf("expected nested deployment subcommand: %+v", k8s.Subcommands)
	}
	if k8s.Maturity != "" {
		t.Errorf("k8s carries no maturity key, expected empty, got %q", k8s.Maturity)
	}
	if k8s.Subcommands[0].Maturity != "beta" {
		t.Errorf("deployment maturity: got %q, want %q", k8s.Subcommands[0].Maturity, "beta")
	}
}

// TestFetch_404DegradesGracefully confirms a backplane that hasn't
// yet shipped the /api/v1/commands endpoint (v0.1 default) yields
// an empty manifest, not an error. Operators on v0.1 backplanes
// running v0.2 CLIs degrade silently.
func TestFetch_404DegradesGracefully(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.NotFound(w, nil)
	}))
	defer srv.Close()
	manifest, err := Fetch(context.Background(), srv.Client(), srv.URL)
	if err != nil {
		t.Fatalf("Fetch returned error on 404, expected graceful empty: %v", err)
	}
	if len(manifest.Commands) != 0 {
		t.Errorf("expected empty manifest, got %d commands", len(manifest.Commands))
	}
}

// TestFetch_TransportFailureDegradesGracefully confirms an
// unreachable backplane (TCP refused) yields an empty manifest,
// not an error. The discovery scaffold must never block the local
// command set.
func TestFetch_TransportFailureDegradesGracefully(t *testing.T) {
	// Use an unrouteable URL — RFC 5737 TEST-NET-1 with a closed
	// port. http.DefaultClient times out quickly under the
	// fetchTimeout cap.
	manifest, err := Fetch(context.Background(), http.DefaultClient, "http://192.0.2.1:1/")
	if err != nil {
		t.Fatalf("transport failure should degrade gracefully: %v", err)
	}
	if len(manifest.Commands) != 0 {
		t.Errorf("expected empty manifest, got %d", len(manifest.Commands))
	}
}

// TestFetch_DecodeFailureSurfaces confirms a 2xx response with an
// undecodable body returns an error. A backplane contract break
// (operator's CLI is mismatched against the API version) deserves
// to be visible.
func TestFetch_DecodeFailureSurfaces(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json at all"))
	}))
	defer srv.Close()
	if _, err := Fetch(context.Background(), srv.Client(), srv.URL); err == nil {
		t.Error("expected decode failure to surface")
	}
}

// TestRegister_GraftsCommands confirms the mock test scenario from
// the issue body's acceptance criterion: a manifest populating a
// fake `k8s` command yields `meho k8s` and `meho k8s --help` works
// post-registration.
func TestRegister_GraftsCommands(t *testing.T) {
	root := &cobra.Command{Use: "meho"}
	manifest := &CommandManifest{Commands: []Command{
		{
			Name: "k8s", Short: "Kubernetes operations",
			Subcommands: []Command{
				{Name: "list", Short: "List managed clusters"},
			},
		},
	}}
	if err := Register(root, manifest); err != nil {
		t.Fatalf("Register: %v", err)
	}
	// Find the grafted command.
	var k8s *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "k8s" {
			k8s = c
		}
	}
	if k8s == nil {
		t.Fatalf("k8s subcommand not registered; got: %v", root.Commands())
	}
	if k8s.Short != "Kubernetes operations" {
		t.Errorf("k8s.Short: got %q", k8s.Short)
	}
	if len(k8s.Commands()) != 1 || k8s.Commands()[0].Name() != "list" {
		t.Errorf("k8s.list subcommand not registered: %v", k8s.Commands())
	}

	// `meho k8s --help` exercises cobra's help rendering — the
	// grafted command must have a usable help surface even though
	// it has subcommands and no RunE.
	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetArgs([]string{"k8s", "--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("k8s --help failed: %v", err)
	}
	if !strings.Contains(stdout.String(), "Kubernetes operations") {
		t.Errorf("--help output missing short description:\n%s", stdout.String())
	}
}

// TestRegister_MaturitySuffixInHelp is the #2676 round-trip test:
// a manifest carrying maturity tiers renders "(beta)" /
// "(experimental)" after the short description in help output, GA
// and unrecognized tiers render no label, and a label-only short
// still surfaces for commands whose short is empty.
func TestRegister_MaturitySuffixInHelp(t *testing.T) {
	root := &cobra.Command{Use: "meho"}
	manifest := &CommandManifest{Commands: []Command{
		{Name: "sensor", Short: "Deterministic-check sensors", Maturity: "beta"},
		{Name: "ingest", Short: "Spec ingestion", Maturity: "experimental"},
		{Name: "audit", Short: "Audit queries", Maturity: "ga"},
		{Name: "future", Short: "From a newer backplane", Maturity: "alpha-preview"},
		{Name: "bare", Maturity: "beta"},
		{
			Name: "topology", Short: "Topology graph",
			Subcommands: []Command{
				{Name: "refresh", Short: "Refresh the graph", Maturity: "beta"},
			},
		},
	}}
	if err := Register(root, manifest); err != nil {
		t.Fatalf("Register: %v", err)
	}

	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("--help failed: %v", err)
	}
	help := stdout.String()
	for _, want := range []string{
		"Deterministic-check sensors (beta)",
		"Spec ingestion (experimental)",
	} {
		if !strings.Contains(help, want) {
			t.Errorf("--help missing %q:\n%s", want, help)
		}
	}
	for _, unwanted := range []string{
		"Audit queries (",
		"From a newer backplane (",
	} {
		if strings.Contains(help, unwanted) {
			t.Errorf("--help shows a maturity label it must not: %q\n%s", unwanted, help)
		}
	}

	// Empty short + non-GA tier: the label alone is the short text.
	var bare *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "bare" {
			bare = c
		}
	}
	if bare == nil {
		t.Fatal("bare subcommand not registered")
	}
	if bare.Short != "(beta)" {
		t.Errorf("bare.Short: got %q, want %q", bare.Short, "(beta)")
	}

	// Nested subcommands inherit the same rendering via the
	// recursive buildCommand walk.
	stdout.Reset()
	root.SetArgs([]string{"topology", "--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("topology --help failed: %v", err)
	}
	if !strings.Contains(stdout.String(), "Refresh the graph (beta)") {
		t.Errorf("nested subcommand help missing beta label:\n%s", stdout.String())
	}
}

// TestMaturity_WireRoundTrip drives the full path — httptest
// backplane JSON → Fetch → Register → cobra help — for a manifest
// carrying maturity, proving the wire field and the rendering agree
// end to end (#2676 acceptance criterion).
func TestMaturity_WireRoundTrip(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(Endpoint, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
            "commands": [
                {"name": "sensor", "short": "Deterministic-check sensors",
                 "maturity": "beta"}
            ]
        }`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	manifest, err := Fetch(context.Background(), srv.Client(), srv.URL)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	root := &cobra.Command{Use: "meho"}
	if err := Register(root, manifest); err != nil {
		t.Fatalf("Register: %v", err)
	}
	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("--help failed: %v", err)
	}
	if !strings.Contains(stdout.String(), "Deterministic-check sensors (beta)") {
		t.Errorf("--help missing wire-driven beta label:\n%s", stdout.String())
	}
}

// TestRegister_NoMaturityNoSuffix pins the other direction of the
// version skew: a manifest from a backplane that predates the
// maturity field (#2674 not deployed) renders help output without
// any label — byte-identical short descriptions.
func TestRegister_NoMaturityNoSuffix(t *testing.T) {
	root := &cobra.Command{Use: "meho"}
	manifest := &CommandManifest{Commands: []Command{
		{Name: "sensor", Short: "Deterministic-check sensors"},
	}}
	if err := Register(root, manifest); err != nil {
		t.Fatalf("Register: %v", err)
	}
	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("--help failed: %v", err)
	}
	if !strings.Contains(stdout.String(), "Deterministic-check sensors") {
		t.Fatalf("--help missing short description:\n%s", stdout.String())
	}
	if strings.Contains(stdout.String(), "Deterministic-check sensors (") {
		t.Errorf("--help shows a label for a maturity-less manifest:\n%s", stdout.String())
	}
}

// TestRegister_RefusesCollision confirms a manifest that
// advertises a name already taken by a built-in (login, status,
// version) is rejected, so a misconfigured backplane can't shadow
// a critical subcommand.
func TestRegister_RefusesCollision(t *testing.T) {
	root := &cobra.Command{Use: "meho"}
	root.AddCommand(&cobra.Command{Use: "login"})
	manifest := &CommandManifest{Commands: []Command{
		{Name: "login", Short: "Hostile shadow"},
	}}
	err := Register(root, manifest)
	if err == nil {
		t.Fatal("expected collision error, got nil")
	}
	if !strings.Contains(err.Error(), "shadows a built-in") {
		t.Errorf("unexpected collision message: %v", err)
	}
}

// TestRegister_NilManifest is a defensive smoke test: a nil
// manifest (theoretically impossible after Fetch but cheap to
// guard) is a no-op rather than a panic.
func TestRegister_NilManifest(t *testing.T) {
	root := &cobra.Command{Use: "meho"}
	if err := Register(root, nil); err != nil {
		t.Errorf("Register(nil) returned error: %v", err)
	}
}

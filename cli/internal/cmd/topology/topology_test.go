// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"bytes"
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// seedXDGAndToken redirects XDG_CONFIG_HOME / MEHO_KEYRING_DISABLE
// and seeds a token + config for the supplied backplane URL. Mirrors
// the targets-package helper of the same name; kept independent
// because that package can't be imported here.
func seedXDGAndToken(t *testing.T, backplaneURL string) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// attached and a bounded context.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// TestNewRootCmdWiresFourVerbs — the parent must expose exactly the
// four topology verbs (the fifth T6 verb, `targets discover`, lives
// on the targets parent, not here).
func TestNewRootCmdWiresFourVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"refresh": false, "dependents": false,
		"dependencies": false, "path": false,
	}
	for _, c := range root.Commands() {
		name := strings.Fields(c.Use)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("topology root missing %q subcommand", name)
		}
	}
}

// TestBuildRefreshPathEscapes — an operator-typed target with a slash
// must not split the URL path.
func TestBuildRefreshPathEscapes(t *testing.T) {
	got := buildRefreshPath("a/b")
	if got != "/api/v1/topology/refresh/a%2Fb" {
		t.Fatalf("buildRefreshPath: got %q", got)
	}
}

// TestBuildClosurePathOmitsDefaults — the empty-options shape sends
// no query string so the server applies its defaults.
func TestBuildClosurePathOmitsDefaults(t *testing.T) {
	got := buildClosurePath(closureOptions{Verb: "dependents", Name: "foo"})
	if got != "/api/v1/topology/dependents/foo" {
		t.Fatalf("default path: got %q", got)
	}
}

// TestBuildClosurePathMapsFlags — --depth / --kind / --node-kind land
// on the wire as depth / kind_filter / kind respectively (the route's
// param contract; --kind is the edge filter, --node-kind disambiguates
// the anchor).
func TestBuildClosurePathMapsFlags(t *testing.T) {
	got := buildClosurePath(closureOptions{
		Verb: "dependencies", Name: "db", Depth: 4,
		EdgeKind: "mounts", NodeKind: "datastore",
	})
	if !strings.HasPrefix(got, "/api/v1/topology/dependencies/db?") {
		t.Fatalf("path prefix wrong: %q", got)
	}
	for _, want := range []string{"depth=4", "kind_filter=mounts", "kind=datastore"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildClosurePath missing %q in %q", want, got)
		}
	}
}

// TestBuildPathQuerySetsFromTo — from/to are always present; the
// optional pins/hop cap ride along only when set.
func TestBuildPathQuerySetsFromTo(t *testing.T) {
	got := buildPathQuery(pathOptions{From: "a", To: "b"})
	for _, want := range []string{"from=a", "to=b"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildPathQuery missing %q in %q", want, got)
		}
	}
	if strings.Contains(got, "max_hops") {
		t.Errorf("buildPathQuery should omit max_hops when unset: %q", got)
	}
	got = buildPathQuery(pathOptions{From: "a", To: "b", MaxHops: 3, FromKind: "vm", ToKind: "host"})
	for _, want := range []string{"max_hops=3", "from_kind=vm", "to_kind=host"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildPathQuery missing %q in %q", want, got)
		}
	}
}

// TestPrintNodeClosureEmpty — zero rows render the not-found line
// (the cross-tenant / missing-node surface) without a header.
func TestPrintNodeClosureEmpty(t *testing.T) {
	var buf bytes.Buffer
	printNodeClosure(&buf, "ghost", nil)
	out := buf.String()
	if !strings.Contains(out, `no node named "ghost"`) {
		t.Errorf("empty closure missing not-found hint; got %q", out)
	}
	if strings.Contains(out, "DEPTH") {
		t.Errorf("empty closure should skip header; got %q", out)
	}
}

// TestPrintNodeClosureRendersRows — root (depth 0, empty via) plus a
// dependent with its via-edge kind.
func TestPrintNodeClosureRendersRows(t *testing.T) {
	via := "runs-on"
	rows := []Node{
		{ID: "1", Kind: "host", Name: "esxi-1", Depth: 0, ViaEdgeKind: nil},
		{ID: "2", Kind: "vm", Name: "web-1", Depth: 1, ViaEdgeKind: &via},
	}
	var buf bytes.Buffer
	printNodeClosure(&buf, "esxi-1", rows)
	out := buf.String()
	for _, want := range []string{"DEPTH", "KIND", "esxi-1", "web-1", "runs-on"} {
		if !strings.Contains(out, want) {
			t.Errorf("printNodeClosure missing %q in %q", want, out)
		}
	}
}

// TestPrintPathNil — a nil path (unreachable / missing endpoint /
// cross-tenant) renders the no-path line, never an error.
func TestPrintPathNil(t *testing.T) {
	var buf bytes.Buffer
	printPath(&buf, "a", "b", nil)
	if !strings.Contains(buf.String(), `no path from "a" to "b"`) {
		t.Errorf("nil path render wrong; got %q", buf.String())
	}
}

// TestPrintPathChain — a two-hop chain renders kind/name arrows and a
// pluralised hop count.
func TestPrintPathChain(t *testing.T) {
	p := &Path{
		Nodes: []Node{
			{Kind: "vm", Name: "web-1"},
			{Kind: "host", Name: "esxi-1"},
			{Kind: "datastore", Name: "ds-1"},
		},
		TotalHops: 2,
	}
	var buf bytes.Buffer
	printPath(&buf, "web-1", "ds-1", p)
	out := buf.String()
	if !strings.Contains(out, "vm/web-1 -> host/esxi-1 -> datastore/ds-1") {
		t.Errorf("path chain wrong; got %q", out)
	}
	if !strings.Contains(out, "(2 hops)") {
		t.Errorf("expected pluralised hop count; got %q", out)
	}
}

// TestPrintPathSingleHopSingular — one hop is rendered "1 hop".
func TestPrintPathSingleHopSingular(t *testing.T) {
	p := &Path{
		Nodes:     []Node{{Kind: "vm", Name: "a"}, {Kind: "host", Name: "b"}},
		TotalHops: 1,
	}
	var buf bytes.Buffer
	printPath(&buf, "a", "b", p)
	if !strings.Contains(buf.String(), "(1 hop)") {
		t.Errorf("expected singular hop; got %q", buf.String())
	}
}

// TestFormatAmbiguousNode — the 409 envelope is rendered into a line
// that names the colliding kinds and the --node-kind remedy (the
// anchor `kind` pin). It must NOT point at --kind, which maps to the
// edge filter `kind_filter` and would not clear the 409.
func TestFormatAmbiguousNode(t *testing.T) {
	body := `{"detail":{"error":"ambiguous_node","name":"prod","kinds":["host","vm"]}}`
	got := formatAmbiguousNode(body)
	for _, want := range []string{`"prod"`, "host", "vm", "--node-kind"} {
		if !strings.Contains(got, want) {
			t.Errorf("formatAmbiguousNode missing %q in %q", want, got)
		}
	}
	if strings.Contains(strings.ReplaceAll(got, "--node-kind", ""), "--kind") {
		t.Errorf("formatAmbiguousNode must not point at --kind (edge filter); got %q", got)
	}
}

// TestFormatNotFoundResolver — refresh's 404 resolver envelope yields
// the near-miss suggestion line.
func TestFormatNotFoundResolver(t *testing.T) {
	body := `{"detail":{"error":"no_target","query":"vc","matches":[{"name":"rdc-vcenter","aliases":["vc-prod"]}]}}`
	got := formatNotFound(body)
	if !strings.Contains(got, "rdc-vcenter") || !strings.Contains(got, "did you mean") {
		t.Errorf("formatNotFound missing near-miss; got %q", got)
	}
}

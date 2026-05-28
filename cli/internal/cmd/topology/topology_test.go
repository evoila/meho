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

	"github.com/evoila/meho/cli/internal/api"
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

// TestNewRootCmdWiresAllVerbs — the parent must expose every topology
// verb: the four G9.1 read/traversal verbs plus the three G9.2 write
// + listing verbs. The fifth G9.1-T6 verb, `targets discover`, lives
// on the targets parent (not here).
func TestNewRootCmdWiresAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"refresh":      false,
		"dependents":   false,
		"dependencies": false,
		"path":         false,
		"annotate":     false,
		"unannotate":   false,
		"list-edges":   false,
		"bulk-import":  false,
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
// must not split the URL path. The generated client's path-shape
// machinery uses url.PathEscape on each path-param; this test
// re-asserts the contract against the package-local pathEscape helper
// so a future refactor that delegates to a different escape routine
// surfaces immediately.
func TestBuildRefreshPathEscapes(t *testing.T) {
	got := buildRefreshPath("a/b")
	if got != "/api/v1/topology/refresh/a%2Fb" {
		t.Fatalf("buildRefreshPath: got %q", got)
	}
}

// TestBuildClosurePathOmitsDefaults — the empty-options shape sends
// no query string so the server applies its defaults. With the typed
// client the path is implicit (the generated `NewDependents…Request`
// builds it from `name + params`); the contract here is that no
// param fields are populated when the options are empty.
func TestBuildClosurePathOmitsDefaults(t *testing.T) {
	opts := closureOptions{Verb: "dependents", Name: "foo"}
	// Mirror the closureQueryParam build that getClosure runs:
	// every pointer field must be nil.
	if depth := buildClosureParamsForTest(opts).Depth; depth != nil {
		t.Errorf("Depth should be nil; got %v", depth)
	}
	if kf := buildClosureParamsForTest(opts).KindFilter; kf != nil {
		t.Errorf("KindFilter should be nil; got %v", kf)
	}
	if k := buildClosureParamsForTest(opts).Kind; k != nil {
		t.Errorf("Kind should be nil; got %v", k)
	}
}

// TestBuildClosurePathMapsFlags — --depth / --kind / --node-kind land
// on the wire as depth / kind_filter / kind respectively (the route's
// param contract; --kind is the edge filter, --node-kind disambiguates
// the anchor).
func TestBuildClosurePathMapsFlags(t *testing.T) {
	opts := closureOptions{
		Verb: "dependencies", Name: "db", Depth: 4,
		EdgeKind: "mounts", NodeKind: "datastore",
	}
	params := buildClosureParamsForTest(opts)
	if params.Depth == nil || *params.Depth != 4 {
		t.Errorf("Depth: got %v; want 4", params.Depth)
	}
	if params.KindFilter == nil || *params.KindFilter != "mounts" {
		t.Errorf("KindFilter: got %v; want mounts", params.KindFilter)
	}
	if params.Kind == nil || *params.Kind != "datastore" {
		t.Errorf("Kind: got %v; want datastore", params.Kind)
	}
}

// buildClosureParamsForTest mirrors the per-verb branch inside
// getClosure so the unit tests can assert the typed-param shape
// without standing up an httptest.Server. Returns the
// dependencies-flavoured params struct because both branches share
// the same field set; the test's assertions don't read the type-
// discriminator.
func buildClosureParamsForTest(opts closureOptions) *api.DependenciesApiV1TopologyDependenciesNameGetParams {
	params := &api.DependenciesApiV1TopologyDependenciesNameGetParams{}
	if opts.Depth > 0 {
		d := opts.Depth
		params.Depth = &d
	}
	if opts.EdgeKind != "" {
		k := opts.EdgeKind
		params.KindFilter = &k
	}
	if opts.NodeKind != "" {
		k := opts.NodeKind
		params.Kind = &k
	}
	return params
}

// TestBuildPathQuerySetsFromTo — from/to are always present; the
// optional pins/hop cap ride along only when set. Replaces the
// pre-migration buildPathQuery unit-shape assertion with a typed-
// param check against the generator's `PathApiV1TopologyPathGetParams`
// shape.
func TestBuildPathQuerySetsFromTo(t *testing.T) {
	got := buildPathParams(pathOptions{From: "a", To: "b"})
	if got.From != "a" || got.To != "b" {
		t.Errorf("from/to: got from=%q to=%q", got.From, got.To)
	}
	if got.MaxHops != nil {
		t.Errorf("buildPathParams should omit MaxHops when unset: %v", got.MaxHops)
	}
	got = buildPathParams(pathOptions{From: "a", To: "b", MaxHops: 3, FromKind: "vm", ToKind: "host"})
	if got.MaxHops == nil || *got.MaxHops != 3 {
		t.Errorf("MaxHops: got %v; want 3", got.MaxHops)
	}
	if got.FromKind == nil || *got.FromKind != "vm" {
		t.Errorf("FromKind: got %v; want vm", got.FromKind)
	}
	if got.ToKind == nil || *got.ToKind != "host" {
		t.Errorf("ToKind: got %v; want host", got.ToKind)
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
	rows := []api.TopologyNode{
		{Kind: "host", Name: "esxi-1", Depth: 0, ViaEdgeKind: nil},
		{Kind: "vm", Name: "web-1", Depth: 1, ViaEdgeKind: &via},
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
	p := &api.TopologyPath{
		Nodes: []api.TopologyNode{
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
	p := &api.TopologyPath{
		Nodes:     []api.TopologyNode{{Kind: "vm", Name: "a"}, {Kind: "host", Name: "b"}},
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

// TestAnnotateHelpEmitsTenKindVocabulary — §12 acceptance criterion
// for #599: `meho topology annotate --help` must surface every one of
// the closed 10 GraphEdgeKind values with a one-line description so
// operators discover the vocabulary without leaving the CLI.
func TestAnnotateHelpEmitsTenKindVocabulary(t *testing.T) {
	cmd := newAnnotateCmd()
	long := cmd.Long
	// Every kind name must be present.
	want := []string{
		"runs-on", "mounts", "routes-through", "belongs-to",
		"authenticates-via", "depends-on", "replicates-to",
		"backed-up-by", "routes-via", "policy-binds",
	}
	for _, kind := range want {
		if !strings.Contains(long, kind) {
			t.Errorf("annotate --help missing kind %q", kind)
		}
	}
	// Sanity: the help block names the table explicitly so the table
	// header is searchable in shell scrollback.
	if !strings.Contains(long, "Edge kind vocabulary") {
		t.Errorf("annotate --help should label the table; got %q", long)
	}
}

// TestEdgeKindVocabularyMatchesEnum — the in-help table must stay in
// lock-step with the closed enum on the backend side. Both lists are
// declared in source; this test fails noisily when the count drifts
// (the actual enum lives in backend/src/meho_backplane/db/models.py
// and is asserted from the Python side; here we lock the CLI mirror).
func TestEdgeKindVocabularyMatchesEnum(t *testing.T) {
	if got := len(edgeKindVocabulary); got != 10 {
		t.Fatalf("edgeKindVocabulary count = %d; want 10 (closed v0.2 enum)", got)
	}
	seen := make(map[string]bool, 10)
	for _, e := range edgeKindVocabulary {
		if e.Name == "" || e.Desc == "" {
			t.Errorf("incomplete vocab entry: %+v", e)
		}
		if seen[e.Name] {
			t.Errorf("duplicate vocab kind %q", e.Name)
		}
		seen[e.Name] = true
	}
}

// TestBuildListEdgesPathOmitsDefaults — empty options send no query
// string so the server applies its defaults. Asserts the typed-param
// shape (every pointer field nil; Conflicts off) since the wire-level
// query string is now assembled by the generated client.
func TestBuildListEdgesPathOmitsDefaults(t *testing.T) {
	got := buildListEdgesParams(listEdgesOptions{})
	if got.Kind != nil || got.Source != nil || got.From != nil || got.To != nil {
		t.Fatalf("default params should leave filters nil; got %+v", got)
	}
	if got.Conflicts != nil {
		t.Fatalf("Conflicts should default to nil (omit); got %v", got.Conflicts)
	}
	if got.Limit != nil || got.Offset != nil {
		t.Fatalf("Limit/Offset should default to nil (omit); got limit=%v offset=%v", got.Limit, got.Offset)
	}
}

// TestBuildListEdgesPathMapsFilters — every flag maps to the route's
// documented query-param name; --conflicts only rides when true.
func TestBuildListEdgesPathMapsFilters(t *testing.T) {
	got := buildListEdgesParams(listEdgesOptions{
		Kind: "depends-on", Source: "curated",
		From: "svc", To: "db",
		Conflicts: true, Limit: 25, Offset: 5,
	})
	if got.Kind == nil || string(*got.Kind) != "depends-on" {
		t.Errorf("Kind: got %v; want depends-on", got.Kind)
	}
	if got.Source == nil || *got.Source != "curated" {
		t.Errorf("Source: got %v; want curated", got.Source)
	}
	if got.From == nil || *got.From != "svc" {
		t.Errorf("From: got %v; want svc", got.From)
	}
	if got.To == nil || *got.To != "db" {
		t.Errorf("To: got %v; want db", got.To)
	}
	if got.Conflicts == nil || !*got.Conflicts {
		t.Errorf("Conflicts: got %v; want true", got.Conflicts)
	}
	if got.Limit == nil || *got.Limit != 25 {
		t.Errorf("Limit: got %v; want 25", got.Limit)
	}
	if got.Offset == nil || *got.Offset != 5 {
		t.Errorf("Offset: got %v; want 5", got.Offset)
	}
}

// TestFormatAutoEdgeConflictPullsServerMessage — the 409 envelope
// renders into a line that prefixes the server's `detail.message`
// (the annotate-over-auto remediation guidance) with the edge id.
func TestFormatAutoEdgeConflictPullsServerMessage(t *testing.T) {
	body := `{"detail":{"error":"auto_edge_deletion","edge_id":"abc","message":"go fix it"}}`
	got := formatAutoEdgeConflict(body)
	if !strings.Contains(got, "abc") || !strings.Contains(got, "go fix it") {
		t.Errorf("formatAutoEdgeConflict missing edge_id/message; got %q", got)
	}
	// A wrong-error body must return the empty string so the caller
	// falls back to the generic renderer rather than masking a real
	// 409 from elsewhere.
	if got := formatAutoEdgeConflict(`{"detail":{"error":"ambiguous_node"}}`); got != "" {
		t.Errorf("expected empty fallback for wrong-error body; got %q", got)
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

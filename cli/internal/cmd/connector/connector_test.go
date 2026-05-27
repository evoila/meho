// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// ---------- helper tests (pure-function) ----------

// TestTruncatePassthroughAndCut covers the rune-aware truncate
// helper. Same shape as the operation sibling's test — duplicated
// because cmd/connector can't import cmd/operation without an
// import cycle, and a future re-shape on either copy would diverge
// silently without an in-package pin.
func TestTruncatePassthroughAndCut(t *testing.T) {
	tests := []struct {
		name   string
		in     string
		maxLen int
		want   string
	}{
		{"within budget", "abc", 5, "abc"},
		{"over budget ascii", "abcdef", 4, "abc…"},
		{"multi-byte safe", "café world", 5, "café…"},
		{"zero budget", "x", 0, ""},
	}
	for _, tt := range tests {
		if got := truncate(tt.in, tt.maxLen); got != tt.want {
			t.Errorf("%s: truncate(%q, %d) = %q; want %q", tt.name, tt.in, tt.maxLen, got, tt.want)
		}
	}
}

// TestNormaliseURLBasic mirrors the operation sibling — trailing
// slash trimming + reject-empty are the load-bearing properties.
func TestNormaliseURLBasic(t *testing.T) {
	got, err := normaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
	if _, err := normaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty should reject; got %v", err)
	}
}

// TestNormaliseURLRejectsNonHTTPScheme — fail-fast on schemes the
// HTTP client can't dial (ftp://, ssh://, file:// for a backplane
// URL, etc.). Without this gate the operator only sees an obscure
// error later inside http.Client.Do.
func TestNormaliseURLRejectsNonHTTPScheme(t *testing.T) {
	cases := []string{
		"ftp://meho.test",
		"ssh://meho.test",
		"file:///tmp/meho",
	}
	for _, in := range cases {
		_, err := normaliseURL(in)
		if err == nil || !strings.Contains(err.Error(), "must use http or https") {
			t.Errorf("normaliseURL(%q): want http/https rejection; got %v", in, err)
		}
	}
}

// TestNormaliseURLAcceptsHTTP — plain http (no s) is accepted —
// operators on bench / staging deploys sometimes run without TLS.
func TestNormaliseURLAcceptsHTTP(t *testing.T) {
	got, err := normaliseURL("http://localhost:8080/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "http://localhost:8080" {
		t.Fatalf("got %q", got)
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any wrapping error) maps to AuthExpired; everything else maps
// to Unexpected. Same routing as the operation sibling.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &errNoBackplaneConfigured{inner: auth.ErrConfigNotFound}
	se := classifyBackplaneError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	se = classifyBackplaneError(errors.New("parse failure"))
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected_response; got %+v", se)
	}
}

// TestPathEscapeOpIDColonsAndSlashes — op_id values carry method
// + path (`GET:/api/vcenter/cluster`); the escape must keep them
// safe for URL path segments.
func TestPathEscapeOpIDColonsAndSlashes(t *testing.T) {
	got := pathEscapeOpID("GET:/api/vcenter/cluster")
	// url.PathEscape leaves ':' unescaped (RFC 3986 sub-delim-ok in
	// path segments) but escapes '/'. Both behaviours are correct;
	// we only assert that the result decodes back to the input.
	decoded, err := url.PathUnescape(got)
	if err != nil {
		t.Fatalf("PathUnescape: %v", err)
	}
	if decoded != "GET:/api/vcenter/cluster" {
		t.Fatalf("escape round-trip: got %q want %q", decoded, "GET:/api/vcenter/cluster")
	}
	if strings.Contains(got, "/") {
		t.Fatalf("op_id slashes should be escaped; got %q", got)
	}
}

// ---------- resolveSpecURI ----------

// TestResolveSpecURIFile — file:// passes through verbatim once
// validated (scheme=file, path absolute).
func TestResolveSpecURIFile(t *testing.T) {
	got, err := resolveSpecURI("file:///abs/path/spec.yaml")
	if err != nil {
		t.Fatalf("file scheme: %v", err)
	}
	if got != "file:///abs/path/spec.yaml" {
		t.Fatalf("file scheme passthrough; got %q", got)
	}
}

// TestResolveSpecURIFileRejectsRelative — a `file://relative/path`
// URI (no leading slash, so url.Parse reads `relative` as a host)
// or an empty path is rejected client-side so operators see a fast
// hint rather than a backplane 4xx.
func TestResolveSpecURIFileRejectsRelative(t *testing.T) {
	cases := []string{
		"file://relative/path/spec.yaml", // host=relative, not empty
		"file://",                        // empty path
		"file:///",                       // root only — no spec name
	}
	for _, in := range cases {
		_, err := resolveSpecURI(in)
		if err == nil || !strings.Contains(err.Error(), "file URI") {
			t.Errorf("resolveSpecURI(%q): want rejection; got %v", in, err)
		}
	}
}

// TestResolveSpecURIFileAcceptsLocalhostHost — `file://localhost/abs`
// is the RFC 8089 equivalent of `file:///abs` and must pass.
func TestResolveSpecURIFileAcceptsLocalhostHost(t *testing.T) {
	got, err := resolveSpecURI("file://localhost/abs/spec.yaml")
	if err != nil {
		t.Fatalf("file://localhost: %v", err)
	}
	if got != "file://localhost/abs/spec.yaml" {
		t.Fatalf("file://localhost passthrough; got %q", got)
	}
}

// TestResolveSpecURIHTTPS — http(s) passes through verbatim.
func TestResolveSpecURIHTTPS(t *testing.T) {
	got, err := resolveSpecURI("https://example.com/spec.yaml")
	if err != nil {
		t.Fatalf("https scheme: %v", err)
	}
	if got != "https://example.com/spec.yaml" {
		t.Fatalf("https passthrough; got %q", got)
	}
}

// TestResolveSpecURIDocsWithEnv — `docs:` shorthand resolves to a
// file:// path when CLAUDE_RDC_DOCS is set.
func TestResolveSpecURIDocsWithEnv(t *testing.T) {
	t.Setenv("CLAUDE_RDC_DOCS", "/opt/rdc/docs")
	got, err := resolveSpecURI("docs:vcenter-9.0/vcenter.yaml")
	if err != nil {
		t.Fatalf("docs shorthand: %v", err)
	}
	want := "file:///opt/rdc/docs/vcenter-9.0/vcenter.yaml"
	if got != want {
		t.Fatalf("docs shorthand: got %q want %q", got, want)
	}
}

// TestResolveSpecURIDocsNoEnv — `docs:` passes through verbatim
// when CLAUDE_RDC_DOCS is unset so the backplane can resolve it.
func TestResolveSpecURIDocsNoEnv(t *testing.T) {
	t.Setenv("CLAUDE_RDC_DOCS", "")
	got, err := resolveSpecURI("docs:vcenter-9.0/vcenter.yaml")
	if err != nil {
		t.Fatalf("docs shorthand, no env: %v", err)
	}
	if got != "docs:vcenter-9.0/vcenter.yaml" {
		t.Fatalf("docs shorthand passthrough; got %q", got)
	}
}

// TestResolveSpecURIUnknownScheme — anything not file/http(s)/docs
// rejects with a clear message.
func TestResolveSpecURIUnknownScheme(t *testing.T) {
	_, err := resolveSpecURI("ftp://example.com/spec.yaml")
	if err == nil || !strings.Contains(err.Error(), "unknown URI scheme") {
		t.Fatalf("unknown scheme should reject; got %v", err)
	}
}

// TestResolveSpecURIEmpty — empty input rejects.
func TestResolveSpecURIEmpty(t *testing.T) {
	_, err := resolveSpecURI("   ")
	if err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty spec should reject; got %v", err)
	}
}

// ---------- loadTextFlag ----------

// TestLoadTextFlagUnsetReturnsNotPresent — flag not provided ⇒ present=false
// so callers can omit the field from the PATCH body.
func TestLoadTextFlagUnsetReturnsNotPresent(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	cmd.SetArgs([]string{})
	if err := cmd.ParseFlags(nil); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	val, present, err := loadTextFlag(cmd, "name")
	if err != nil {
		t.Fatalf("loadTextFlag: %v", err)
	}
	if present {
		t.Fatalf("unset flag should report present=false")
	}
	if val != "" {
		t.Fatalf("unset flag should return empty val; got %q", val)
	}
}

// TestLoadTextFlagInlineText — `--name foo` ⇒ ("foo", true, nil).
func TestLoadTextFlagInlineText(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	if err := cmd.ParseFlags([]string{"--name", "foo"}); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	val, present, err := loadTextFlag(cmd, "name")
	if err != nil {
		t.Fatalf("loadTextFlag: %v", err)
	}
	if !present || val != "foo" {
		t.Fatalf("inline text: got (%q, %v); want (\"foo\", true)", val, present)
	}
}

// TestLoadTextFlagFileReference — `--name @path` ⇒ reads + trims
// trailing newline.
func TestLoadTextFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "blob.md")
	if err := os.WriteFile(path, []byte("hello world\n"), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	if err := cmd.ParseFlags([]string{"--name", "@" + path}); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	val, present, err := loadTextFlag(cmd, "name")
	if err != nil {
		t.Fatalf("loadTextFlag @file: %v", err)
	}
	if !present || val != "hello world" {
		t.Fatalf("file ref: got (%q, %v); want (\"hello world\", true)", val, present)
	}
}

// TestLoadTextFlagFileReferenceCRLF — file with CRLF line endings
// (`hello\r\n`) trims to "hello", not "hello\r". A stray `\r`
// silently slips into the persisted when_to_use / name field on
// every CRLF-saved override file otherwise.
func TestLoadTextFlagFileReferenceCRLF(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "blob.md")
	if err := os.WriteFile(path, []byte("hello world\r\n"), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	if err := cmd.ParseFlags([]string{"--name", "@" + path}); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	val, present, err := loadTextFlag(cmd, "name")
	if err != nil {
		t.Fatalf("loadTextFlag: %v", err)
	}
	if !present || val != "hello world" {
		t.Fatalf("CRLF trim: got (%q, %v); want (\"hello world\", true)", val, present)
	}
}

// TestLoadTextFlagMissingFile — `@nonexistent` surfaces a read
// failure with the path name.
func TestLoadTextFlagMissingFile(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	if err := cmd.ParseFlags([]string{"--name", "@/nonexistent/path/blob.md"}); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	_, _, err := loadTextFlag(cmd, "name")
	if err == nil || !strings.Contains(err.Error(), "read --name file") {
		t.Fatalf("missing file: should report read failure; got %v", err)
	}
}

// TestLoadTextFlagPresentEmpty — `--name ""` ⇒ ("", true, nil).
// This is load-bearing for the "clear an override" PATCH semantic.
func TestLoadTextFlagPresentEmpty(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.Flags().String("name", "", "")
	if err := cmd.ParseFlags([]string{"--name", ""}); err != nil {
		t.Fatalf("ParseFlags: %v", err)
	}
	val, present, err := loadTextFlag(cmd, "name")
	if err != nil {
		t.Fatalf("loadTextFlag empty: %v", err)
	}
	if !present || val != "" {
		t.Fatalf("--name '': got (%q, %v); want (\"\", true)", val, present)
	}
}

// ---------- renderers ----------

// TestPrintIngestSummaryDryRun — dry-run header + counts; no
// "Connector is in review_status=staged" trailer because that
// only applies after a real ingest. The canonical
// IngestionResultModel ships only the aggregate inserted/updated/
// skipped counts plus the two boolean flags — no per-spec
// breakdown, no embeddings split. Total is derived client-side.
func TestPrintIngestSummaryDryRun(t *testing.T) {
	r := &IngestResponse{
		Ingestion: IngestionResult{
			ConnectorID:   "vmware-rest-9.0",
			InsertedCount: 12,
		},
	}
	opts := ingestOptions{Product: "vmware", Version: "9.0", ImplID: "vmware-rest", DryRun: true}
	var buf bytes.Buffer
	printIngestSummary(&buf, opts, r)
	out := buf.String()
	for _, want := range []string{"DRY RUN", "12 total"} {
		if !strings.Contains(out, want) {
			t.Errorf("dry-run render missing %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "review_status=staged") {
		t.Errorf("dry-run render should not announce review_status=staged; got:\n%s", out)
	}
	// The dry-run path deliberately omits connector_registered /
	// operations_grouped because the route returns False for both
	// when dry_run=True, which is uninteresting noise.
	if strings.Contains(out, "connector_registered") {
		t.Errorf("dry-run should not print connector_registered; got:\n%s", out)
	}
}

// TestPrintIngestSummaryCatalogMode — catalog-mode regression (G0.14-T9
// / #1150). The pre-#1150 CLI resolved the catalog entry client-side
// and populated opts.Product/Version/ImplID before printing; post-#1150
// the backplane resolves the entry and opts stays empty in catalog
// mode. The heading must still carry the resolved triple — derived
// from the response's connector_id via parseConnectorID — so the
// operator-visible output (`ingest vmware/9.0/vmware-rest — ...`)
// matches v0.6.0 verbatim. The B1 review on PR #1182 caught the
// regression where opts.Product/Version/ImplID being empty rendered
// `ingest // — ...` (bare slashes); this test pins the fix.
func TestPrintIngestSummaryCatalogMode(t *testing.T) {
	cases := []struct {
		name   string
		dryRun bool
		// The bits the operator reads to identify what was ingested.
		// Bare `//` would mean the heading was rendered from empty
		// opts.Product/Version/ImplID — the regression we're guarding.
		wantStrings  []string
		bannedString string
	}{
		{
			name:        "dry-run",
			dryRun:      true,
			wantStrings: []string{"ingest vmware/9.0/vmware-rest", "DRY RUN"},
			// `//` appears bare only when product/version/impl are all
			// empty; the post-fix render carries `vmware/9.0/vmware-rest`.
			bannedString: "ingest // —",
		},
		{
			name:   "non-dry-run",
			dryRun: false,
			wantStrings: []string{
				"ingest vmware/9.0/vmware-rest",
				"connector_id=vmware-rest-9.0",
			},
			bannedString: "ingest // —",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := &IngestResponse{
				Ingestion: IngestionResult{
					ConnectorID:         "vmware-rest-9.0",
					InsertedCount:       961,
					ConnectorRegistered: true,
					OperationsGrouped:   !tc.dryRun,
				},
			}
			// Catalog mode: opts.Product/Version/ImplID are unset; the
			// CLI's runIngest leaves them empty because the request
			// shape is `{"catalog_entry": "..."}` (no client-side
			// resolution).
			opts := ingestOptions{Catalog: "vmware/9.0", DryRun: tc.dryRun}
			var buf bytes.Buffer
			printIngestSummary(&buf, opts, r)
			out := buf.String()
			for _, want := range tc.wantStrings {
				if !strings.Contains(out, want) {
					t.Errorf("catalog-mode render missing %q in:\n%s", want, out)
				}
			}
			if strings.Contains(out, tc.bannedString) {
				t.Errorf("catalog-mode render contains %q (bare-slash regression); got:\n%s",
					tc.bannedString, out)
			}
		})
	}
}

// TestIngestSummaryHeadingFromConnectorID — table-pins the parser's
// shape against the backend's parse_connector_id convention
// (backend/src/meho_backplane/operations/ingest/parser.py). The
// heading derives the (product, version, impl_id) triple from the
// response's connector_id so catalog mode (G0.14-T9 / #1150) renders
// the resolved triple instead of empty placeholders.
func TestIngestSummaryHeadingFromConnectorID(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		// Worked examples from parse_connector_id's docstring — the
		// CLI parser must agree on every one or the heading drifts
		// from `meho connector list` / review output.
		{"vmware-rest-9.0", "vmware/9.0/vmware-rest"},
		{"nsx-4.2", "nsx/4.2/nsx"},
		{"harbor-2.x", "harbor/2.x/harbor"},
		{"hetzner-robot-2026-04", "hetzner/2026-04/hetzner-robot"},
		{"vault-1.x", "vault/1.x/vault"},
		{"k8s-1.x", "k8s/1.x/k8s"},
		// Non-conforming connector_id (no dash-before-digit) falls
		// back to echoing verbatim — operators still see something
		// useful instead of bare slashes if the backend ever drifts.
		{"weird", "weird"},
		// Empty impl_id segment (`-9.0`) is non-conforming; fall back
		// to the verbatim form rather than render `/9.0/`.
		{"-9.0", "-9.0"},
	}
	for _, tc := range cases {
		if got := ingestSummaryHeading(tc.in); got != tc.want {
			t.Errorf("ingestSummaryHeading(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

// TestPrintIngestSummaryNonDryRun — happy-path render includes
// connector_id + next-steps hint + canonical IngestionResult fields
// (connector_registered, operations_grouped) and the canonical
// GroupingResult fields (groups_created, llm_call_count).
func TestPrintIngestSummaryNonDryRun(t *testing.T) {
	r := &IngestResponse{
		Ingestion: IngestionResult{
			ConnectorID:         "vmware-rest-9.0",
			InsertedCount:       961,
			ConnectorRegistered: true,
			OperationsGrouped:   true,
		},
		Grouping: &GroupingResult{
			ConnectorID:        "vmware-rest-9.0",
			GroupsCreated:      9,
			OperationsAssigned: 961,
			LLMCallCount:       20,
			LLMDurationMs:      4321,
		},
	}
	opts := ingestOptions{Product: "vmware", Version: "9.0", ImplID: "vmware-rest", DryRun: false}
	var buf bytes.Buffer
	printIngestSummary(&buf, opts, r)
	out := buf.String()
	for _, want := range []string{
		"connector_id=vmware-rest-9.0",
		"961 total",
		"connector_registered: true",
		"operations_grouped: true",
		"9 groups",
		"961 ops assigned",
		"20 LLM call(s)",
		"4321ms",
		"review_status=staged",
		"meho connector review vmware-rest-9.0",
		"meho connector enable vmware-rest-9.0",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("non-dry-run render missing %q in:\n%s", want, out)
		}
	}
}

// TestIngestResponseDecodesCanonical pins the wire shape: the
// canonical IngestionResultModel + GroupingResultModel (PR #488
// api_schemas.py) ship snake_case field names that mirror the
// Pydantic projections verbatim. Decoding drift here surfaces as
// a Blocker-class wire-contract failure on the next `meho
// connector ingest` round-trip.
func TestIngestResponseDecodesCanonical(t *testing.T) {
	raw := []byte(`{
		"ingestion": {
			"connector_id": "vmware-rest-9.0",
			"inserted_count": 961,
			"updated_count": 0,
			"skipped_count": 0,
			"connector_registered": true,
			"operations_grouped": true
		},
		"grouping": {
			"connector_id": "vmware-rest-9.0",
			"groups_created": 9,
			"operations_assigned": 961,
			"operations_unassigned": 0,
			"llm_call_count": 20,
			"llm_duration_ms": 4321.5
		}
	}`)
	var got IngestResponse
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode IngestResponse: %v", err)
	}
	if got.Ingestion.ConnectorID != "vmware-rest-9.0" {
		t.Errorf("ingestion.connector_id: got %q", got.Ingestion.ConnectorID)
	}
	if got.Ingestion.InsertedCount != 961 ||
		got.Ingestion.UpdatedCount != 0 ||
		got.Ingestion.SkippedCount != 0 {
		t.Errorf("ingestion counts: got %+v", got.Ingestion)
	}
	if !got.Ingestion.ConnectorRegistered || !got.Ingestion.OperationsGrouped {
		t.Errorf("ingestion flags: got %+v", got.Ingestion)
	}
	if got.Grouping == nil {
		t.Fatalf("grouping should not be nil")
	}
	if got.Grouping.GroupsCreated != 9 || got.Grouping.OperationsAssigned != 961 {
		t.Errorf("grouping counts: got %+v", got.Grouping)
	}
	if got.Grouping.LLMCallCount != 20 {
		t.Errorf("llm_call_count: got %d, want 20", got.Grouping.LLMCallCount)
	}
	// Float decode preserves sub-ms timing — int would truncate.
	if got.Grouping.LLMDurationMs != 4321.5 {
		t.Errorf("llm_duration_ms: got %v, want 4321.5", got.Grouping.LLMDurationMs)
	}
}

// TestIngestResponseDecodesDryRun pins the canonical dry-run shape:
// the route returns IngestionResult with all-zero counts and
// grouping=null when dry_run=true. The Go decoder must accept
// JSON `null` for the nullable grouping field.
func TestIngestResponseDecodesDryRun(t *testing.T) {
	raw := []byte(`{
		"ingestion": {
			"connector_id": "vmware-rest-9.0",
			"inserted_count": 0,
			"updated_count": 0,
			"skipped_count": 0,
			"connector_registered": false,
			"operations_grouped": false
		},
		"grouping": null
	}`)
	var got IngestResponse
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode dry-run IngestResponse: %v", err)
	}
	if got.Grouping != nil {
		t.Errorf("grouping should decode to nil for dry-run; got %+v", got.Grouping)
	}
	if got.Ingestion.ConnectorRegistered || got.Ingestion.OperationsGrouped {
		t.Errorf("dry-run flags should both be false; got %+v", got.Ingestion)
	}
}

// TestPrintListTableEmpty — zero connectors renders the count line.
func TestPrintListTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, "staged", &ListResponse{})
	if !strings.Contains(buf.String(), "0 connector(s) with status=staged") {
		t.Errorf("empty list: missing 0-count line; got:\n%s", buf.String())
	}
}

// TestPrintListTableHappyPath — happy-path render with two connectors;
// built-in connectors (TenantID=nil) render as "(built-in)". The
// rollup label is derived per-row from the three *_group_count fields
// (see deriveRollupLabel) — the canonical wire shape has no
// connector-wide review_status field.
func TestPrintListTableHappyPath(t *testing.T) {
	tenantA := "tenant-a"
	r := &ListResponse{
		Connectors: []Summary{
			{
				ConnectorID:       "vault-1.x",
				GroupCount:        2,
				EnabledGroupCount: 2,
				OperationCount:    7,
				TenantID:          nil,
			},
			{
				ConnectorID:      "vmware-rest-9.0",
				GroupCount:       9,
				StagedGroupCount: 9,
				OperationCount:   961,
				TenantID:         &tenantA,
			},
		},
	}
	var buf bytes.Buffer
	printListTable(&buf, "all", r)
	out := buf.String()
	for _, want := range []string{"2 connector(s) with status=all", "vault-1.x", "enabled", "(built-in)", "vmware-rest-9.0", "staged", "tenant-a", "961"} {
		if !strings.Contains(out, want) {
			t.Errorf("list render missing %q in:\n%s", want, out)
		}
	}
}

// TestDeriveRollupLabelTable pins the per-status → rollup mapping.
// Operators reading `meho connector list` see this label; getting it
// right matters for the "is there review backlog?" question.
func TestDeriveRollupLabelTable(t *testing.T) {
	cases := []struct {
		name                      string
		staged, enabled, disabled int
		want                      string
	}{
		{"empty connector", 0, 0, 0, "(empty)"},
		{"all staged", 3, 0, 0, "staged"},
		{"all enabled", 0, 5, 0, "enabled"},
		{"all disabled", 0, 0, 2, "disabled"},
		{"partial enable", 1, 2, 0, "mixed"},
		{"staged plus disabled", 1, 0, 1, "mixed"},
		{"every bucket non-zero", 1, 1, 1, "mixed"},
	}
	for _, tc := range cases {
		got := deriveRollupLabel(tc.staged, tc.enabled, tc.disabled)
		if got != tc.want {
			t.Errorf("%s: deriveRollupLabel(%d,%d,%d) = %q; want %q",
				tc.name, tc.staged, tc.enabled, tc.disabled, got, tc.want)
		}
	}
}

// TestSummaryDecodesCanonicalListItem pins the wire shape: the
// canonical ConnectorListItem (PR #488 api_schemas.py) ships
// per-status group counts and no top-level review_status. Decoding
// drift here surfaces as a Major-class wire-contract failure on the
// next ingest round-trip.
func TestSummaryDecodesCanonicalListItem(t *testing.T) {
	raw := []byte(`{
		"connector_id": "vmware-rest-9.0",
		"product": "vmware",
		"version": "9.0",
		"impl_id": "vmware-rest",
		"tenant_id": null,
		"group_count": 9,
		"staged_group_count": 5,
		"enabled_group_count": 3,
		"disabled_group_count": 1,
		"operation_count": 961
	}`)
	var got Summary
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode ConnectorListItem: %v", err)
	}
	if got.ConnectorID != "vmware-rest-9.0" {
		t.Errorf("connector_id: got %q", got.ConnectorID)
	}
	if got.TenantID != nil {
		t.Errorf("tenant_id should be nil for built-in; got %v", got.TenantID)
	}
	if got.StagedGroupCount != 5 || got.EnabledGroupCount != 3 || got.DisabledGroupCount != 1 {
		t.Errorf("per-status counts: got %+v", got)
	}
}

// TestPrintReviewTableHappyPath — review render shows groups + ops
// + per-group review_status flags. The connector-wide rollup label
// is derived from the per-group review_status counts (same shape as
// `meho connector list`).
func TestPrintReviewTableHappyPath(t *testing.T) {
	summary := "List vSphere clusters"
	r := &ReviewPayload{
		ConnectorID:  "vmware-rest-9.0",
		Product:      "vmware",
		Version:      "9.0",
		ImplID:       "vmware-rest",
		TotalOpCount: 2,
		Groups: []ReviewGroup{
			{
				GroupKey:     "cluster",
				Name:         "Cluster",
				WhenToUse:    "Use for vSphere cluster lifecycle ops.",
				ReviewStatus: "staged",
				OpCount:      2,
				Ops: []ReviewOperation{
					{OpID: "GET:/api/vcenter/cluster", Summary: &summary, SafetyLevel: "safe", IsEnabled: false},
					{OpID: "DELETE:/api/vcenter/cluster/{id}", SafetyLevel: "dangerous", RequiresApproval: true, IsEnabled: false},
				},
			},
		},
	}
	var buf bytes.Buffer
	printReviewTable(&buf, r)
	out := buf.String()
	for _, want := range []string{"vmware-rest-9.0", "staged", "[cluster]", "Cluster", "vSphere cluster lifecycle", "GET:/api/vcenter/cluster", "safe", "DELETE:/api/vcenter/cluster", "dangerous"} {
		if !strings.Contains(out, want) {
			t.Errorf("review render missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintReviewTableEmptyGroups — zero-group connector renders
// the explanation line and shows "(empty)" rollup.
func TestPrintReviewTableEmptyGroups(t *testing.T) {
	r := &ReviewPayload{ConnectorID: "k8s-1.x", Groups: nil}
	var buf bytes.Buffer
	printReviewTable(&buf, r)
	if !strings.Contains(buf.String(), "no groups") {
		t.Errorf("empty review: missing explanation; got:\n%s", buf.String())
	}
}

// TestReviewPayloadDecodesCanonical pins the wire shape: the
// canonical ConnectorReviewGroup (PR #431 / PR #488) ships `ops`
// (not `operations`) and a per-group `review_status`; the payload
// has no top-level review_status. Decoding drift here surfaces as
// a Major-class wire-contract failure on the next `meho connector
// review` round-trip.
func TestReviewPayloadDecodesCanonical(t *testing.T) {
	raw := []byte(`{
		"connector_id": "vmware-rest-9.0",
		"product": "vmware",
		"version": "9.0",
		"impl_id": "vmware-rest",
		"tenant_id": null,
		"groups": [
			{
				"group_key": "cluster",
				"name": "Cluster",
				"when_to_use": "use for cluster lifecycle ops",
				"review_status": "staged",
				"op_count": 1,
				"ops": [
					{
						"op_id": "GET:/api/vcenter/cluster",
						"summary": "List clusters",
						"description": null,
						"custom_description": null,
						"safety_level": "safe",
						"requires_approval": false,
						"is_enabled": false,
						"tags": ["cluster", "list"]
					}
				]
			}
		],
		"total_op_count": 1
	}`)
	var got ReviewPayload
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode ConnectorReviewPayload: %v", err)
	}
	if got.TotalOpCount != 1 {
		t.Errorf("total_op_count: got %d", got.TotalOpCount)
	}
	if len(got.Groups) != 1 {
		t.Fatalf("groups: got %d, want 1", len(got.Groups))
	}
	g := got.Groups[0]
	if g.ReviewStatus != "staged" {
		t.Errorf("group review_status: got %q", g.ReviewStatus)
	}
	if g.OpCount != 1 {
		t.Errorf("group op_count: got %d", g.OpCount)
	}
	if len(g.Ops) != 1 {
		t.Fatalf("group ops: got %d, want 1", len(g.Ops))
	}
	op := g.Ops[0]
	if op.OpID != "GET:/api/vcenter/cluster" {
		t.Errorf("op_id: got %q", op.OpID)
	}
	if len(op.Tags) != 2 || op.Tags[0] != "cluster" {
		t.Errorf("op tags: got %v", op.Tags)
	}
}

// ---------- EditOpBody marshal ----------

// TestEditOpBodyMarshalOmitsUnset — a body with only IsEnabled set
// must marshal to exactly that one field; absent fields stay absent
// (no explicit null) so the PATCH semantic on the backend stays
// "leave unchanged".
func TestEditOpBodyMarshalOmitsUnset(t *testing.T) {
	enabled := true
	body := EditOpBody{IsEnabled: &enabled}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(raw) != `{"is_enabled":true}` {
		t.Fatalf("unset fields should be omitted; got %s", raw)
	}
}

// TestEditOpBodyMarshalAllFields — all-fields-set marshal round-
// trips with the right keys.
func TestEditOpBodyMarshalAllFields(t *testing.T) {
	desc := "Read a vault KV secret"
	safety := "caution"
	req := true
	enabled := false
	body := EditOpBody{
		CustomDescription: &desc,
		SafetyLevel:       &safety,
		RequiresApproval:  &req,
		IsEnabled:         &enabled,
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, want := range []string{`"custom_description":"Read a vault KV secret"`, `"safety_level":"caution"`, `"requires_approval":true`, `"is_enabled":false`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("marshal missing %q in %s", want, raw)
		}
	}
}

// TestIsEmptyEditOpBody — both branches.
func TestIsEmptyEditOpBody(t *testing.T) {
	if !isEmptyEditOpBody(EditOpBody{}) {
		t.Fatalf("zero-value body should be empty")
	}
	enabled := true
	if isEmptyEditOpBody(EditOpBody{IsEnabled: &enabled}) {
		t.Fatalf("body with IsEnabled set should not be empty")
	}
}

// ---------- transition + confirm ----------

// TestConfirmYes — typing y on stdin returns true.
func TestConfirmYes(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.SetIn(strings.NewReader("y\n"))
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	if !confirm(cmd, "really?") {
		t.Fatalf("confirm 'y' should return true")
	}
}

// TestConfirmNo — typing n returns false.
func TestConfirmNo(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.SetIn(strings.NewReader("n\n"))
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	if confirm(cmd, "really?") {
		t.Fatalf("confirm 'n' should return false")
	}
}

// TestConfirmEOF — closed stdin returns false (scripted/no-tty
// path should not accidentally enable).
func TestConfirmEOF(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	cmd.SetIn(strings.NewReader(""))
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	if confirm(cmd, "really?") {
		t.Fatalf("confirm with empty stdin should return false")
	}
}

// TestPrintTransitionResult — happy-path render. T6's enable /
// disable routes return HTTP 204 No Content, so the renderer's
// only input is the synthetic transitionResult envelope the verb
// constructs locally (connector_id + action).
func TestPrintTransitionResult(t *testing.T) {
	r := transitionResult{
		ConnectorID: "vmware-rest-9.0",
		Action:      "enabled",
	}
	var buf bytes.Buffer
	printTransitionResult(&buf, "enable", r)
	out := buf.String()
	for _, want := range []string{"enable vmware-rest-9.0", "enabled", "204 No Content"} {
		if !strings.Contains(out, want) {
			t.Errorf("transition render missing %q in:\n%s", want, out)
		}
	}
}

// ---------- httpError ----------

// TestHTTPErrorString — Error() format pins the renderer's input
// shape.
func TestHTTPErrorString(t *testing.T) {
	he := &httpError{StatusCode: 403, Body: "forbidden"}
	if he.Error() != "HTTP 403: forbidden" {
		t.Fatalf("httpError format: got %q", he.Error())
	}
}

// ---------- HTTP wire shape (mocked) ----------

// TestPostIngestRoundTripWithMockServer — pins the wire contract
// between T5 and T6 (#488). The CLI POSTs the IngestRequest, the
// (mocked) T6 endpoint validates the shape and returns an
// IngestResponse with canonical IngestionResultModel /
// GroupingResultModel fields (inserted_count / groups_created etc).
// Used for the JSON contract sanity check that doesn't require a
// live backplane — true end-to-end coverage lives in the T8 canary.
func TestPostIngestRoundTripWithMockServer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			var req IngestRequest
			if err := json.Unmarshal(body, &req); err != nil {
				t.Errorf("decode IngestRequest body: %v", err)
				w.WriteHeader(400)
				return
			}
			// G0.14-T9 (#1150): IngestRequest.Product/Version/ImplID are
			// now *string so the JSON serializer can omit them on the
			// catalog-driven shape. Deref for the equality check; nil
			// here means the test request was misconstructed.
			if req.Product == nil || req.Version == nil || req.ImplID == nil ||
				*req.Product != "vmware" || *req.Version != "9.0" || *req.ImplID != "vmware-rest" {
				t.Errorf("unexpected triple: %+v", req)
			}
			if len(req.Specs) != 1 || req.Specs[0].URI != "file:///abs/vcenter.yaml" {
				t.Errorf("unexpected specs: %+v", req.Specs)
			}
			resp := IngestResponse{
				Ingestion: IngestionResult{
					ConnectorID:         "vmware-rest-9.0",
					InsertedCount:       961,
					ConnectorRegistered: true,
					OperationsGrouped:   true,
				},
				Grouping: &GroupingResult{
					ConnectorID:        "vmware-rest-9.0",
					GroupsCreated:      9,
					OperationsAssigned: 961,
				},
			}
			writeJSON(t, w, 200, resp)
		},
	})
	defer srv.Close()

	primeToken(t, srv.URL)
	product := "vmware"
	version := "9.0"
	implID := "vmware-rest"
	got, err := postIngest(context.Background(), srv.URL, IngestRequest{
		Product: &product, Version: &version, ImplID: &implID,
		Specs: []SpecSource{{URI: "file:///abs/vcenter.yaml"}},
	})
	if err != nil {
		t.Fatalf("postIngest: %v", err)
	}
	if got.Ingestion.ConnectorID != "vmware-rest-9.0" || got.Ingestion.InsertedCount != 961 {
		t.Fatalf("unexpected ingest result: %+v", got)
	}
	if got.Grouping == nil || got.Grouping.GroupsCreated != 9 {
		t.Fatalf("unexpected grouping: %+v", got.Grouping)
	}
}

// TestGetListWithMockServer — validates the status query param
// shape and the list response decode.
func TestGetListWithMockServer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("status") != "staged" {
				t.Errorf("expected status=staged; got %q", r.URL.Query().Get("status"))
			}
			writeJSON(t, w, 200, ListResponse{
				Connectors: []Summary{
					{
						ConnectorID:      "vmware-rest-9.0",
						GroupCount:       9,
						StagedGroupCount: 9,
						OperationCount:   961,
					},
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	got, err := getList(context.Background(), srv.URL, "staged")
	if err != nil {
		t.Fatalf("getList: %v", err)
	}
	if len(got.Connectors) != 1 || got.Connectors[0].ConnectorID != "vmware-rest-9.0" {
		t.Fatalf("unexpected list: %+v", got)
	}
}

// TestGetListAllOmitsStatusQueryParam — the `all` filter sends no
// `status` param (T6 treats absent as no-filter via `Literal | None`).
func TestGetListAllOmitsStatusQueryParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("status"); got != "" {
				t.Errorf("status=all should not send param; got %q", got)
			}
			writeJSON(t, w, 200, ListResponse{})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := getList(context.Background(), srv.URL, "all"); err != nil {
		t.Fatalf("getList all: %v", err)
	}
}

// TestPatchGroupSendsBody — confirms the wire shape for edit-group.
// T6's PATCH route returns HTTP 204 No Content; the test asserts the
// PATCH body shape and that the 204 path returns no error (no decode
// of a non-existent JSON body).
func TestPatchGroupSendsBody(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vmware-rest-9.0/groups/cluster": func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			var got EditGroupBody
			if err := json.Unmarshal(body, &got); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if got.WhenToUse == nil || *got.WhenToUse != "use for cluster ops" {
				t.Errorf("when_to_use missing; got %+v", got)
			}
			if got.Name != nil {
				t.Errorf("Name should be unset; got %+v", got.Name)
			}
			// Canonical T6 response: 204 No Content with no body.
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	whenToUse := "use for cluster ops"
	if err := patchGroup(context.Background(), srv.URL, "vmware-rest-9.0", "cluster", EditGroupBody{WhenToUse: &whenToUse}); err != nil {
		t.Fatalf("patchGroup: %v", err)
	}
}

// TestPrintEditGroupResultRendersBody — the renderer outputs the
// connector_id/group_key coordinates + the operator's PATCH body
// fields (the 204 No Content response carries no body to mirror).
func TestPrintEditGroupResultRendersBody(t *testing.T) {
	whenToUse := "use for cluster lifecycle ops"
	name := "Cluster"
	var buf bytes.Buffer
	printEditGroupResult(&buf, "vmware-rest-9.0", "cluster", EditGroupBody{
		WhenToUse: &whenToUse,
		Name:      &name,
	})
	out := buf.String()
	for _, want := range []string{
		"vmware-rest-9.0/cluster",
		"204 No Content",
		"name: Cluster",
		"when_to_use: use for cluster lifecycle ops",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("edit-group render missing %q in:\n%s", want, out)
		}
	}
}

// TestPatchOpEscapesOpID — colons and slashes in op_id must
// survive the URL path. The mock asserts the decoded segment and
// returns the canonical T6 204 No Content response.
func TestPatchOpEscapesOpID(t *testing.T) {
	called := false
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			// Catch-all: confirm the escaped path segment carrying
			// the op_id round-trips back to "GET:/api/vcenter/cluster".
			// r.URL.RawPath holds the on-the-wire encoded form;
			// r.URL.Path is the auto-decoded form FastAPI's
			// `{op_id:path}` route ultimately sees.
			called = true
			if r.Method != "PATCH" {
				t.Errorf("expected PATCH; got %s", r.Method)
			}
			raw := r.URL.RawPath
			if raw == "" {
				raw = r.URL.Path
			}
			parts := strings.Split(strings.TrimPrefix(raw, "/"), "/")
			if len(parts) < 6 {
				t.Errorf("path too short: %q", raw)
				w.WriteHeader(404)
				return
			}
			decodedOp, err := url.PathUnescape(parts[5])
			if err != nil {
				t.Errorf("PathUnescape: %v", err)
			}
			if decodedOp != "GET:/api/vcenter/cluster" {
				t.Errorf("op_id round-trip: got %q (raw path %q)", decodedOp, raw)
			}
			// Canonical T6 response: 204 No Content with no body.
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	safety := "dangerous"
	if err := patchOp(context.Background(), srv.URL, "vmware-rest-9.0", "GET:/api/vcenter/cluster", EditOpBody{SafetyLevel: &safety}); err != nil {
		t.Fatalf("patchOp: %v", err)
	}
	if !called {
		t.Fatalf("mock handler not invoked")
	}
}

// TestPostTransitionEnable204 — pins the canonical enable / disable
// wire shape. T6 returns HTTP 204 No Content with no body; the CLI
// must accept the 204 as success without trying to decode a JSON
// envelope. A regression to the old 200+JSON contract would either
// fail decode (empty body) or 500-classify (200 with malformed body).
func TestPostTransitionEnable204(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/vmware-rest-9.0/enable": func(w http.ResponseWriter, r *http.Request) {
			// Canonical T6 response: 204 No Content with no body.
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if err := postTransition(context.Background(), srv.URL, "/api/v1/connectors/vmware-rest-9.0/enable"); err != nil {
		t.Fatalf("postTransition: %v", err)
	}
}

// TestDoAuthedRequest204AcceptsEmptyBody — pins the load-bearing
// behaviour of the shared HTTP helper: a 204 No Content response
// must surface as a nil error with an empty byte slice, NOT as an
// *httpError. Three of the seven T6 routes return 204 (enable,
// disable, PATCH edit-group, PATCH edit-op), so this property gates
// the entire mutating-verb surface.
func TestDoAuthedRequest204AcceptsEmptyBody(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/test": func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	body, err := doAuthedRequest(context.Background(), srv.URL, "POST", "/api/v1/test", []byte("{}"))
	if err != nil {
		t.Fatalf("204 should be a success; got err %v", err)
	}
	if len(body) != 0 {
		t.Fatalf("204 should yield empty body; got %q", body)
	}
}

// TestDecodeErrorClassifiedAsUnexpected — a 200 OK with garbage
// JSON body must classify as `unexpected_response` (the request
// reached the backplane and the backplane returned 200; the body
// just didn't match the agreed contract). Without this branch, a
// contract drift between T5 and T6 presents to the operator as
// "your network is down", which is misleading.
func TestDecodeErrorClassifiedAsUnexpected(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(200)
			// Malformed JSON — triggers json.SyntaxError on decode.
			_, _ = w.Write([]byte(`{"connectors": [{"connector_id": "x"`))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, err := getList(context.Background(), srv.URL, "all")
	if err == nil {
		t.Fatalf("expected decode error; got nil")
	}
	// The error should be classifiable as a JSON syntax error so
	// renderRequestError routes it to Unexpected (not Unreachable).
	var se *json.SyntaxError
	var ute *json.UnmarshalTypeError
	if !errors.As(err, &se) && !errors.As(err, &ute) && !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("expected JSON decode error; got %T %v", err, err)
	}
}

// TestHTTPErrorClassification403 — backplane 403 propagates as a
// *httpError with the right status code; renderRequestError maps
// to insufficient_role.
func TestHTTPErrorClassification403(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(403)
			_, _ = w.Write([]byte("tenant_admin required"))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	product := "x"
	version := "y"
	implID := "z"
	_, err := postIngest(context.Background(), srv.URL, IngestRequest{
		Product: &product, Version: &version, ImplID: &implID,
		Specs: []SpecSource{{URI: "file:///a.yaml"}},
	})
	if err == nil {
		t.Fatalf("expected 403 error; got nil")
	}
	var he *httpError
	if !errors.As(err, &he) || he.StatusCode != 403 {
		t.Fatalf("expected *httpError 403; got %T %v", err, err)
	}
}

// ---------- httptest helpers ----------

type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>` keys. The empty key acts as a catch-all when
// the test wants to validate URL escaping or other path-derived
// behaviour. Tests are responsible for calling Close() via defer.
func mockBackplane(t *testing.T, routes map[string]mockHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		if h, ok := routes[""]; ok {
			h(w, r)
			return
		}
		t.Errorf("mockBackplane: unhandled route %s", key)
		w.WriteHeader(404)
	}))
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeJSON marshal: %v", err)
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeJSON write: %v", err)
	}
}

// primeToken installs an in-memory token store with a usable
// bearer for the mocked backplane URL. Uses a test-only override
// so the production keychain isn't touched.
func primeToken(t *testing.T, backplaneURL string) {
	t.Helper()
	// The default auth.NewTokenStore + auth.LoadConfig path expects
	// a real config file + keychain entry. Tests can short-circuit
	// by setting XDG_CONFIG_HOME to a tempdir and writing both.
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfg := filepath.Join(dir, "meho", "config.json")
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": backplaneURL})
	if err := os.WriteFile(cfg, cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	// Write a file-backed token (mirrors auth.fileTokenStore shape).
	// auth.KeyForBackplane derives (service, user) from the URL.
	service, user := auth.KeyForBackplane(backplaneURL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: backplaneURL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}

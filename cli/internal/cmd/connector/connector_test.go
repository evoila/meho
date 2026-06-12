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
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
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

// ---------- resolveSpecURI ----------

const sampleSpecYAML = "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\n"

// TestResolveSpecURIFile -- file:// is read CLI-side; the uri is kept as
// the audit label and the file bytes are returned as content so no local
// path reaches the backplane.
func TestResolveSpecURIFile(t *testing.T) {
	specPath := filepath.Join(t.TempDir(), "spec.yaml")
	if err := os.WriteFile(specPath, []byte(sampleSpecYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	uri := "file://" + specPath
	gotURI, content, err := resolveSpecURI(uri)
	if err != nil {
		t.Fatalf("file scheme: %v", err)
	}
	if gotURI != uri {
		t.Fatalf("file uri label; got %q want %q", gotURI, uri)
	}
	if content != sampleSpecYAML {
		t.Fatalf("file content; got %q", content)
	}
}

// TestResolveSpecURIFileMissing -- a file:// to a nonexistent path is
// rejected client-side with the read error.
func TestResolveSpecURIFileMissing(t *testing.T) {
	_, _, err := resolveSpecURI("file:///no/such/spec.yaml")
	if err == nil {
		t.Fatalf("missing file should reject; got nil error")
	}
}

// TestResolveSpecURIFileRejectsRelative -- a `file://relative/path` URI
// (no leading slash, so url.Parse reads `relative` as a host) or an
// empty/root path is rejected client-side.
func TestResolveSpecURIFileRejectsRelative(t *testing.T) {
	cases := []string{
		"file://relative/path/spec.yaml",
		"file://",
		"file:///",
	}
	for _, in := range cases {
		_, _, err := resolveSpecURI(in)
		if err == nil || !strings.Contains(err.Error(), "file URI") {
			t.Errorf("resolveSpecURI(%q): want rejection; got %v", in, err)
		}
	}
}

// TestResolveSpecURIFileAcceptsLocalhostHost -- `file://localhost/abs` is
// the RFC 8089 equivalent of `file:///abs` and is read the same way.
func TestResolveSpecURIFileAcceptsLocalhostHost(t *testing.T) {
	specPath := filepath.Join(t.TempDir(), "spec.yaml")
	if err := os.WriteFile(specPath, []byte(sampleSpecYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	uri := "file://localhost" + specPath
	gotURI, content, err := resolveSpecURI(uri)
	if err != nil {
		t.Fatalf("file://localhost: %v", err)
	}
	if gotURI != uri || content != sampleSpecYAML {
		t.Fatalf("file://localhost; got uri=%q content=%q", gotURI, content)
	}
}

// TestResolveSpecURIHTTPS -- https passes through as the uri; no content
// (the backplane fetches it under the https-only guard).
func TestResolveSpecURIHTTPS(t *testing.T) {
	gotURI, content, err := resolveSpecURI("https://example.com/spec.yaml")
	if err != nil {
		t.Fatalf("https scheme: %v", err)
	}
	if gotURI != "https://example.com/spec.yaml" || content != "" {
		t.Fatalf("https passthrough; got uri=%q content=%q", gotURI, content)
	}
}

// TestResolveSpecURIDocsWithEnv -- `docs:` resolves against
// CLAUDE_RDC_DOCS, is read CLI-side, and uploads the bytes as content
// while keeping the `docs:` label as the uri.
func TestResolveSpecURIDocsWithEnv(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "vcenter-9.0"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "vcenter-9.0", "vcenter.yaml"), []byte(sampleSpecYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLAUDE_RDC_DOCS", root)
	gotURI, content, err := resolveSpecURI("docs:vcenter-9.0/vcenter.yaml")
	if err != nil {
		t.Fatalf("docs shorthand: %v", err)
	}
	if gotURI != "docs:vcenter-9.0/vcenter.yaml" {
		t.Fatalf("docs uri label; got %q", gotURI)
	}
	if content != sampleSpecYAML {
		t.Fatalf("docs content; got %q", content)
	}
}

// TestResolveSpecURIDocsNoEnv -- `docs:` with no CLAUDE_RDC_DOCS is
// rejected client-side with a hint naming the env var (#1535).
func TestResolveSpecURIDocsNoEnv(t *testing.T) {
	t.Setenv("CLAUDE_RDC_DOCS", "")
	_, _, err := resolveSpecURI("docs:vcenter-9.0/vcenter.yaml")
	if err == nil {
		t.Fatalf("docs shorthand with no env should reject; got nil error")
	}
	if !strings.Contains(err.Error(), "CLAUDE_RDC_DOCS") {
		t.Fatalf("docs rejection should name CLAUDE_RDC_DOCS; got %v", err)
	}
}

// TestResolveSpecURIUnknownScheme -- anything not file/http(s)/docs rejects.
func TestResolveSpecURIUnknownScheme(t *testing.T) {
	_, _, err := resolveSpecURI("ftp://example.com/spec.yaml")
	if err == nil || !strings.Contains(err.Error(), "unknown URI scheme") {
		t.Fatalf("unknown scheme should reject; got %v", err)
	}
}

// TestResolveSpecURIEmpty -- empty input rejects.
func TestResolveSpecURIEmpty(t *testing.T) {
	_, _, err := resolveSpecURI("   ")
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

// ---------- validateIngestMode ----------

func TestValidateIngestModeTable(t *testing.T) {
	cases := []struct {
		name    string
		opts    ingestOptions
		wantErr string // substring; "" = no error
	}{
		{"catalog only", ingestOptions{Catalog: "vmware/9.0"}, ""},
		{"manual complete", ingestOptions{
			Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
			Specs: []string{"file:///x.yaml"},
		}, ""},
		{"catalog + manual", ingestOptions{
			Catalog: "vmware/9.0", Product: "vmware",
		}, "cannot be combined"},
		{"neither", ingestOptions{}, "specify a connector"},
		{"manual missing impl+spec", ingestOptions{
			Product: "vmware", Version: "9.0",
		}, "manual ingest requires --impl, --spec"},
		{"no-wait + dry-run", ingestOptions{
			Catalog: "vmware/9.0", DryRun: true, NoWait: true,
		}, "--no-wait cannot be combined with --dry-run"},
		{"no-wait alone is fine", ingestOptions{
			Catalog: "vmware/9.0", NoWait: true,
		}, ""},
	}
	for _, c := range cases {
		err := validateIngestMode(c.opts)
		if c.wantErr == "" {
			if err != nil {
				t.Errorf("%s: want nil, got %v", c.name, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), c.wantErr) {
			t.Errorf("%s: want error containing %q, got %v", c.name, c.wantErr, err)
		}
	}
}

// TestBuildIngestRequestSpecInfoVersionsCompatible — the manual-mode
// --spec-info-versions-compatible flag (T1 #1646) threads onto the
// request body so the backend's spec-vs-label cross-check can widen
// against the operator-declared band; catalog mode and an unset flag
// both leave the field nil.
func TestBuildIngestRequestSpecInfoVersionsCompatible(t *testing.T) {
	t.Run("manual sets the band", func(t *testing.T) {
		body, err := buildIngestRequest(ingestOptions{
			Product:    "vcf-logs",
			Version:    "9.0",
			ImplID:     "vrli-rest",
			Specs:      []string{"https://specs.example.test/vrli.yaml"},
			Compatible: []string{"2.x", ">=2,<3"},
		})
		if err != nil {
			t.Fatalf("buildIngestRequest: %v", err)
		}
		if body.SpecInfoVersionsCompatible == nil {
			t.Fatal("SpecInfoVersionsCompatible: want populated, got nil")
		}
		got := *body.SpecInfoVersionsCompatible
		if len(got) != 2 || got[0] != "2.x" || got[1] != ">=2,<3" {
			t.Errorf("SpecInfoVersionsCompatible = %v, want [2.x >=2,<3]", got)
		}
	})

	t.Run("unset leaves the field nil", func(t *testing.T) {
		body, err := buildIngestRequest(ingestOptions{
			Product: "vcf-logs",
			Version: "9.0",
			ImplID:  "vrli-rest",
			Specs:   []string{"https://specs.example.test/vrli.yaml"},
		})
		if err != nil {
			t.Fatalf("buildIngestRequest: %v", err)
		}
		if body.SpecInfoVersionsCompatible != nil {
			t.Errorf("SpecInfoVersionsCompatible = %v, want nil", *body.SpecInfoVersionsCompatible)
		}
	})

	t.Run("catalog mode never carries the band", func(t *testing.T) {
		// Catalog mode returns before the manual-mode tail, so a
		// stray --spec-info-versions-compatible value is dropped on
		// the wire; the backend validator rejects the combination
		// up front anyway (catalog_entry_conflict).
		body, err := buildIngestRequest(ingestOptions{
			Catalog:    "vmware/9.0",
			Compatible: []string{"2.x"},
		})
		if err != nil {
			t.Fatalf("buildIngestRequest: %v", err)
		}
		if body.SpecInfoVersionsCompatible != nil {
			t.Errorf("SpecInfoVersionsCompatible = %v, want nil", *body.SpecInfoVersionsCompatible)
		}
	})
}

// ---------- renderers ----------

// TestPrintIngestSummaryDryRun — dry-run header + counts; no
// "Connector is in review_status=staged" trailer because that
// only applies after a real ingest. The canonical
// IngestionResultModel ships only the aggregate inserted/updated/
// skipped counts plus the two boolean flags — no per-spec
// breakdown, no embeddings split. Total is derived client-side.
func TestPrintIngestSummaryDryRun(t *testing.T) {
	r := &api.IngestResponse{
		Ingestion: api.IngestionResultModel{
			ConnectorId:   "vmware-rest-9.0",
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
// matches v0.6.0 verbatim.
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
			r := &api.IngestResponse{
				Ingestion: api.IngestionResultModel{
					ConnectorId:         "vmware-rest-9.0",
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
// shape against the backend's parse_connector_id convention.
func TestIngestSummaryHeadingFromConnectorID(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
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
	r := &api.IngestResponse{
		Ingestion: api.IngestionResultModel{
			ConnectorId:         "vmware-rest-9.0",
			InsertedCount:       961,
			ConnectorRegistered: true,
			OperationsGrouped:   true,
		},
		Grouping: &api.GroupingResultModel{
			ConnectorId:        "vmware-rest-9.0",
			GroupsCreated:      9,
			OperationsAssigned: 961,
			LlmCallCount:       20,
			LlmDurationMs:      4321,
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
	var got api.IngestResponse
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode IngestResponse: %v", err)
	}
	if got.Ingestion.ConnectorId != "vmware-rest-9.0" {
		t.Errorf("ingestion.connector_id: got %q", got.Ingestion.ConnectorId)
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
	if got.Grouping.LlmCallCount != 20 {
		t.Errorf("llm_call_count: got %d, want 20", got.Grouping.LlmCallCount)
	}
	// Generated client decodes llm_duration_ms as float32 — sub-ms
	// precision still survives at this magnitude.
	if got.Grouping.LlmDurationMs != 4321.5 {
		t.Errorf("llm_duration_ms: got %v, want 4321.5", got.Grouping.LlmDurationMs)
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
	var got api.IngestResponse
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
	printListTable(&buf, "staged", &connectorListEnvelope{})
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
	r := &connectorListEnvelope{
		Connectors: []listEntry{
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

// TestListEntryDecodesCanonical pins the wire shape: the canonical
// ConnectorListItem (operations/ingest/api_schemas.py) ships the
// per-status group counts, the enabled-vs-total op split (#1636),
// the dispatchability state (#773), the next_step hint (#1133) and
// no top-level review_status. The list endpoint deliberately returns
// `dict[str, list[dict[str, object]]]` (no `response_model`), so we
// keep a package-private listEntry struct for the decode. The strict
// decoder makes the fixture⇄struct direction fail loudly: a canonical
// key without a matching listEntry field is exactly the drift class
// that silently dropped three backend fields from `--json` (#1645) —
// plain json.Unmarshal ignores unknown keys, so three backend
// additions sailed past the previous shape of this test.
func TestListEntryDecodesCanonical(t *testing.T) {
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
		"operation_count": 961,
		"enabled_operation_count": 14,
		"state": "ingested",
		"next_step": null
	}`)
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var got listEntry
	if err := dec.Decode(&got); err != nil {
		t.Fatalf("decode listEntry: %v", err)
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
	if got.OperationCount != 961 || got.EnabledOperationCount != 14 {
		t.Errorf("op rollup: want 961 total / 14 enabled; got %+v", got)
	}
	if got.State != "ingested" {
		t.Errorf("state: got %q", got.State)
	}
	if got.NextStep != nil {
		t.Errorf("next_step must decode JSON null to nil on an ingested row; got %+v", got.NextStep)
	}
}

// TestListEntryJSONRoundTrip pins the machine surface end to end:
// decode a canonical row, re-marshal through output.PrintJSON — the
// exact `--json` emit path (`runList` marshals the decoded envelope,
// not the raw response body) — and assert the fields #773 / #1133 /
// #1636 added survive the round trip. This is the regression class
// #1645 closes: any ConnectorListItem field the struct doesn't
// mirror silently vanishes from machine-readable output.
func TestListEntryJSONRoundTrip(t *testing.T) {
	t.Run("registered row carries next_step", func(t *testing.T) {
		raw := []byte(`{
			"connector_id": "nsx-rest-4.2",
			"product": "nsx",
			"version": "4.2",
			"impl_id": "nsx-rest",
			"tenant_id": null,
			"group_count": 0,
			"staged_group_count": 0,
			"enabled_group_count": 0,
			"disabled_group_count": 0,
			"operation_count": 0,
			"enabled_operation_count": 0,
			"state": "registered",
			"next_step": {
				"verb": "meho connector ingest --catalog nsx/4.2",
				"rationale": "registered without descriptor rows; ingest the catalog spec to make it dispatchable"
			}
		}`)
		var got listEntry
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatalf("decode listEntry: %v", err)
		}
		if got.State != "registered" {
			t.Errorf("state: got %q", got.State)
		}
		if got.NextStep == nil {
			t.Fatal("next_step must decode to a non-nil pointer on a registered row")
		}
		if got.NextStep.Verb != "meho connector ingest --catalog nsx/4.2" {
			t.Errorf("next_step.verb: got %q", got.NextStep.Verb)
		}
		if got.NextStep.Rationale == "" {
			t.Error("next_step.rationale must survive decode")
		}
		var buf bytes.Buffer
		if err := output.PrintJSON(&buf, &connectorListEnvelope{Connectors: []listEntry{got}}); err != nil {
			t.Fatalf("PrintJSON: %v", err)
		}
		out := buf.String()
		for _, want := range []string{
			`"state": "registered"`,
			`"verb": "meho connector ingest --catalog nsx/4.2"`,
			`"rationale": "registered without descriptor rows; ingest the catalog spec to make it dispatchable"`,
			`"enabled_operation_count": 0`,
		} {
			if !strings.Contains(out, want) {
				t.Errorf("--json re-marshal missing %s in:\n%s", want, out)
			}
		}
	})
	t.Run("ingested row re-marshals next_step as null", func(t *testing.T) {
		raw := []byte(`{
			"connector_id": "vault-1.x",
			"product": "vault",
			"version": "1.x",
			"impl_id": "vault",
			"tenant_id": null,
			"group_count": 2,
			"staged_group_count": 0,
			"enabled_group_count": 2,
			"disabled_group_count": 0,
			"operation_count": 7,
			"enabled_operation_count": 7,
			"state": "ingested",
			"next_step": null
		}`)
		var got listEntry
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatalf("decode listEntry: %v", err)
		}
		if got.NextStep != nil {
			t.Fatalf("next_step must be nil on an ingested row; got %+v", got.NextStep)
		}
		var buf bytes.Buffer
		if err := output.PrintJSON(&buf, &connectorListEnvelope{Connectors: []listEntry{got}}); err != nil {
			t.Fatalf("PrintJSON: %v", err)
		}
		out := buf.String()
		if !strings.Contains(out, `"next_step": null`) {
			t.Errorf("nil next_step must re-marshal as JSON null, not be dropped or rendered as {}; got:\n%s", out)
		}
		if !strings.Contains(out, `"state": "ingested"`) {
			t.Errorf("state must survive the --json round trip; got:\n%s", out)
		}
	})
}

// TestPrintReviewTableHappyPath — review render shows groups + ops
// + per-group review_status flags. The connector-wide rollup label
// is derived from the per-group review_status counts (same shape as
// `meho connector list`).
func TestPrintReviewTableHappyPath(t *testing.T) {
	summary := "List vSphere clusters"
	r := &api.ConnectorReviewPayload{
		ConnectorId:  "vmware-rest-9.0",
		Product:      "vmware",
		Version:      "9.0",
		ImplId:       "vmware-rest",
		TotalOpCount: 2,
		Groups: []api.ConnectorReviewGroup{
			{
				GroupKey:     "cluster",
				Name:         "Cluster",
				WhenToUse:    "Use for vSphere cluster lifecycle ops.",
				ReviewStatus: "staged",
				OpCount:      2,
				Ops: []api.ConnectorReviewOp{
					{OpId: "GET:/api/vcenter/cluster", Summary: &summary, SafetyLevel: "safe", IsEnabled: false},
					{OpId: "DELETE:/api/vcenter/cluster/{id}", SafetyLevel: "dangerous", RequiresApproval: true, IsEnabled: false},
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
	r := &api.ConnectorReviewPayload{ConnectorId: "k8s-1.x", Groups: nil}
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
	var got api.ConnectorReviewPayload
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
	if op.OpId != "GET:/api/vcenter/cluster" {
		t.Errorf("op_id: got %q", op.OpId)
	}
	if len(op.Tags) != 2 || op.Tags[0] != "cluster" {
		t.Errorf("op tags: got %v", op.Tags)
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

// ---------- httpResponseError ----------

// TestHTTPResponseErrorString — Error() format pins the renderer's
// input shape.
func TestHTTPResponseErrorString(t *testing.T) {
	he := &httpResponseError{statusCode: 403, body: []byte("forbidden")}
	if he.Error() != "HTTP 403: forbidden" {
		t.Fatalf("httpResponseError format: got %q", he.Error())
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
			var req api.IngestRequest
			if err := json.Unmarshal(body, &req); err != nil {
				t.Errorf("decode IngestRequest body: %v", err)
				w.WriteHeader(400)
				return
			}
			if req.Product == nil || req.Version == nil || req.ImplId == nil ||
				*req.Product != "vmware" || *req.Version != "9.0" || *req.ImplId != "vmware-rest" {
				t.Errorf("unexpected triple: %+v", req)
			}
			if req.Specs == nil || len(*req.Specs) != 1 || (*req.Specs)[0].Uri != "file:///abs/vcenter.yaml" {
				t.Errorf("unexpected specs: %+v", req.Specs)
			}
			resp := api.IngestResponse{
				Ingestion: api.IngestionResultModel{
					ConnectorId:         "vmware-rest-9.0",
					InsertedCount:       961,
					ConnectorRegistered: true,
					OperationsGrouped:   true,
				},
				Grouping: &api.GroupingResultModel{
					ConnectorId:        "vmware-rest-9.0",
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
	specs := []api.SpecSource{{Uri: "file:///abs/vcenter.yaml"}}
	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	got, err := postIngest(context.Background(), authed, api.IngestRequest{
		Product: &product, Version: &version, ImplId: &implID,
		Specs: &specs,
	})
	if err != nil {
		t.Fatalf("postIngest: %v", err)
	}
	if got.job != nil {
		t.Fatalf("sync 200 answer must not produce a job handle: %+v", got.job)
	}
	if got.sync == nil {
		t.Fatal("sync 200 answer must populate the IngestResponse")
	}
	if got.sync.Ingestion.ConnectorId != "vmware-rest-9.0" || got.sync.Ingestion.InsertedCount != 961 {
		t.Fatalf("unexpected ingest result: %+v", got.sync)
	}
	if got.sync.Grouping == nil || got.sync.Grouping.GroupsCreated != 9 {
		t.Fatalf("unexpected grouping: %+v", got.sync.Grouping)
	}
}

// TestRunIngestCatalogModePostsCatalogEntry pins the post-#1150
// contract: catalog mode POSTs `{"catalog_entry": "..."}` directly
// without the explicit quadruple. The backplane resolves the entry
// server-side, so the CLI no longer pre-fetches the catalog.
func TestRunIngestCatalogModePostsCatalogEntry(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			rawBody, _ = io.ReadAll(r.Body)
			writeJSON(t, w, 200, api.IngestResponse{
				Ingestion: api.IngestionResultModel{ConnectorId: "vmware-rest-9.0", InsertedCount: 961},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest catalog mode: %v", err)
	}

	// Decode into a generic map to inspect the exact wire shape. The
	// CLI must NOT post product/version/impl_id/specs in catalog mode
	// — the backend's mutual-exclusivity validator would reject the
	// body. Since the generated `api.IngestRequest` tags lack
	// `omitempty`, the JSON marshal still emits the unset pointer
	// fields as JSON `null`; the validator interprets null as
	// "field unset", so the wire shape stays catalog-only without
	// regressing the round-trip.
	var posted map[string]any
	if err := json.Unmarshal(rawBody, &posted); err != nil {
		t.Fatalf("decode posted body: %v", err)
	}
	if posted["catalog_entry"] != "vmware/9.0" {
		t.Errorf("catalog_entry not posted: %+v", posted)
	}
	for _, banned := range []string{"product", "version", "impl_id"} {
		// Catalog mode must leave the quadruple slots null, never
		// populate them with strings — the route's mutual-exclusivity
		// validator treats any non-null value as "quadruple shape".
		if v, present := posted[banned]; present && v != nil {
			t.Errorf("catalog mode must not populate %q (would conflict): got %v", banned, v)
		}
	}
	// specs has `omitempty` on the generated tag (it's a slice of
	// struct), so its absence is the right check.
	if v, present := posted["specs"]; present && v != nil {
		t.Errorf("catalog mode must not populate specs: got %v", v)
	}
}

// TestRunIngestCatalogModeRendersResolvedTriple pins the operator-
// visible summary heading in catalog mode. The CLI posts
// `{"catalog_entry": "vmware/9.0"}` without the explicit quadruple;
// the backplane resolves the entry and returns a connector_id
// (`vmware-rest-9.0`) that round-trips through parse_connector_id
// to the resolved triple. The heading derives from that response
// value.
func TestRunIngestCatalogModeRendersResolvedTriple(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, api.IngestResponse{
				Ingestion: api.IngestionResultModel{
					ConnectorId:   "vmware-rest-9.0",
					InsertedCount: 961,
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		DryRun:            true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest catalog mode: %v", err)
	}

	out := stdout.String()
	if !strings.Contains(out, "ingest vmware/9.0/vmware-rest") {
		t.Errorf("catalog-mode summary missing resolved triple `vmware/9.0/vmware-rest` in:\n%s", out)
	}
	if strings.Contains(out, "ingest // —") {
		t.Errorf("catalog-mode summary contains bare `//` heading (B1 regression); got:\n%s", out)
	}
}

// TestRunIngestManualModePostsQuadruple pins the parallel contract:
// manual mode POSTs the explicit quadruple, not a catalog_entry.
// Regression guard for the historical --product/--version/--impl/--spec
// form.
func TestRunIngestManualModePostsQuadruple(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			rawBody, _ = io.ReadAll(r.Body)
			writeJSON(t, w, 200, api.IngestResponse{
				Ingestion: api.IngestionResultModel{ConnectorId: "test-1.0", InsertedCount: 2},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Product: "test", Version: "1.0", ImplID: "test-impl",
		Specs:             []string{"https://example.test/spec.yaml"},
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest manual mode: %v", err)
	}

	var posted map[string]any
	if err := json.Unmarshal(rawBody, &posted); err != nil {
		t.Fatalf("decode posted body: %v", err)
	}
	// catalog_entry tag has no `omitempty`, so the marshal still
	// emits the field as `null` when unset; the route's validator
	// reads null as "absent". The right contract is "no string
	// value present", not "key absent".
	if v, present := posted["catalog_entry"]; present && v != nil {
		t.Errorf("manual mode must not populate catalog_entry: got %v", v)
	}
	if posted["product"] != "test" || posted["version"] != "1.0" || posted["impl_id"] != "test-impl" {
		t.Errorf("manual mode posted quadruple wrong: %+v", posted)
	}
}

// ---------- async-202 ingest (G0.22-T4 #1609) ----------

// asyncIngestJobID is the fixed job id the async-202 mock fixtures
// use; the GET-route key embeds it because mockBackplane routes on
// the literal path.
var asyncIngestJobID = uuid.MustParse("0c4b7e8f-1111-2222-3333-444455556666")

// asyncIngestHandle builds the 202 envelope the #1303 backplane
// returns for a default (async) ingest.
func asyncIngestHandle() api.IngestJobHandle {
	return api.IngestJobHandle{
		JobId:   asyncIngestJobID,
		Status:  api.IngestJobHandleStatusRunning,
		PollUrl: "/api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(),
	}
}

// TestPostIngest202ReturnsJobHandle pins the half of the #1609 fix
// that used to be the bug: a 202 + IngestJobHandle is a *success*
// shape on the postIngest surface, never an *httpResponseError
// (the pre-fix code rendered it as a fatal unexpected_response
// after the pipeline had already started server-side).
func TestPostIngest202ReturnsJobHandle(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	catalog := "vmware/9.0"
	got, err := postIngest(context.Background(), authed, api.IngestRequest{CatalogEntry: &catalog})
	if err != nil {
		t.Fatalf("postIngest on a 202 must not error (double-ingest bait): %v", err)
	}
	if got.sync != nil {
		t.Fatalf("202 answer must not populate the sync IngestResponse: %+v", got.sync)
	}
	if got.job == nil {
		t.Fatal("202 answer must populate the job handle")
	}
	if got.job.JobId != asyncIngestJobID || got.job.PollUrl != asyncIngestHandle().PollUrl {
		t.Fatalf("unexpected handle: %+v", got.job)
	}
}

// TestPostIngest202WithoutBodyErrors covers the decode guard: a 202
// whose body didn't decode against IngestJobHandle (wrong content
// type) is a contract violation, not a silent success.
func TestPostIngest202WithoutBodyErrors(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/plain")
			w.WriteHeader(202)
			_, _ = w.Write([]byte("accepted"))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	catalog := "vmware/9.0"
	_, err = postIngest(context.Background(), authed, api.IngestRequest{CatalogEntry: &catalog})
	if err == nil || !strings.Contains(err.Error(), "202 Accepted but no JSON body") {
		t.Fatalf("want decode-guard error, got %v", err)
	}
}

// TestRunIngestAsyncPollsToSuccess pins the default async flow
// end-to-end: POST → 202 + handle, poll → running, poll →
// succeeded, then the CLI renders the exact summary the sync path
// renders (assembled from the job's ingestion + grouping clusters)
// and exits 0. Also pins single-submission: exactly one POST ever
// leaves the CLI (the double-ingest regression guard).
func TestRunIngestAsyncPollsToSuccess(t *testing.T) {
	var postCalls, pollCalls atomic.Int32
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			postCalls.Add(1)
			writeJSON(t, w, 202, asyncIngestHandle())
		},
		"GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(): func(w http.ResponseWriter, _ *http.Request) {
			if pollCalls.Add(1) == 1 {
				writeJSON(t, w, 200, api.IngestJobStatusResponse{
					JobId:  asyncIngestJobID,
					Status: api.IngestJobStatusResponseStatusRunning,
				})
				return
			}
			writeJSON(t, w, 200, api.IngestJobStatusResponse{
				JobId:  asyncIngestJobID,
				Status: api.IngestJobStatusResponseStatusSucceeded,
				Ingestion: &api.IngestionResultModel{
					ConnectorId:         "vmware-rest-9.0",
					InsertedCount:       961,
					ConnectorRegistered: true,
					OperationsGrouped:   true,
				},
				Grouping: &api.GroupingResultModel{
					ConnectorId:        "vmware-rest-9.0",
					GroupsCreated:      9,
					OperationsAssigned: 961,
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		BackplaneOverride: srv.URL,
		pollInterval:      time.Millisecond,
	}); err != nil {
		t.Fatalf("runIngest async: %v", err)
	}
	if got := postCalls.Load(); got != 1 {
		t.Errorf("expected exactly 1 ingest POST (re-submitting double-ingests), got %d", got)
	}
	if got := pollCalls.Load(); got != 2 {
		t.Errorf("expected 2 job polls (running, then succeeded), got %d", got)
	}
	out := stdout.String()
	for _, want := range []string{
		"ingest vmware/9.0/vmware-rest — connector_id=vmware-rest-9.0",
		"operations: 961 total (961 inserted / 0 updated / 0 skipped)",
		"grouping: 9 groups, 961 ops assigned",
		"meho connector review vmware-rest-9.0",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q:\n%s", want, out)
		}
	}
	errOut := stderr.String()
	if !strings.Contains(errOut, "ingest accepted (HTTP 202)") ||
		!strings.Contains(errOut, asyncIngestJobID.String()) {
		t.Errorf("stderr missing the 202 progress notice:\n%s", errOut)
	}
	if strings.Contains(out, "202") {
		t.Errorf("stdout must stay reserved for the result (progress goes to stderr):\n%s", out)
	}
}

// TestRunIngestAsyncNoWaitPrintsHandle pins the detached mode: a
// 202 + --no-wait exits 0 with the handle and never polls (the
// mock registers no GET route, so any poll fails the test via the
// unhandled-route check in mockBackplane).
func TestRunIngestAsyncNoWaitPrintsHandle(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		NoWait:            true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest --no-wait on 202 must exit clean: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"job_id=" + asyncIngestJobID.String(),
		"poll: GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(),
		"re-running ingest would start a second job",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q:\n%s", want, out)
		}
	}
}

// TestRunIngestAsyncNoWaitJSONEmitsHandle pins the machine shape of
// the detached mode: stdout is exactly the IngestJobHandle document.
func TestRunIngestAsyncNoWaitJSONEmitsHandle(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		NoWait:            true,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest --no-wait --json: %v", err)
	}
	var handle api.IngestJobHandle
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &handle); err != nil {
		t.Fatalf("stdout is not a single IngestJobHandle document: %v\n%s", err, stdout.String())
	}
	if handle.JobId != asyncIngestJobID {
		t.Errorf("handle round-trip lost the job id: %+v", handle)
	}
}

// TestRunIngestAsyncJSONStableSuccessShape pins the script contract:
// `--json` (wait mode) emits the same IngestResponse shape on the
// async path that the legacy sync path emits, so jq consumers see
// one stable success document regardless of how the backplane ran
// the pipeline.
func TestRunIngestAsyncJSONStableSuccessShape(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
		"GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(): func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, api.IngestJobStatusResponse{
				JobId:  asyncIngestJobID,
				Status: api.IngestJobStatusResponseStatusSucceeded,
				Ingestion: &api.IngestionResultModel{
					ConnectorId:   "vmware-rest-9.0",
					InsertedCount: 961,
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&bytes.Buffer{})

	if err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
		pollInterval:      time.Millisecond,
	}); err != nil {
		t.Fatalf("runIngest async --json: %v", err)
	}
	var resp api.IngestResponse
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &resp); err != nil {
		t.Fatalf("stdout is not a single IngestResponse document: %v\n%s", err, stdout.String())
	}
	if resp.Ingestion.ConnectorId != "vmware-rest-9.0" || resp.Ingestion.InsertedCount != 961 {
		t.Errorf("IngestResponse round-trip wrong: %+v", resp)
	}
}

// TestRunIngestAsyncJobFailure pins the failed-terminal contract:
// the job's error_class + capped error render as
// unexpected_response (exit 4) with the job id, mirroring where the
// sync path's pipeline failures land.
func TestRunIngestAsyncJobFailure(t *testing.T) {
	errClass := "VersionMismatchError"
	errMsg := "spec info.version 8.0 does not match requested 9.0"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
		"GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(): func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, api.IngestJobStatusResponse{
				JobId:      asyncIngestJobID,
				Status:     api.IngestJobStatusResponseStatusFailed,
				ErrorClass: &errClass,
				Error:      &errMsg,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stderr bytes.Buffer
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&stderr)

	err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		BackplaneOverride: srv.URL,
		pollInterval:      time.Millisecond,
	})
	if err == nil {
		t.Fatal("failed job must exit non-zero")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) || coder.ExitCode() != output.ExitUnexpected {
		t.Fatalf("want exit %d (unexpected_response), got %v / %T", output.ExitUnexpected, err, err)
	}
	errOut := stderr.String()
	for _, want := range []string{asyncIngestJobID.String(), errClass, errMsg} {
		if !strings.Contains(errOut, want) {
			t.Errorf("stderr missing %q:\n%s", want, errOut)
		}
	}
}

// TestRunIngestAsyncPollLost404 pins the pod-restart story: the
// poll 404s (registry is process-local), the CLI exits non-zero
// with the check-before-re-running guidance, and crucially never
// re-POSTs the ingest.
func TestRunIngestAsyncPollLost404(t *testing.T) {
	var postCalls atomic.Int32
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			postCalls.Add(1)
			writeJSON(t, w, 202, asyncIngestHandle())
		},
		"GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(): func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 404, map[string]string{"detail": "ingest_job_not_found"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stderr bytes.Buffer
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&stderr)

	err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		BackplaneOverride: srv.URL,
		pollInterval:      time.Millisecond,
	})
	if err == nil {
		t.Fatal("lost job must exit non-zero")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) || coder.ExitCode() != output.ExitUnexpected {
		t.Fatalf("want exit %d (unexpected_response), got %v", output.ExitUnexpected, err)
	}
	if got := postCalls.Load(); got != 1 {
		t.Errorf("expected exactly 1 ingest POST even when the poll dies, got %d", got)
	}
	errOut := stderr.String()
	for _, want := range []string{"no longer tracked", "meho connector list", "before re-running ingest"} {
		if !strings.Contains(errOut, want) {
			t.Errorf("stderr missing %q:\n%s", want, errOut)
		}
	}
}

// TestRunIngestAsyncUndocumentedStatus pins forward-compat
// fail-loud: a terminal status outside running/succeeded/failed is
// an error, never a silent success.
func TestRunIngestAsyncUndocumentedStatus(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 202, asyncIngestHandle())
		},
		"GET /api/v1/connectors/ingest/jobs/" + asyncIngestJobID.String(): func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, api.IngestJobStatusResponse{
				JobId:  asyncIngestJobID,
				Status: api.IngestJobStatusResponseStatus("paused"),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stderr bytes.Buffer
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&stderr)

	err := runIngest(cmd, ingestOptions{
		Catalog:           "vmware/9.0",
		BackplaneOverride: srv.URL,
		pollInterval:      time.Millisecond,
	})
	if err == nil {
		t.Fatal("undocumented job status must exit non-zero")
	}
	if !strings.Contains(stderr.String(), `undocumented status "paused"`) {
		t.Errorf("stderr missing the undocumented-status detail:\n%s", stderr.String())
	}
}

// TestGetListWithMockServer — validates the status query param
// shape and the list response decode through the typed-client surface.
func TestGetListWithMockServer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("status") != "staged" {
				t.Errorf("expected status=staged; got %q", r.URL.Query().Get("status"))
			}
			writeJSON(t, w, 200, connectorListEnvelope{
				Connectors: []listEntry{
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
			writeJSON(t, w, 200, connectorListEnvelope{})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := getList(context.Background(), srv.URL, "all"); err != nil {
		t.Fatalf("getList all: %v", err)
	}
}

// TestGetCatalogDecodesEntries — round-trips the typed
// CatalogListResponse through the generated client decoder. Pins
// the `ConnectorSpecEntry` field-name shape (`ImplId`, not
// `ImplID`) and the nullable `Upstream` / `SpecInfoVersion`
// passthrough.
func TestGetCatalogDecodesEntries(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/catalog": func(w http.ResponseWriter, _ *http.Request) {
			specVer := "9.0.1"
			upstream := []string{"https://example.test/vcenter.yaml"}
			writeJSON(t, w, 200, api.CatalogListResponse{Catalog: []api.ConnectorSpecEntry{
				{
					Product: "vmware", Version: "9.0", ImplId: "vmware-rest",
					RequiresConnectorClass: "VmwareRestConnector",
					Upstream:               &upstream,
					SpecInfoVersion:        &specVer,
				},
				{
					Product: "vault", Version: "1.x", ImplId: "vault",
					RequiresConnectorClass: "VaultConnector",
					Upstream:               nil,
					SpecInfoVersion:        nil,
				},
			}})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	got, err := getCatalog(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("getCatalog: %v", err)
	}
	if len(got.Catalog) != 2 {
		t.Fatalf("want 2 entries, got %d", len(got.Catalog))
	}
	if got.Catalog[1].Upstream != nil {
		t.Errorf("typed entry upstream should decode to nil, got %+v", got.Catalog[1].Upstream)
	}
	if got.Catalog[1].SpecInfoVersion != nil {
		t.Errorf("null spec_info_version should decode to nil pointer")
	}
	if got.Catalog[0].SpecInfoVersion == nil || *got.Catalog[0].SpecInfoVersion != "9.0.1" {
		t.Errorf("spec_info_version decode wrong: %+v", got.Catalog[0].SpecInfoVersion)
	}
}

// TestPrintCatalogTableHappyPath — happy-path table render with one
// registered and one unregistered entry; the cross-reference column
// reflects map membership.
func TestPrintCatalogTableHappyPath(t *testing.T) {
	specVer := "9.0.1"
	upstream := []string{"https://example.test/x.yaml"}
	notes := "generic"
	notesV := "typed"
	var buf bytes.Buffer
	registered := map[string]bool{tripleKey("vmware", "9.0", "vmware-rest"): true}
	printCatalogTable(&buf, &api.CatalogListResponse{Catalog: []api.ConnectorSpecEntry{
		{Product: "vmware", Version: "9.0", ImplId: "vmware-rest",
			RequiresConnectorClass: "VmwareRestConnector",
			Upstream:               &upstream,
			SpecInfoVersion:        &specVer, Notes: &notes},
		{Product: "vault", Version: "1.x", ImplId: "vault",
			RequiresConnectorClass: "VaultConnector", Notes: &notesV},
	}}, registered)
	out := buf.String()
	for _, want := range []string{"vmware/9.0", "VmwareRestConnector", "9.0.1", "yes", "vault/1.x", "no"} {
		if !strings.Contains(out, want) {
			t.Errorf("catalog table missing %q\n%s", want, out)
		}
	}
}

// TestPrintCatalogTableUnknownRegistration — nil registered map →
// registration column renders "?".
func TestPrintCatalogTableUnknownRegistration(t *testing.T) {
	upstream := []string{"https://example.test/x.yaml"}
	var buf bytes.Buffer
	printCatalogTable(&buf, &api.CatalogListResponse{Catalog: []api.ConnectorSpecEntry{
		{Product: "vmware", Version: "9.0", ImplId: "vmware-rest",
			RequiresConnectorClass: "VmwareRestConnector",
			Upstream:               &upstream},
	}}, nil)
	if !strings.Contains(buf.String(), "?") {
		t.Errorf("nil registration map should render ?\n%s", buf.String())
	}
}

// TestPrintCatalogTableEmpty — zero entries renders the count line.
func TestPrintCatalogTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printCatalogTable(&buf, &api.CatalogListResponse{}, map[string]bool{})
	if !strings.Contains(buf.String(), "0 catalog entries") {
		t.Errorf("empty catalog render wrong: %q", buf.String())
	}
}

// TestRegisteredTriplesFromMockServer — exercises the cross-
// reference helper end-to-end against a mocked list endpoint.
func TestRegisteredTriplesFromMockServer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, connectorListEnvelope{Connectors: []listEntry{
				{ConnectorID: "vmware-rest-9.0", Product: "vmware", Version: "9.0", ImplID: "vmware-rest"},
			}})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	set := registeredTriples(context.Background(), srv.URL)
	if !set[tripleKey("vmware", "9.0", "vmware-rest")] {
		t.Fatalf("expected vmware triple registered; got %+v", set)
	}
	if set[tripleKey("nsx", "4.2", "nsx-rest")] {
		t.Fatalf("nsx should not be registered")
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
			var got api.EditGroupBody
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
	if err := patchGroup(context.Background(), srv.URL, "vmware-rest-9.0", "cluster", api.EditGroupBody{WhenToUse: &whenToUse}); err != nil {
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
	printEditGroupResult(&buf, "vmware-rest-9.0", "cluster", api.EditGroupBody{
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

// TestPatchOpEscapesOpID — colons and slashes in op_id must survive
// the URL path. The generated client URL-escapes the path parameter,
// so the colons / slashes round-trip back to the canonical
// `METHOD:/path` form on the server side.
func TestPatchOpEscapesOpID(t *testing.T) {
	called := false
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			// Catch-all: confirm the escaped path segment carrying
			// the op_id round-trips back to "GET:/api/vcenter/cluster".
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
			// Canonical response since G0.23-T4 (#1630): 200 with an
			// EditOpResponse envelope (empty warnings on the clean path).
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"warnings": []}`))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	safety := api.Dangerous
	resp, err := patchOp(context.Background(), srv.URL, "vmware-rest-9.0", "GET:/api/vcenter/cluster", api.EditOpBody{SafetyLevel: &safety})
	if err != nil {
		t.Fatalf("patchOp: %v", err)
	}
	if len(resp.Warnings) != 0 {
		t.Errorf("expected no warnings on the clean path; got %+v", resp.Warnings)
	}
	if !called {
		t.Fatalf("mock handler not invoked")
	}
}

// TestPatchOpSurfacesEnableTimeWarnings — the 200 EditOpResponse's
// `warnings` field (G0.23-T4 #1630) must round-trip through patchOp so
// runEditOp can render the unreplaced_auto_shim advisory on stderr.
func TestPatchOpSurfacesEnableTimeWarnings(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "PATCH" {
				t.Errorf("expected PATCH; got %s", r.Method)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"warnings": [{
				"code": "unreplaced_auto_shim",
				"connector_class": "AutoShim_acme_1_2_acme_rest",
				"message": "register the per-product Connector subclass"
			}]}`))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	enabled := true
	resp, err := patchOp(context.Background(), srv.URL, "acme-rest-1.2", "GET:/api/v1/group-0/0", api.EditOpBody{IsEnabled: &enabled})
	if err != nil {
		t.Fatalf("patchOp: %v", err)
	}
	if len(resp.Warnings) != 1 {
		t.Fatalf("expected 1 warning; got %+v", resp.Warnings)
	}
	warning := resp.Warnings[0]
	if warning.Code != "unreplaced_auto_shim" {
		t.Errorf("warning code: got %q", warning.Code)
	}
	if warning.ConnectorClass != "AutoShim_acme_1_2_acme_rest" {
		t.Errorf("warning connector_class: got %q", warning.ConnectorClass)
	}
}

// TestPrintEditOpWarnings — the stderr rendering names the stable code
// so operators (and log scrapers) can grep `unreplaced_auto_shim`; the
// clean path prints nothing.
func TestPrintEditOpWarnings(t *testing.T) {
	var buf bytes.Buffer
	printEditOpWarnings(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("clean path must print nothing; got %q", buf.String())
	}

	printEditOpWarnings(&buf, []api.EditOpWarning{{
		Code:           "unreplaced_auto_shim",
		ConnectorClass: "AutoShim_acme_1_2_acme_rest",
		Message:        "register the per-product Connector subclass",
	}})
	out := buf.String()
	for _, want := range []string{
		"warning (unreplaced_auto_shim):",
		"register the per-product Connector subclass",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("warning render missing %q in:\n%s", want, out)
		}
	}
}

// TestPostTransitionEnable204 — pins the canonical enable / disable
// wire shape. T6 returns HTTP 204 No Content with no body; the typed
// client surfaces 204 as a success envelope with empty Body.
func TestPostTransitionEnable204(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/vmware-rest-9.0/enable": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if err := postTransition(context.Background(), srv.URL, verbEnable, "vmware-rest-9.0"); err != nil {
		t.Fatalf("postTransition enable: %v", err)
	}
}

// TestPostTransitionDisable204 — mirror of TestPostTransitionEnable204
// for the disable route.
func TestPostTransitionDisable204(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/vmware-rest-9.0/disable": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if err := postTransition(context.Background(), srv.URL, verbDisable, "vmware-rest-9.0"); err != nil {
		t.Fatalf("postTransition disable: %v", err)
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
		"GET /api/v1/connectors": func(w http.ResponseWriter, _ *http.Request) {
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
// *httpResponseError with the right status code; renderHTTPStatus
// maps to insufficient_role at the verb edge.
func TestHTTPErrorClassification403(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(403)
			_, _ = w.Write([]byte("tenant_admin required"))
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	product := "x"
	version := "y"
	implID := "z"
	specs := []api.SpecSource{{Uri: "file:///a.yaml"}}
	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	_, err = postIngest(context.Background(), authed, api.IngestRequest{
		Product: &product, Version: &version, ImplId: &implID,
		Specs: &specs,
	})
	if err == nil {
		t.Fatalf("expected 403 error; got nil")
	}
	var he *httpResponseError
	if !errors.As(err, &he) || he.statusCode != 403 {
		t.Fatalf("expected *httpResponseError 403; got %T %v", err, err)
	}
}

// TestRetryOn401FastPath — when the first call returns a non-401
// status the wrapper skips the retry entirely. This pins the
// hot-path latency contract: every successful connector verb pays
// at most one round-trip.
func TestRetryOn401FastPath(t *testing.T) {
	calls := 0
	type fakeResp struct{ status int }
	srv := mockBackplane(t, map[string]mockHandler{}) // unused; we exercise the wrapper directly
	defer srv.Close()
	primeToken(t, srv.URL)
	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	resp, err := retryOn401(context.Background(), authed,
		func(_ context.Context) (*fakeResp, error) {
			calls++
			return &fakeResp{status: 200}, nil
		},
		func(r *fakeResp) int { return r.status },
	)
	if err != nil {
		t.Fatalf("retryOn401: %v", err)
	}
	if resp == nil || resp.status != 200 {
		t.Fatalf("expected 200; got %+v", resp)
	}
	if calls != 1 {
		t.Fatalf("expected 1 call on the fast path; got %d", calls)
	}
}

// TestRetryOn401TransportErrorPropagates — a transport-layer error
// surfaces verbatim without a retry attempt. This is the contract
// renderRequestError consumes on the verb edge.
func TestRetryOn401TransportErrorPropagates(t *testing.T) {
	type fakeResp struct{ status int }
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()
	primeToken(t, srv.URL)
	authed, err := newAuthedClient(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("newAuthedClient: %v", err)
	}
	sentinel := errors.New("network down")
	calls := 0
	_, err = retryOn401(context.Background(), authed,
		func(_ context.Context) (*fakeResp, error) {
			calls++
			return nil, sentinel
		},
		func(r *fakeResp) int { return r.status },
	)
	if !errors.Is(err, sentinel) {
		t.Fatalf("expected sentinel to propagate; got %v", err)
	}
	if calls != 1 {
		t.Fatalf("expected exactly 1 call on transport error; got %d", calls)
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

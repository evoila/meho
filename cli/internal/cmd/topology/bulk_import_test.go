// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// --- parseBulkImportDoc ----------------------------------------------------

func TestParseBulkImportDocYAMLBareEndpoint(t *testing.T) {
	data := []byte(`edges:
  - from: sa-a
    kind: authenticates-via
    to: vr-a
`)
	doc, err := parseBulkImportDoc(data)
	if err != nil {
		t.Fatalf("parseBulkImportDoc: %v", err)
	}
	if len(doc.Edges) != 1 {
		t.Fatalf("want 1 edge, got %d", len(doc.Edges))
	}
	e := doc.Edges[0]
	if e.From.Name != "sa-a" || e.From.Kind != "" {
		t.Errorf("bare from: got %+v", e.From)
	}
	if e.Kind != "authenticates-via" {
		t.Errorf("kind: got %q", e.Kind)
	}
	if e.To.Name != "vr-a" || e.To.Kind != "" {
		t.Errorf("bare to: got %+v", e.To)
	}
}

func TestParseBulkImportDocYAMLNestedEndpoint(t *testing.T) {
	data := []byte(`edges:
  - from: { name: svc-orders, kind: service }
    kind: depends-on
    to: { name: db-orders, kind: service }
    note: "rebuilt"
    evidence_url: "https://x/y#L1"
`)
	doc, err := parseBulkImportDoc(data)
	if err != nil {
		t.Fatalf("parseBulkImportDoc: %v", err)
	}
	e := doc.Edges[0]
	if e.From.Name != "svc-orders" || e.From.Kind != "service" {
		t.Errorf("nested from: got %+v", e.From)
	}
	if e.To.Name != "db-orders" || e.To.Kind != "service" {
		t.Errorf("nested to: got %+v", e.To)
	}
	if e.Note != "rebuilt" {
		t.Errorf("note: got %q", e.Note)
	}
	if e.EvidenceURL != "https://x/y#L1" {
		t.Errorf("evidence_url: got %q", e.EvidenceURL)
	}
}

func TestParseBulkImportDocJSON(t *testing.T) {
	// yaml.v3 parses JSON shapes — operators can feed either format.
	data := []byte(`{"edges":[{"from":"a","kind":"depends-on","to":"b"}]}`)
	doc, err := parseBulkImportDoc(data)
	if err != nil {
		t.Fatalf("parseBulkImportDoc JSON: %v", err)
	}
	if len(doc.Edges) != 1 || doc.Edges[0].From.Name != "a" {
		t.Errorf("JSON parse: got %+v", doc.Edges)
	}
}

func TestParseBulkImportDocMissingKindFails(t *testing.T) {
	data := []byte(`edges:
  - from: a
    to: b
`)
	if _, err := parseBulkImportDoc(data); err == nil {
		t.Fatal("expected error for missing kind")
	} else if !strings.Contains(err.Error(), "kind") {
		t.Errorf("error should mention kind: %v", err)
	}
}

func TestParseBulkImportDocMissingFromFails(t *testing.T) {
	data := []byte(`edges:
  - kind: depends-on
    to: b
`)
	if _, err := parseBulkImportDoc(data); err == nil {
		t.Fatal("expected error for missing from")
	}
}

func TestParseBulkImportDocMalformedYAML(t *testing.T) {
	data := []byte("edges: [not a list of maps")
	if _, err := parseBulkImportDoc(data); err == nil {
		t.Fatal("expected parse error for malformed YAML")
	}
}

// --- formatInvalidBulkEnvelope ---------------------------------------------

func TestFormatInvalidBulkEnvelopeRendersRows(t *testing.T) {
	body := `{"detail":{"error":"invalid_bulk","errors":[` +
		`{"index":0,"error":"node_not_found","message":"node 'ghost' not found","name":"ghost","kind":"vm","kinds":null},` +
		`{"index":2,"error":"invalid_kind","message":"edge kind 'bad' is not in the v0.2 vocabulary","name":null,"kind":"bad","kinds":["depends-on","runs-on"]}` +
		`]}}`
	msg := formatInvalidBulkEnvelope(body)
	if !strings.Contains(msg, "2 row(s) failed") {
		t.Errorf("missing count: %q", msg)
	}
	if !strings.Contains(msg, "row 0: node_not_found") {
		t.Errorf("missing row 0: %q", msg)
	}
	if !strings.Contains(msg, "row 2: invalid_kind") {
		t.Errorf("missing row 2: %q", msg)
	}
}

func TestFormatInvalidBulkEnvelopeReturnsEmptyForGenericBody(t *testing.T) {
	body := `{"detail":[{"loc":["body","edges"],"msg":"field required"}]}`
	if msg := formatInvalidBulkEnvelope(body); msg != "" {
		t.Errorf("expected empty for generic Pydantic body; got %q", msg)
	}
}

// --- printBulkImportSummary ------------------------------------------------

func TestPrintBulkImportSummaryDryRun(t *testing.T) {
	resp := &api.UnderscoreBulkImportResponse{
		DryRun:    true,
		Created:   1,
		Updated:   0,
		Conflicts: 1,
		Rows: []api.UnderscoreBulkImportRowResponse{
			{Index: 0, Action: "create", Kind: "depends-on",
				FromKind: "service", FromName: "svc-A",
				ToKind: "service", ToName: "db-1"},
			{Index: 1, Action: "conflict", Kind: "runs-on",
				FromKind: "vm", FromName: "vm-1",
				ToKind: "host", ToName: "host-new",
				Superseded: []string{"00000000-0000-0000-0000-000000000001"}},
		},
	}
	var buf bytes.Buffer
	printBulkImportSummary(&buf, resp)
	out := buf.String()
	if !strings.Contains(out, "planned (dry-run)") {
		t.Errorf("missing dry-run banner: %q", out)
	}
	if !strings.Contains(out, "Run without --dry-run") {
		t.Errorf("missing dry-run footer: %q", out)
	}
	if !strings.Contains(out, "supersedes auto edge") {
		t.Errorf("missing supersede line: %q", out)
	}
}

func TestPrintBulkImportSummaryApply(t *testing.T) {
	resp := &api.UnderscoreBulkImportResponse{Created: 3, Updated: 0, Conflicts: 0, Rows: []api.UnderscoreBulkImportRowResponse{}}
	var buf bytes.Buffer
	printBulkImportSummary(&buf, resp)
	out := buf.String()
	if !strings.Contains(out, "applied") {
		t.Errorf("missing applied banner: %q", out)
	}
	if strings.Contains(out, "dry-run") {
		t.Errorf("apply summary leaked dry-run text: %q", out)
	}
}

// --- runBulkImport end-to-end with httptest --------------------------------

func TestRunBulkImportPostsBatchAndRendersSummary(t *testing.T) {
	tmp := t.TempDir()
	file := filepath.Join(tmp, "edges.yaml")
	if err := os.WriteFile(file, []byte(`edges:
  - from: sa-a
    kind: authenticates-via
    to: vr-a
`), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}

	var receivedBody api.UnderscoreBulkImportRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/topology/edges/bulk" {
			http.Error(w, "wrong path", http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "wrong method", http.StatusMethodNotAllowed)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(raw, &receivedBody); err != nil {
			http.Error(w, "decode body: "+err.Error(), http.StatusBadRequest)
			return
		}
		edgeID := "11111111-2222-3333-4444-555555555555"
		resp := api.UnderscoreBulkImportResponse{
			DryRun: false, Created: 1, Updated: 0, Conflicts: 0,
			Rows: []api.UnderscoreBulkImportRowResponse{{
				Index: 0, Action: "create", EdgeId: &edgeID,
				FromName: "sa-a", FromKind: "principal",
				ToName: "vr-a", ToKind: "vault-role",
				Kind: "authenticates-via",
			}},
		}
		body, _ := json.Marshal(resp)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	seedXDGAndToken(t, srv.URL)
	cmd, stdout, _ := newRunCmd(t)
	if err := runBulkImport(cmd, bulkImportOptions{
		File:              file,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runBulkImport: %v", err)
	}

	if len(receivedBody.Edges) != 1 || receivedBody.Edges[0].From.Name != "sa-a" {
		t.Errorf("wrong body posted: %+v", receivedBody)
	}
	// dry_run should default to false / absent on the wire — the
	// CLI omits it from the body unless --dry-run is set.
	if receivedBody.DryRun != nil && *receivedBody.DryRun {
		t.Errorf("dry-run should default to false; got %v", *receivedBody.DryRun)
	}
	if !strings.Contains(stdout.String(), "Bulk-import applied: 1 to create") {
		t.Errorf("missing summary in stdout: %q", stdout.String())
	}
}

func TestRunBulkImportDryRunForwardsFlag(t *testing.T) {
	tmp := t.TempDir()
	file := filepath.Join(tmp, "edges.yaml")
	if err := os.WriteFile(file, []byte(`edges:
  - from: a
    kind: depends-on
    to: b
`), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}

	var receivedBody api.UnderscoreBulkImportRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &receivedBody)
		resp := api.UnderscoreBulkImportResponse{DryRun: true, Created: 1}
		body, _ := json.Marshal(resp)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	seedXDGAndToken(t, srv.URL)
	cmd, _, stderr := newRunCmd(t)
	if err := runBulkImport(cmd, bulkImportOptions{
		File:              file,
		DryRun:            true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runBulkImport: %v; stderr=%s", err, stderr.String())
	}
	if receivedBody.DryRun == nil || !*receivedBody.DryRun {
		t.Errorf("dry_run not forwarded; got body %+v", receivedBody)
	}
}

func TestRunBulkImportRendersInvalidBulkEnvelope(t *testing.T) {
	tmp := t.TempDir()
	file := filepath.Join(tmp, "edges.yaml")
	if err := os.WriteFile(file, []byte(`edges:
  - from: a
    kind: not-a-kind
    to: b
`), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":{"error":"invalid_bulk","errors":[` +
			`{"index":0,"error":"invalid_kind","message":"kind 'not-a-kind' not in v0.2 vocabulary","name":null,"kind":"not-a-kind","kinds":["depends-on"]}` +
			`]}}`))
	}))
	defer srv.Close()

	seedXDGAndToken(t, srv.URL)
	cmd, _, stderr := newRunCmd(t)
	err := runBulkImport(cmd, bulkImportOptions{
		File:              file,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error from 422 envelope")
	}
	out := stderr.String()
	if !strings.Contains(out, "invalid_kind") {
		t.Errorf("stderr missing invalid_kind detail: %q", out)
	}
	if !strings.Contains(out, "row 0") {
		t.Errorf("stderr missing row index: %q", out)
	}
}

func TestRunBulkImportNonexistentFileSurfacesUnexpected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runBulkImport(cmd, bulkImportOptions{File: "/no/such/file.yaml"})
	if err == nil {
		t.Fatal("expected error for missing file")
	}
	if !strings.Contains(stderr.String(), "no/such/file") {
		t.Errorf("stderr missing path: %q", stderr.String())
	}
}

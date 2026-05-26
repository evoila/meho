// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func strptr(s string) *string { return &s }

// --- validateIngestMode ----------------------------------------------------

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

// --- getCatalog decode -----------------------------------------------------

func TestGetCatalogDecodesEntries(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/catalog": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CatalogResponse{Catalog: []CatalogEntry{
				{
					Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
					RequiresConnectorClass: "VmwareRestConnector",
					Upstream:               []string{"https://example.test/vcenter.yaml"},
					SpecInfoVersion:        strptr("9.0.1"),
				},
				{
					Product: "vault", Version: "1.x", ImplID: "vault",
					RequiresConnectorClass: "VaultConnector",
					Upstream:               nil, // typed
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

// --- printCatalogTable -----------------------------------------------------

func TestPrintCatalogTableHappyPath(t *testing.T) {
	var buf bytes.Buffer
	registered := map[string]bool{tripleKey("vmware", "9.0", "vmware-rest"): true}
	printCatalogTable(&buf, &CatalogResponse{Catalog: []CatalogEntry{
		{Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
			RequiresConnectorClass: "VmwareRestConnector",
			Upstream:               []string{"https://example.test/x.yaml"},
			SpecInfoVersion:        strptr("9.0.1"), Notes: "generic"},
		{Product: "vault", Version: "1.x", ImplID: "vault",
			RequiresConnectorClass: "VaultConnector", Notes: "typed"},
	}}, registered)
	out := buf.String()
	for _, want := range []string{"vmware/9.0", "VmwareRestConnector", "9.0.1", "yes", "vault/1.x", "no"} {
		if !strings.Contains(out, want) {
			t.Errorf("catalog table missing %q\n%s", want, out)
		}
	}
}

func TestPrintCatalogTableUnknownRegistration(t *testing.T) {
	var buf bytes.Buffer
	// nil registered map → registration column renders "?".
	printCatalogTable(&buf, &CatalogResponse{Catalog: []CatalogEntry{
		{Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
			RequiresConnectorClass: "VmwareRestConnector",
			Upstream:               []string{"https://example.test/x.yaml"}},
	}}, nil)
	if !strings.Contains(buf.String(), "?") {
		t.Errorf("nil registration map should render ?\n%s", buf.String())
	}
}

func TestPrintCatalogTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printCatalogTable(&buf, &CatalogResponse{}, map[string]bool{})
	if !strings.Contains(buf.String(), "0 catalog entries") {
		t.Errorf("empty catalog render wrong: %q", buf.String())
	}
}

// --- registeredTriples -----------------------------------------------------

func TestRegisteredTriplesFromMockServer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, ListResponse{Connectors: []Summary{
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

// --- runIngest catalog mode (G0.14-T9 / #1150) -----------------------------

// TestRunIngestCatalogModePostsCatalogEntry pins the post-#1150
// contract: catalog mode POSTs `{"catalog_entry": "..."}` directly.
// The backplane now resolves the entry server-side, so the CLI no
// longer pre-fetches the catalog and posts the resolved quadruple.
func TestRunIngestCatalogModePostsCatalogEntry(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			rawBody, _ = io.ReadAll(r.Body)
			writeJSON(t, w, 200, IngestResponse{
				Ingestion: IngestionResult{ConnectorID: "vmware-rest-9.0", InsertedCount: 961},
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
	// body. The omitempty tags on IngestRequest's pointer fields are
	// the load-bearing mechanism.
	var posted map[string]any
	if err := json.Unmarshal(rawBody, &posted); err != nil {
		t.Fatalf("decode posted body: %v", err)
	}
	if posted["catalog_entry"] != "vmware/9.0" {
		t.Errorf("catalog_entry not posted: %+v", posted)
	}
	for _, banned := range []string{"product", "version", "impl_id", "specs"} {
		if _, present := posted[banned]; present {
			t.Errorf("catalog mode must not post %q (would conflict): %+v", banned, posted)
		}
	}
}

// TestRunIngestManualModePostsQuadruple pins the parallel contract:
// manual mode POSTs the explicit quadruple, NOT a catalog_entry.
// This is the regression guard for the historical
// --product/--version/--impl/--spec form.
func TestRunIngestManualModePostsQuadruple(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			rawBody, _ = io.ReadAll(r.Body)
			writeJSON(t, w, 200, IngestResponse{
				Ingestion: IngestionResult{ConnectorID: "test-1.0", InsertedCount: 2},
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
	if _, present := posted["catalog_entry"]; present {
		t.Errorf("manual mode must not post catalog_entry: %+v", posted)
	}
	if posted["product"] != "test" || posted["version"] != "1.0" || posted["impl_id"] != "test-impl" {
		t.Errorf("manual mode posted quadruple wrong: %+v", posted)
	}
}

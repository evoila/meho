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
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func strptr(s string) *string { return &s }

// --- parseCatalogRef -------------------------------------------------------

func TestParseCatalogRefTable(t *testing.T) {
	cases := []struct {
		in               string
		product, version string
		wantErr          bool
	}{
		{"vmware/9.0", "vmware", "9.0", false},
		{"sddc-manager/9.0", "sddc-manager", "9.0", false},
		{"  vmware / 9.0  ", "vmware", "9.0", false},
		{"vmware", "", "", true},
		{"", "", "", true},
		{"vmware/", "", "", true},
		{"/9.0", "", "", true},
	}
	for _, c := range cases {
		p, v, err := parseCatalogRef(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("parseCatalogRef(%q): want error, got (%q,%q)", c.in, p, v)
			} else if !errors.Is(err, errCatalogResolve) {
				t.Errorf("parseCatalogRef(%q): error should wrap errCatalogResolve, got %v", c.in, err)
			}
			continue
		}
		if err != nil || p != c.product || v != c.version {
			t.Errorf("parseCatalogRef(%q) = (%q,%q,%v); want (%q,%q,nil)",
				c.in, p, v, err, c.product, c.version)
		}
	}
}

// --- upstreamSpecs ---------------------------------------------------------

func TestUpstreamSpecs(t *testing.T) {
	got := upstreamSpecs([]string{"https://a/x.yaml", "https://b/y.yaml"})
	if len(got) != 2 || got[0].URI != "https://a/x.yaml" || got[1].URI != "https://b/y.yaml" {
		t.Fatalf("upstreamSpecs mapping wrong: %+v", got)
	}
	if len(upstreamSpecs(nil)) != 0 {
		t.Fatalf("upstreamSpecs(nil) should be empty")
	}
}

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

// --- resolveCatalogEntry ---------------------------------------------------

func catalogServer(t *testing.T, entries []CatalogEntry) string {
	t.Helper()
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/catalog": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CatalogResponse{Catalog: entries})
		},
	})
	t.Cleanup(srv.Close)
	primeToken(t, srv.URL)
	return srv.URL
}

func TestResolveCatalogEntryHappyPath(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{{
		Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
		RequiresConnectorClass: "VmwareRestConnector",
		Upstream:               []string{"https://example.test/vcenter.yaml"},
	}})
	entry, err := resolveCatalogEntry(context.Background(), url, "vmware/9.0")
	if err != nil {
		t.Fatalf("resolveCatalogEntry: %v", err)
	}
	if entry.ImplID != "vmware-rest" || len(entry.Upstream) != 1 {
		t.Fatalf("unexpected entry: %+v", entry)
	}
}

func TestResolveCatalogEntryNotFound(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{{
		Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
		Upstream: []string{"https://example.test/x.yaml"},
	}})
	_, err := resolveCatalogEntry(context.Background(), url, "nsx/4.2")
	if err == nil || !errors.Is(err, errCatalogResolve) {
		t.Fatalf("want errCatalogResolve, got %v", err)
	}
	if !strings.Contains(err.Error(), "vmware/9.0") {
		t.Errorf("not-found error should list available entries; got %v", err)
	}
}

func TestResolveCatalogEntryTypedRefused(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{{
		Product: "vault", Version: "1.x", ImplID: "vault",
		RequiresConnectorClass: "VaultConnector",
		Upstream:               nil,
	}})
	_, err := resolveCatalogEntry(context.Background(), url, "vault/1.x")
	if err == nil || !errors.Is(err, errCatalogResolve) {
		t.Fatalf("want errCatalogResolve, got %v", err)
	}
	if !strings.Contains(err.Error(), "typed connector") {
		t.Errorf("typed refusal message wrong: %v", err)
	}
}

func TestResolveCatalogEntryTemplatedRefused(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{{
		Product: "nsx", Version: "4.2", ImplID: "nsx-rest",
		RequiresConnectorClass: "NsxConnector",
		Upstream:               []string{"https://<nsx-mgr-fqdn>/api/v1/spec/openapi/nsx_api.yaml"},
	}})
	_, err := resolveCatalogEntry(context.Background(), url, "nsx/4.2")
	if err == nil || !errors.Is(err, errCatalogResolve) {
		t.Fatalf("want errCatalogResolve, got %v", err)
	}
	if !strings.Contains(err.Error(), "templated") {
		t.Errorf("templated refusal message wrong: %v", err)
	}
}

func TestResolveCatalogEntryDuplicateRejected(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{
		{Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
			Upstream: []string{"https://example.test/a.yaml"}},
		{Product: "vmware", Version: "9.0", ImplID: "vmware-rest-2",
			Upstream: []string{"https://example.test/b.yaml"}},
	})
	_, err := resolveCatalogEntry(context.Background(), url, "vmware/9.0")
	if err == nil || !errors.Is(err, errCatalogResolve) {
		t.Fatalf("want errCatalogResolve, got %v", err)
	}
	if !strings.Contains(err.Error(), "multiple entries") {
		t.Errorf("duplicate message wrong: %v", err)
	}
}

func TestResolveCatalogEntryEmptyUpstreamRejected(t *testing.T) {
	url := catalogServer(t, []CatalogEntry{{
		Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
		Upstream: []string{"   "},
	}})
	_, err := resolveCatalogEntry(context.Background(), url, "vmware/9.0")
	if err == nil || !errors.Is(err, errCatalogResolve) {
		t.Fatalf("want errCatalogResolve, got %v", err)
	}
	if !strings.Contains(err.Error(), "empty upstream") {
		t.Errorf("empty-upstream message wrong: %v", err)
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

// --- runIngest catalog mode (end-to-end through the verb) -------------------

func TestRunIngestCatalogModeRoundTrip(t *testing.T) {
	var posted IngestRequest
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/catalog": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CatalogResponse{Catalog: []CatalogEntry{{
				Product: "vmware", Version: "9.0", ImplID: "vmware-rest",
				RequiresConnectorClass: "VmwareRestConnector",
				Upstream: []string{
					"https://example.test/vcenter.yaml",
					"https://example.test/vi-json.yaml",
				},
			}}})
		},
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &posted); err != nil {
				t.Errorf("decode ingest body: %v", err)
			}
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

	if err := runIngest(cmd, ingestOptions{Catalog: "vmware/9.0", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runIngest catalog mode: %v", err)
	}
	if posted.Product != "vmware" || posted.Version != "9.0" || posted.ImplID != "vmware-rest" {
		t.Fatalf("posted triple wrong: %+v", posted)
	}
	if len(posted.Specs) != 2 || posted.Specs[0].URI != "https://example.test/vcenter.yaml" {
		t.Fatalf("posted specs wrong: %+v", posted.Specs)
	}
}

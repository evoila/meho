// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func TestRunCollectionCreateRefusesWhenUnprovisioned(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCollectionCreate(cmd, createCollectionOptions{
		CollectionKey: "vmware",
		Vendor:        "VMware",
		BackendType:   "corpus-http",
		Provisioned:   false,
	})
	if exitCodeOf(t, err) != 5 {
		t.Errorf("expected exit 5 (insufficient_role family); got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "addon_not_provisioned") {
		t.Errorf("expected addon_not_provisioned code; got %q", stderr.String())
	}
}

func TestBuildCreateBodyFromFlagsRequiresVendorAndBackend(t *testing.T) {
	cases := []struct {
		name string
		opts createCollectionOptions
	}{
		{"missing key", createCollectionOptions{Vendor: "V", BackendType: "corpus-http"}},
		{"missing vendor", createCollectionOptions{CollectionKey: "k", BackendType: "corpus-http"}},
		{"missing backend type", createCollectionOptions{CollectionKey: "k", Vendor: "V"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildCreateBody(tc.opts); err == nil {
				t.Errorf("expected an error for %s", tc.name)
			}
		})
	}
}

func TestBuildCreateBodyFromFlagsHappyPath(t *testing.T) {
	body, err := buildCreateBody(createCollectionOptions{
		CollectionKey: "vmware",
		Vendor:        "VMware by Broadcom",
		Products:      []string{"vsphere", "nsx"},
		BackendType:   "corpus-http",
		BackendRef:    `{"endpoint":"https://corpus/v1/search"}`,
	})
	if err != nil {
		t.Fatalf("buildCreateBody: %v", err)
	}
	if body.CollectionKey != "vmware" || body.Vendor != "VMware by Broadcom" {
		t.Errorf("identity not wired: %+v", body)
	}
	if body.Backend.Type != "corpus-http" {
		t.Errorf("backend type not wired: %+v", body.Backend)
	}
	if got := body.Backend.Ref["endpoint"]; got != "https://corpus/v1/search" {
		t.Errorf("backend ref not parsed: %v", body.Backend.Ref)
	}
	if body.Products == nil || len(*body.Products) != 2 {
		t.Errorf("products not wired: %v", body.Products)
	}
}

func TestBuildCreateBodyRejectsMalformedBackendRef(t *testing.T) {
	_, err := buildCreateBody(createCollectionOptions{
		CollectionKey: "vmware",
		Vendor:        "V",
		BackendType:   "corpus-http",
		BackendRef:    "not-json",
	})
	if err == nil {
		t.Errorf("expected an error for a non-JSON --backend-ref")
	}
}

func TestBuildCreateBodyFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "collection.json")
	payload := `{"collection_key":"netapp","vendor":"NetApp",` +
		`"backend":{"type":"corpus-http","ref":{"endpoint":"https://corpus/v1/search"}}}`
	if err := os.WriteFile(path, []byte(payload), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	body, err := buildCreateBody(createCollectionOptions{FromFile: path})
	if err != nil {
		t.Fatalf("buildCreateBody from file: %v", err)
	}
	if body.CollectionKey != "netapp" || body.Backend.Type != "corpus-http" {
		t.Errorf("file body not parsed: %+v", body)
	}
}

func TestRunCollectionCreateHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			var got api.DocCollectionCreate
			if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
				t.Errorf("decode body: %v", err)
			}
			if got.CollectionKey != "vmware" {
				t.Errorf("unexpected key: %s", got.CollectionKey)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(api.DocCollection{
				CollectionKey: "vmware",
				Vendor:        "VMware by Broadcom",
				Status:        "provisioning",
				Backend:       map[string]interface{}{"type": "corpus-http"},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runCollectionCreate(cmd, createCollectionOptions{
		CollectionKey:     "vmware",
		Vendor:            "VMware by Broadcom",
		BackendType:       "corpus-http",
		BackendRef:        `{"endpoint":"https://corpus/v1/search"}`,
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionCreate: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"vmware", "provisioning", "probe"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCollectionCreateRendersUnknownBackend422(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnprocessableEntity)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"detail": map[string]interface{}{
					"kind":                "unknown_backend_type",
					"backend_type":        "no-such-backend",
					"valid_backend_types": []string{"corpus-http"},
					"message":             "backend.type='no-such-backend' is not a registered search backend",
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runCollectionCreate(cmd, createCollectionOptions{
		CollectionKey:     "vmware",
		Vendor:            "VMware",
		BackendType:       "no-such-backend",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 (unexpected_response) for a 422; got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "no-such-backend") {
		t.Errorf("expected the unknown-backend detail surfaced; got %q", stderr.String())
	}
}

func TestRunCollectionCreateRendersConflict409(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"detail": "doc collection 'vmware' already exists in the tenant scope",
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runCollectionCreate(cmd, createCollectionOptions{
		CollectionKey:     "vmware",
		Vendor:            "VMware",
		BackendType:       "corpus-http",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 (unexpected_response) for a 409; got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "already exists") {
		t.Errorf("expected the conflict detail surfaced; got %q", stderr.String())
	}
}

func TestCollectionsCreateHelpExitsZero(t *testing.T) {
	cmd := newCollectionsCreateCmd(true)
	cmd.SetArgs([]string{"--help"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("create --help: %v", err)
	}
}

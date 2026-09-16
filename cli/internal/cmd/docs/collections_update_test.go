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

// TestBuildUpdateBodyOnlySendsSetFields is the load-bearing PATCH-semantics
// guard: a backend-only update must NOT carry description / when_to_use /
// products keys, otherwise the server (which distinguishes absent from null)
// would clear those untouched fields.
func TestBuildUpdateBodyOnlySendsSetFields(t *testing.T) {
	raw, err := buildUpdateBody(updateCollectionOptions{
		CollectionKey:  "vmware",
		BackendType:    "corpus-http",
		BackendRef:     `{"endpoint":"https://corpus-new/v1/search"}`,
		setBackendType: true,
		setBackendRef:  true,
	})
	if err != nil {
		t.Fatalf("buildUpdateBody: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, ok := body["backend"]; !ok {
		t.Errorf("backend not wired: %v", body)
	}
	for _, forbidden := range []string{"description", "when_to_use", "products"} {
		if _, ok := body[forbidden]; ok {
			t.Errorf("unset field %q must not be sent (PATCH clobber): %v", forbidden, body)
		}
	}
	backend, _ := body["backend"].(map[string]any)
	ref, _ := backend["ref"].(map[string]any)
	if ref["endpoint"] != "https://corpus-new/v1/search" {
		t.Errorf("backend ref not parsed: %v", backend)
	}
}

func TestBuildUpdateBodyMetadataOnly(t *testing.T) {
	raw, err := buildUpdateBody(updateCollectionOptions{
		CollectionKey:  "vmware",
		Description:    "refreshed",
		setDescription: true,
	})
	if err != nil {
		t.Fatalf("buildUpdateBody: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if body["description"] != "refreshed" {
		t.Errorf("description not wired: %v", body)
	}
	if _, ok := body["backend"]; ok {
		t.Errorf("backend must not be sent when unset: %v", body)
	}
}

func TestBuildUpdateBodyClearRef(t *testing.T) {
	// --backend-type with no --backend-ref sends ref={} (fall back to the
	// deployment's global corpus URL).
	raw, err := buildUpdateBody(updateCollectionOptions{
		CollectionKey:  "vmware",
		BackendType:    "corpus-http",
		setBackendType: true,
	})
	if err != nil {
		t.Fatalf("buildUpdateBody: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	backend, _ := body["backend"].(map[string]any)
	ref, ok := backend["ref"].(map[string]any)
	if !ok || len(ref) != 0 {
		t.Errorf("expected empty ref, got %v", backend)
	}
}

func TestBuildUpdateBodyRejectsRefWithoutType(t *testing.T) {
	_, err := buildUpdateBody(updateCollectionOptions{
		CollectionKey: "vmware",
		BackendRef:    `{"endpoint":"https://x/y"}`,
		setBackendRef: true,
	})
	if err == nil {
		t.Errorf("expected an error for --backend-ref without --backend-type")
	}
}

func TestBuildUpdateBodyRejectsEmpty(t *testing.T) {
	if _, err := buildUpdateBody(updateCollectionOptions{CollectionKey: "vmware"}); err == nil {
		t.Errorf("expected an error for an empty update (no fields set)")
	}
}

func TestBuildUpdateBodyRejectsMalformedBackendRef(t *testing.T) {
	_, err := buildUpdateBody(updateCollectionOptions{
		CollectionKey:  "vmware",
		BackendType:    "corpus-http",
		BackendRef:     "not-json",
		setBackendType: true,
		setBackendRef:  true,
	})
	if err == nil {
		t.Errorf("expected an error for a non-JSON --backend-ref")
	}
}

func TestBuildUpdateBodyFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "update.json")
	payload := `{"backend":{"type":"corpus-http","ref":{"endpoint":"https://corpus-new/v1/search"}}}`
	if err := os.WriteFile(path, []byte(payload), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	raw, err := buildUpdateBody(updateCollectionOptions{FromFile: path})
	if err != nil {
		t.Fatalf("buildUpdateBody from file: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, ok := body["backend"]; !ok {
		t.Errorf("file body not forwarded: %v", body)
	}
	if _, ok := body["description"]; ok {
		t.Errorf("file body must not synthesize unset keys: %v", body)
	}
}

func TestBuildUpdateBodyFromFileRejectsUnknownKey(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "update.json")
	if err := os.WriteFile(path, []byte(`{"vendor":"NetApp"}`), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if _, err := buildUpdateBody(updateCollectionOptions{FromFile: path}); err == nil {
		t.Errorf("expected an error for an unsupported field in --from-file")
	}
}

func TestRunCollectionUpdateHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPatch {
				t.Errorf("expected PATCH; got %s", r.Method)
			}
			var got map[string]any
			if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
				t.Errorf("decode body: %v", err)
			}
			if _, ok := got["backend"]; !ok {
				t.Errorf("backend not sent: %v", got)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
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
	err := runCollectionUpdate(cmd, updateCollectionOptions{
		CollectionKey:     "vmware",
		BackendType:       "corpus-http",
		BackendRef:        `{"endpoint":"https://corpus-new/v1/search"}`,
		setBackendType:    true,
		setBackendRef:     true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionUpdate: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"vmware", "provisioning", "probe"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCollectionUpdateRendersGlobalForbidden403(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"detail": map[string]interface{}{
					"error":          "global_collection_update_forbidden",
					"collection_key": "vmware",
					"message":        "updating it requires the platform_admin capability",
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runCollectionUpdate(cmd, updateCollectionOptions{
		CollectionKey:     "vmware",
		Description:       "x",
		setDescription:    true,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected a non-nil error for a 403")
	}
	if !strings.Contains(stderr.String(), "global_collection_update_forbidden") &&
		!strings.Contains(stderr.String(), "platform_admin") {
		t.Errorf("expected the forbidden detail surfaced; got %q", stderr.String())
	}
}

func TestCollectionsUpdateHelpExitsZero(t *testing.T) {
	cmd := newCollectionsUpdateCmd()
	cmd.SetArgs([]string{"--help"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("update --help: %v", err)
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// #2487. The `meho docs collections delete` verb: DELETE
// /api/v1/doc_collections/<key> → 204, with the two server-side guards
// (409 `collection_not_disabled`, 403 `global_collection`) mapped to
// distinct operator-facing errors.

func TestRunCollectionDeleteRejectsEmptyKey(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	err := runCollectionDelete(cmd, lifecycleOptions{CollectionKey: ""})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 (unexpected_response) for empty key; got %d", exitCodeOf(t, err))
	}
}

func TestRunCollectionDeleteHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete {
				t.Errorf("expected DELETE; got %s", r.Method)
			}
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runCollectionDelete(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionDelete: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"vmware", "deleted", "re-create"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCollectionDeleteNotDisabled409(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"detail": map[string]any{
					"error":          "collection_not_disabled",
					"collection_key": "vmware",
					"status":         "ready",
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runCollectionDelete(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 for a 409 not-disabled; got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "not disabled") {
		t.Errorf("expected a disabled-first hint; got %q", stderr.String())
	}
}

func TestRunCollectionDeleteGlobalRow403(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"detail": map[string]any{
					"error":          "global_collection",
					"collection_key": "vmware",
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runCollectionDelete(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		BackplaneOverride: srv.URL,
	})
	// A structured global_collection 403 is not an insufficient_role miss;
	// it maps to unexpected (exit 4) with a distinct message, not exit 5.
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 for a global_collection 403; got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "global") {
		t.Errorf("expected a global-collection hint; got %q", stderr.String())
	}
}

func TestRunCollectionDeleteInsufficientRole403(t *testing.T) {
	// A plain-string 403 (role miss, not a structured marker) still maps to
	// insufficient_role (exit 5).
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(map[string]any{"detail": "tenant_admin required"})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, _ := newRunCmd(t)
	err := runCollectionDelete(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 5 {
		t.Errorf("expected exit 5 (insufficient_role) for a plain 403; got %d", exitCodeOf(t, err))
	}
}

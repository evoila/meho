// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func TestCollectionsCmdHiddenWhenUnprovisioned(t *testing.T) {
	cmd := newCollectionsCmd(false)
	if !cmd.Hidden {
		t.Errorf("expected collections parent Hidden when unprovisioned")
	}
}

func TestRunCollectionProbeRefusesWhenUnprovisioned(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCollectionProbe(cmd, lifecycleOptions{
		CollectionKey: "vmware",
		Provisioned:   false,
	})
	if exitCodeOf(t, err) != 5 {
		t.Errorf("expected exit 5 (insufficient_role family); got %d", exitCodeOf(t, err))
	}
	if !strings.Contains(stderr.String(), "addon_not_provisioned") {
		t.Errorf("expected addon_not_provisioned code; got %q", stderr.String())
	}
}

func TestRunCollectionProbeRefusalIsBeforeNetwork(t *testing.T) {
	// An unprovisioned refusal must short-circuit before any HTTP call.
	hit := false
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		hit = true
	}))
	defer srv.Close()

	cmd, _, _ := newRunCmd(t)
	_ = runCollectionProbe(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		Provisioned:       false,
		BackplaneOverride: srv.URL,
	})
	if hit {
		t.Errorf("expected no HTTP call for an unprovisioned refusal")
	}
}

func TestRunCollectionProbeRejectsEmptyKey(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	err := runCollectionProbe(cmd, lifecycleOptions{
		CollectionKey: "",
		Provisioned:   true,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 (unexpected_response) for empty key; got %d", exitCodeOf(t, err))
	}
}

func TestRunCollectionProbeHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware/probe",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			docCount := 17000
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(api.BackendReadiness{
				Reachable:  true,
				IndexBuilt: true,
				DocCount:   &docCount,
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runCollectionProbe(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionProbe: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"vmware", "reachable:", "index built:", "17000"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCollectionDisableHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware/disable",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runCollectionDisable(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionDisable: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "is now disabled") {
		t.Errorf("expected confirmation; got %q", stdout.String())
	}
}

func TestRunCollectionEnableForbiddenTransition409(t *testing.T) {
	// A 409 from the route surfaces as the default unexpected_response
	// mapping (exit 4) — the forbidden-transition path.
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware/enable",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"detail": map[string]any{
					"error":       "invalid_collection_transition",
					"from_status": "ready",
					"to_status":   "provisioning",
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, _ := newRunCmd(t)
	err := runCollectionEnable(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 for a 409 conflict; got %d", exitCodeOf(t, err))
	}
}

func TestRunCollectionProbeForbiddenRole403(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections/vmware/probe",
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
	err := runCollectionProbe(cmd, lifecycleOptions{
		CollectionKey:     "vmware",
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 5 {
		t.Errorf("expected exit 5 (insufficient_role) for a 403; got %d", exitCodeOf(t, err))
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunOverridesListEmptyResult -- HTTP 200 + empty array renders
// the `(no broadcast-detail overrides ...)` line in the human table
// and an empty JSON array under --json.
func TestRunOverridesListEmptyResult(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runOverridesList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "(no broadcast-detail overrides") {
		t.Errorf("empty-result rendering missing: %q", stdout.String())
	}
}

// TestRunOverridesListJSON -- --json passes the typed array through.
// Decoded against api.BroadcastOverrideRead directly (the typed
// client's response shape) -- pre-migration this decoded against
// the package-local Entry struct.
func TestRunOverridesListJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"vault.kv.*","scope_field":null,"scope_value":null,` +
			`"detail":"aggregate","created_by_sub":"op-1",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesList --json: %v", err)
	}
	var decoded []api.BroadcastOverrideRead
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded) != 1 || decoded[0].OpIdPattern != "vault.kv.*" {
		t.Errorf("decoded JSON shape mismatch: %+v", decoded)
	}
}

// TestRunOverridesListBindsPatternQuery -- --op-id-pattern lands as a
// URL query parameter on the GET. The typed client encodes the
// `op_id_pattern` form parameter onto the query string per the
// generated `ListOverridesApiV1BroadcastOverridesGetParams` shape.
func TestRunOverridesListBindsPatternQuery(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		got := r.URL.Query().Get("op_id_pattern")
		if got != "k8s.configmap.info" {
			t.Errorf("op_id_pattern query: got %q; want %q", got, "k8s.configmap.info")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{
		OpIDPattern:       "k8s.configmap.info",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesList: %v", err)
	}
}

// TestRunOverridesListGlobPatternEncoded -- a glob pattern with `*`
// survives the typed-client's URL encoding. Pre-migration this was
// covered by `buildListPath` directly; the typed client now does
// the encoding, but the behaviour is the same.
func TestRunOverridesListGlobPatternEncoded(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		got := r.URL.Query().Get("op_id_pattern")
		if got != "vault.kv.*" {
			t.Errorf("op_id_pattern query: got %q; want %q", got, "vault.kv.*")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{
		OpIDPattern:       "vault.kv.*",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesList: %v", err)
	}
}

// TestRunOverridesListRendersTable -- non-empty result renders the
// human-readable table; checks the columns and a row payload.
func TestRunOverridesListRendersTable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"vault.kv.*","scope_field":null,"scope_value":null,` +
			`"detail":"aggregate","created_by_sub":"op-1",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runOverridesList: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"op_id_pattern", // header
		"vault.kv.*",    // pattern column
		"aggregate",     // detail column
		"op-1",          // created_by column
		"-",             // op-wide rule renders nil scope fields as "-"
	} {
		if !strings.Contains(out, want) {
			t.Errorf("table missing %q: %q", want, out)
		}
	}
}

// TestRunOverridesList403RendersInsufficientRole -- non-tenant-admin
// callers see `insufficient_role` derived from the backend's detail
// envelope, not the raw HTTP body.
func TestRunOverridesList403RendersInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"tenant_admin role required"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runOverridesList(cmd, overridesListOptions{BackplaneOverride: srv.URL})
	_ = err
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("stderr should classify as insufficient_role: %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "tenant_admin role required") {
		t.Errorf("stderr should surface backend detail: %q", stderr.String())
	}
}

// TestNewOverridesListCmdAdvertisesFlags -- autocomplete-consumer
// contract.
func TestNewOverridesListCmdAdvertisesFlags(t *testing.T) {
	cmd := newOverridesListCmd()
	for _, name := range []string{"op-id-pattern", "json", "backplane"} {
		if cmd.Flag(name) == nil {
			t.Errorf("list verb missing --%s flag", name)
		}
	}
}

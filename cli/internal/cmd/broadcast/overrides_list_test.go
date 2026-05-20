// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildListPathOmitsEmptyQuery -- without --op-id-pattern, the
// path is the bare resource URL (no trailing `?`).
func TestBuildListPathOmitsEmptyQuery(t *testing.T) {
	got := buildListPath("")
	want := "/api/v1/broadcast/overrides"
	if got != want {
		t.Errorf("buildListPath(\"\") = %q; want %q", got, want)
	}
}

// TestBuildListPathEncodesPattern -- glob characters round-trip
// through url.Values encoding (the `*` survives as `%2A`).
func TestBuildListPathEncodesPattern(t *testing.T) {
	got := buildListPath("vault.kv.*")
	if !strings.Contains(got, "op_id_pattern=") {
		t.Errorf("buildListPath did not encode query: %q", got)
	}
}

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
		_, _ = w.Write([]byte(`[]`))
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

// TestRunOverridesListJSON -- --json passes the raw array through.
func TestRunOverridesListJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"vault.kv.*","scope_field":null,"scope_value":null,` +
			`"detail":"aggregate","created_by_sub":"op-1",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}]`))
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
	var decoded []Entry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded) != 1 || decoded[0].OpIDPattern != "vault.kv.*" {
		t.Errorf("decoded JSON shape mismatch: %+v", decoded)
	}
}

// TestRunOverridesListBindsPatternQuery -- --op-id-pattern lands as a
// URL query parameter on the GET.
func TestRunOverridesListBindsPatternQuery(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		got := r.URL.Query().Get("op_id_pattern")
		if got != "k8s.configmap.info" {
			t.Errorf("op_id_pattern query: got %q; want %q", got, "k8s.configmap.info")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[]`))
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

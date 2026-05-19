// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildRemovePathEscapesID -- the override id path-segment is
// URL-encoded so reserved characters survive the round-trip. Uses
// an ID with `/`, ` `, `?` so a regression that dropped
// `url.PathEscape` from `buildRemovePath` would actually fail the
// test (the pre-fixup UUID-only fixture would have passed even
// with no escaping).
func TestBuildRemovePathEscapesID(t *testing.T) {
	got := buildRemovePath("abc/def ghi?")
	want := "/api/v1/broadcast/overrides/abc%2Fdef%20ghi%3F"
	if got != want {
		t.Errorf("buildRemovePath: got %q; want %q", got, want)
	}
}

// TestRunOverridesRemoveSilentOn204 -- success path emits nothing on
// stdout (mirrors `meho` UX convention).
func TestRunOverridesRemoveSilentOn204(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("method: got %s; want DELETE", r.Method)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runOverridesRemove(cmd, overridesRemoveOptions{
		OverrideID:        "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesRemove: %v", err)
	}
	if stdout.Len() != 0 {
		t.Errorf("success should be silent; got %q", stdout.String())
	}
}

// TestRunOverridesRemove404RendersBackendDetail -- 404 surfaces the
// backend's own `detail` string (post-fixup behavior). The remove
// verb's 404 carries `broadcast_override_not_found`; list/set on
// an older backplane (route missing) would return a different
// detail and the same surface flows through. The pre-fixup
// behavior hard-coded "broadcast override not found" regardless of
// the actual response.
func TestRunOverridesRemove404RendersBackendDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"broadcast_override_not_found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runOverridesRemove(cmd, overridesRemoveOptions{
		OverrideID:        "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	_ = err
	if !strings.Contains(stderr.String(), "broadcast_override_not_found") {
		t.Errorf("stderr should report the backend's detail string: %q", stderr.String())
	}
}

// TestRunOverridesRemoveEmptyIDRejected -- the runner short-circuits
// an empty argument with a clear error message before the HTTP call.
func TestRunOverridesRemoveEmptyIDRejected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runOverridesRemove(cmd, overridesRemoveOptions{
		OverrideID:        "",
		BackplaneOverride: "http://unreached.test",
	})
	_ = err
	if !strings.Contains(stderr.String(), "non-empty <override-id>") {
		t.Errorf("stderr should reject empty override-id: %q", stderr.String())
	}
}

// TestNewOverridesRemoveCmdHasFlags -- autocomplete-consumer contract.
func TestNewOverridesRemoveCmdHasFlags(t *testing.T) {
	cmd := newOverridesRemoveCmd()
	for _, name := range []string{"json", "backplane"} {
		if cmd.Flag(name) == nil {
			t.Errorf("remove verb missing --%s flag", name)
		}
	}
}

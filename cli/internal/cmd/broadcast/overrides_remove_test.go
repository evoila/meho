// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const stubOverrideID = "11111111-1111-1111-1111-111111111111"

// TestParseOverrideIDRejectsGarbage -- the bad-input path returns
// the operator-friendly "override-id is not a valid UUID" message.
// This is the typed-client-edge equivalent of the pre-migration
// `pathEscape`-on-arbitrary-string behaviour: the generated
// `Delete...WithResponse` method requires `openapi_types.UUID`, so
// the verb has to parse + reject before calling.
func TestParseOverrideIDRejectsGarbage(t *testing.T) {
	cases := []string{
		"not-a-uuid",
		"abc/def ghi?",
		"11111111",
	}
	for _, in := range cases {
		t.Run(in, func(t *testing.T) {
			if _, err := parseOverrideID(in); err == nil {
				t.Errorf("parseOverrideID(%q) should have failed", in)
			}
		})
	}
}

// TestParseOverrideIDAcceptsValidUUID -- the happy path round-trips
// a canonical UUID string into the typed UUID expected by the
// generated client.
func TestParseOverrideIDAcceptsValidUUID(t *testing.T) {
	id, err := parseOverrideID(stubOverrideID)
	if err != nil {
		t.Fatalf("parseOverrideID(%q): %v", stubOverrideID, err)
	}
	if id.String() != stubOverrideID {
		t.Errorf("parsed UUID round-trip: got %q; want %q", id.String(), stubOverrideID)
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
		OverrideID:        stubOverrideID,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesRemove: %v", err)
	}
	if stdout.Len() != 0 {
		t.Errorf("success should be silent; got %q", stdout.String())
	}
}

// TestRunOverridesRemovePathContainsID -- the typed client builds
// the DELETE path with the UUID embedded in the path segment.
func TestRunOverridesRemovePathContainsID(t *testing.T) {
	var gotPath string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides/", func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runOverridesRemove(cmd, overridesRemoveOptions{
		OverrideID:        stubOverrideID,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesRemove: %v", err)
	}
	want := "/api/v1/broadcast/overrides/" + stubOverrideID
	if gotPath != want {
		t.Errorf("DELETE path: got %q; want %q", gotPath, want)
	}
}

// TestRunOverridesRemove404RendersBackendDetail -- 404 surfaces the
// backend's own `detail` string. The remove verb's 404 carries
// `broadcast_override_not_found`. Pre-migration this assertion
// hardened the post-fixup behaviour (vs. the hard-coded message);
// post-migration the same `decodeDetail` envelope-unwrapper flows
// through.
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
		OverrideID:        stubOverrideID,
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

// TestRunOverridesRemoveBadUUIDRejected -- a syntactically invalid
// UUID is rejected at the verb edge with the operator-friendly
// "override-id is not a valid UUID" message, instead of either a
// server-side 422 round-trip or a mid-request fmt.Errorf.
func TestRunOverridesRemoveBadUUIDRejected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runOverridesRemove(cmd, overridesRemoveOptions{
		OverrideID:        "not-a-uuid",
		BackplaneOverride: "http://unreached.test",
	})
	_ = err
	if !strings.Contains(stderr.String(), "override-id is not a valid UUID") {
		t.Errorf("stderr should reject bad UUID at the verb edge: %q", stderr.String())
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

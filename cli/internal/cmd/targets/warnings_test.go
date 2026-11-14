// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestPrintTargetWarningsWritesEachLine checks that each warning of a
// create or update answer is printed, and that an answer without
// warnings prints nothing.
func TestPrintTargetWarningsWritesEachLine(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	printTargetWarnings(&out, []byte(`{"name":"kc","warnings":["first line","second line"]}`))
	if out.String() != "Warning: first line\nWarning: second line\n" {
		t.Errorf("warnings = %q", out.String())
	}
	out.Reset()
	printTargetWarnings(&out, []byte(`{"name":"kc"}`))
	printTargetWarnings(&out, []byte(`not json`))
	if out.Len() != 0 {
		t.Errorf("no warning expected, got %q", out.String())
	}
}

// TestAddPrintsTheWarningOnStderr checks that `meho targets add` shows
// the backplane's warning on stderr and keeps stdout for the target.
func TestAddPrintsTheWarningOnStderr(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	answer := strings.Replace(createdTargetJSON, `"extras": {},`,
		`"extras": {}, "warnings": ["The managed realm 'master' is the admin realm of this Keycloak."],`, 1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(answer))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAddCmd(t, srv.URL,
		"kc", "--product", "keycloak", "--host", "kc.example.test", "--extra", "managed_realm=master")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}
	if !strings.Contains(stderr.String(), "Warning: The managed realm 'master' is the admin realm") {
		t.Errorf("stderr has no warning: %q", stderr.String())
	}
	if strings.Contains(stdout.String(), "Warning:") {
		t.Errorf("the warning belongs on stderr, not stdout: %q", stdout.String())
	}
}

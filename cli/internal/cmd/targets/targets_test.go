// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// TestRootCmdLists3Verbs pins the v0.2 verb set so a future split
// (e.g. renaming `describe` to `show`) is caught at compile-test
// time.
func TestRootCmdLists3Verbs(t *testing.T) {
	root := NewRootCmd()
	names := map[string]bool{}
	for _, c := range root.Commands() {
		names[c.Name()] = true
	}
	for _, want := range []string{"list", "describe", "probe"} {
		if !names[want] {
			t.Errorf("expected subcommand %q on `meho targets`; got %v", want, names)
		}
	}
}

// TestRootCmdHelpListsAllVerbs — the parent's help text should list
// the three verbs so operators new to the surface find them via
// `meho targets --help`.
func TestRootCmdHelpListsAllVerbs(t *testing.T) {
	root := NewRootCmd()
	var buf strings.Builder
	root.SetOut(&buf)
	root.SetErr(&buf)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("`meho targets --help` failed: %v", err)
	}
	help := buf.String()
	for _, want := range []string{"list", "describe", "probe", "operator's tenant"} {
		if !strings.Contains(help, want) {
			t.Errorf("expected `meho targets --help` to mention %q; got:\n%s", want, help)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash mirrors the contract the
// sibling operation/retrieval packages enforce; trailing-slash
// trimming is the v0.2 convention.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
}

// TestNormaliseURLRejectsEmpty — empty input returns the "empty"
// error string callers depend on.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	_, err := backplane.NormaliseURL("   ")
	if err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("expected 'empty' error; got %v", err)
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired; everything else maps
// to Unexpected. Same routing ladder as the operation sibling.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrappedNotFound := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrappedNotFound)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("ErrConfigNotFound wrapper should classify as auth_expired; got %+v", se)
	}
	parseFailure := errors.New("invalid backplane URL")
	se = backplane.ClassifyError(parseFailure)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected; got %+v", se)
	}
}

// TestTruncateRuneSafe — multi-byte UTF-8 must not produce invalid
// byte cuts. Pins the same contract the operation sibling pins.
func TestTruncateRuneSafe(t *testing.T) {
	if got := truncate("café world", 5); got != "café…" {
		t.Fatalf("truncate multi-byte: got %q; want %q", got, "café…")
	}
	if got := truncate("hello", 10); got != "hello" {
		t.Fatalf("truncate within budget: got %q; want %q", got, "hello")
	}
	if got := truncate("anything", 0); got != "" {
		t.Fatalf("truncate maxLen=0 should be empty; got %q", got)
	}
}

// TestStrDerefNilEmpty — nil pointer returns empty; pointer returns
// underlying string.
func TestStrDerefNilEmpty(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Fatalf("strDeref(nil): got %q; want %q", got, "")
	}
	v := "x"
	if got := strDeref(&v); got != "x" {
		t.Fatalf("strDeref(&v): got %q; want %q", got, "x")
	}
}

// TestPathEscapeRoundTrip — the helper must escape the FastAPI-
// unsafe characters but leave the operator-typical characters
// (hyphens, dots, alphanumerics) alone. The import-side YAML loader
// is the lone caller now that the verb files delegate path-segment
// encoding to the generated typed client.
func TestPathEscapeRoundTrip(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"rdc-vcenter", "rdc-vcenter"},
		{"vc.prod", "vc.prod"},
		{"weird name with space", "weird%20name%20with%20space"},
		{"slash/in/name", "slash%2Fin%2Fname"},
	}
	for _, c := range cases {
		if got := pathEscape(c.in); got != c.want {
			t.Errorf("pathEscape(%q): got %q; want %q", c.in, got, c.want)
		}
	}
}

// TestDecodeDetailStringFromFastAPI — FastAPI's HTTPException body
// is {"detail": "<string>"}; decodeDetailString must extract the
// string. The raw-body fallback fires only on shape mismatch.
func TestDecodeDetailStringFromFastAPI(t *testing.T) {
	body := `{"detail": "Insufficient role: tenant_admin required"}`
	if got := decodeDetailString(body); got != "Insufficient role: tenant_admin required" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallback — non-JSON body returns the raw
// trimmed body rather than swallowing it.
func TestDecodeDetailStringFallback(t *testing.T) {
	body := "  plain text error\n"
	if got := decodeDetailString(body); got != "plain text error" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestFormatNotFoundStructuredDetail — the resolver's structured
// envelope ({"error": "no_target", "query": "X", "matches": [...]})
// renders as a one-line "did you mean" hint listing near-miss names.
func TestFormatNotFoundStructuredDetail(t *testing.T) {
	body := `{"detail":{"error":"no_target","query":"rdc-vc","matches":[
        {"id":"00000000-0000-0000-0000-000000000001","name":"rdc-vcenter","aliases":["vc-prod"],"product":"vcenter","host":"vc.example"},
        {"id":"00000000-0000-0000-0000-000000000002","name":"rdc-vsphere","aliases":[],"product":"vcenter","host":"vs.example"}
    ]}}`
	got := formatNotFound(body)
	for _, want := range []string{"Target not found", `"rdc-vc"`, "rdc-vcenter", "rdc-vsphere"} {
		if !strings.Contains(got, want) {
			t.Errorf("formatNotFound missing %q in %q", want, got)
		}
	}
}

// TestFormatNotFoundEmptyMatches — query with zero near-misses
// surfaces the "(no near-misses)" hint so operators don't expect
// suggestions when there are none.
func TestFormatNotFoundEmptyMatches(t *testing.T) {
	body := `{"detail":{"error":"no_target","query":"totally-unknown","matches":[]}}`
	got := formatNotFound(body)
	if !strings.Contains(got, "no near-misses") {
		t.Errorf("expected near-misses hint; got %q", got)
	}
	if !strings.Contains(got, `"totally-unknown"`) {
		t.Errorf("expected query in detail; got %q", got)
	}
}

// TestFormatNotFoundPlainStringFallback — FastAPI's plain-string
// detail must still produce a "Target not found: ..." line.
func TestFormatNotFoundPlainStringFallback(t *testing.T) {
	body := `{"detail":"some other 404"}`
	got := formatNotFound(body)
	if !strings.Contains(got, "Target not found") || !strings.Contains(got, "some other 404") {
		t.Errorf("plain-string 404 detail not surfaced; got %q", got)
	}
}

// TestFormatAmbiguousStructuredDetail — 409 from resolve_target must
// list the colliding names so the operator can pick one.
func TestFormatAmbiguousStructuredDetail(t *testing.T) {
	body := `{"detail":{"error":"ambiguous_target","query":"k8s","matches":[
        {"id":"00000000-0000-0000-0000-000000000003","name":"rke2-meho","aliases":["k8s"],"product":"k8s","host":"a.example"},
        {"id":"00000000-0000-0000-0000-000000000004","name":"rke2-infra","aliases":["k8s"],"product":"k8s","host":"b.example"}
    ]}}`
	got := formatAmbiguous(body)
	for _, want := range []string{"Ambiguous", `"k8s"`, "rke2-meho", "rke2-infra"} {
		if !strings.Contains(got, want) {
			t.Errorf("formatAmbiguous missing %q in %q", want, got)
		}
	}
}

// TestFormatNoConnectorAppendsG3Pointer — 501 detail string must
// retain the backend's message and add the G3-goal pointer so
// operators know where to look for the connector work.
func TestFormatNoConnectorAppendsG3Pointer(t *testing.T) {
	body := `{"detail":"no connector registered for product='vcenter'"}`
	got := formatNoConnector(body)
	if !strings.Contains(got, "no connector registered for product='vcenter'") {
		t.Errorf("expected raw backend detail in %q", got)
	}
	if !strings.Contains(got, "Goal G3") {
		t.Errorf("expected G3 pointer in %q", got)
	}
}

// TestRenderHTTPStatusRoutesByStatus — each HTTP status the targets
// surface produces lands in the right StructuredError category.
// Pins the (statusCode, body) renderer the typed-client verbs feed
// directly off the generated response envelope.
func TestRenderHTTPStatusRoutesByStatus(t *testing.T) {
	cases := []struct {
		name     string
		status   int
		body     string
		wantCode string
		wantExit int
	}{
		{
			name:     "401 maps to auth_expired",
			status:   http.StatusUnauthorized,
			body:     `{"detail":"token rejected"}`,
			wantCode: "auth_expired",
			wantExit: 2,
		},
		{
			name:     "403 maps to insufficient_role",
			status:   http.StatusForbidden,
			body:     `{"detail":"Insufficient role: tenant_admin required"}`,
			wantCode: "insufficient_role",
			wantExit: 5,
		},
		{
			name:     "404 maps to unexpected_response",
			status:   http.StatusNotFound,
			body:     `{"detail":{"error":"no_target","query":"x","matches":[]}}`,
			wantCode: "unexpected_response",
			wantExit: 4,
		},
		{
			name:     "409 maps to unexpected_response",
			status:   http.StatusConflict,
			body:     `{"detail":{"error":"ambiguous_target","query":"y","matches":[]}}`,
			wantCode: "unexpected_response",
			wantExit: 4,
		},
		{
			name:     "501 maps to unexpected_response",
			status:   http.StatusNotImplemented,
			body:     `{"detail":"no connector registered for product='vault'"}`,
			wantCode: "unexpected_response",
			wantExit: 4,
		},
		{
			name:     "500 falls through to unexpected_response",
			status:   http.StatusInternalServerError,
			body:     `internal server error`,
			wantCode: "unexpected_response",
			wantExit: 4,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cmd := &cobra.Command{}
			var stderr strings.Builder
			cmd.SetErr(&stderr)
			err := renderHTTPStatus(cmd, "https://meho.test", c.status, []byte(c.body), false)
			if err == nil {
				t.Fatalf("expected non-nil error")
			}
			type ec interface{ ExitCode() int }
			x, ok := err.(ec)
			if !ok {
				t.Fatalf("error %v should expose ExitCode", err)
			}
			if x.ExitCode() != c.wantExit {
				t.Fatalf("exit code: got %d; want %d", x.ExitCode(), c.wantExit)
			}
			// The structured-error rendering writes the code into
			// stderr (human path). Check the prefix lands so
			// downstream `meho status` / `gh` scrapers stay
			// reliable.
			if !strings.Contains(stderr.String(), c.wantCode) {
				t.Errorf("stderr missing %q; got %q", c.wantCode, stderr.String())
			}
		})
	}
}

// TestHTTPErrorErrorString — the *httpError formatter must not lose
// the underlying status / body so wrapping errors stays useful. The
// type now lives in import.go (the lone remaining caller); the test
// stays in package_test so it runs against the same in-tree
// definition.
func TestHTTPErrorErrorString(t *testing.T) {
	he := &httpError{StatusCode: 418, Body: "i am a teapot"}
	got := he.Error()
	if !strings.Contains(got, "HTTP 418") || !strings.Contains(got, "i am a teapot") {
		t.Errorf("httpError.Error() lost detail: %q", got)
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// attached. The runXxx helpers consume cmd.OutOrStdout /
// cmd.ErrOrStderr; tests inspect the buffers afterwards.
//
// Lives in the package-level _test.go alongside the helpers it
// shares with usage_test.go and the renderRequestError /
// oversized-response coverage below — same shape as the kb sibling
// package's `kb_test.go::newRunCmd`.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// TestRenderRequestErrorEmptyBearerMapsToAuthExpired — an empty
// stored bearer is a credential-state failure (the token row
// exists but its `access_token` is empty); renderRequestError must
// map it to auth_expired (exit 2) with a `meho login` hint rather
// than letting it fall through to unreachable (exit 3) the generic
// error string would land on. The sentinel is set by
// newAuthedClient.
func TestRenderRequestErrorEmptyBearerMapsToAuthExpired(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(cmd, "https://meho.test", errMissingAccessToken, false)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 2 {
		t.Errorf("expected ExitCode 2 (auth_expired); got %v", err)
	}
}

// TestRenderRequestErrorMaxBytesErrorMapsToUnexpected pins the
// classification branch in `renderRequestError` that routes an
// `*http.MaxBytesError` to `output.Unexpected` (exit 4) rather
// than `output.Unreachable` (exit 3). The end-to-end coverage in
// `TestRunUsageRejectsOversizedResponse` exercises the full
// transport-cap → renderRequestError path; this unit test pins the
// classification ladder directly so a future regression that
// re-orders the branches surfaces here too.
func TestRenderRequestErrorMaxBytesErrorMapsToUnexpected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(
		cmd,
		"https://meho.test",
		&http.MaxBytesError{Limit: 1024},
		false,
	)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// TestRenderRequestErrorJSONSyntaxErrorMapsToUnexpected pins the
// classification branch in `renderRequestError` that routes a
// JSON shape failure (`*json.SyntaxError`) to `output.Unexpected`.
// The generated `Parse*Response` helpers can surface this when a
// backplane / proxy returns 2xx with a malformed JSON body — that's
// a contract failure on the server, not a transport-down failure
// on the operator.
func TestRenderRequestErrorJSONSyntaxErrorMapsToUnexpected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(
		cmd,
		"https://meho.test",
		&json.SyntaxError{Offset: 12},
		false,
	)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
}

// TestRunUsageRejectsOversizedResponse pins the 1-MiB response-body
// cap coverage on the typed-client surface. The cap is installed at
// the transport layer via the inline `capRoundTripper`
// (`cappedHTTPClient` threaded through `api.AuthedClientOptions.HTTPClient`),
// which wraps `rsp.Body` in an `http.MaxBytesReader` so the
// generated `Parse*Response` helpers can't ReadAll an unbounded
// body. When the cap fires, the resulting `*http.MaxBytesError`
// bubbles out of the typed call and `renderRequestError` maps it
// to `output.Unexpected` (exit 4 — `unexpected_response`) rather
// than `output.Unreachable` (exit 3).
//
// The test drives the property end-to-end through `runUsage`
// against an httptest server that returns an oversized 200 body —
// matching the kb sibling's `TestRunListRejectsOversizedResponse`
// shape so the regression class is covered identically across the
// two verb trees.
func TestRunUsageRejectsOversizedResponse(t *testing.T) {
	// One byte over the cap so the MaxBytesReader fires on the
	// final read (the +1 is the documented overshoot detection
	// pattern from the net/http source).
	oversized := strings.Repeat("a", int(responseBodyCap)+1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve/usage", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// The body is not valid JSON, but that doesn't matter — the
		// MaxBytesReader cap trips before the JSON parser ever sees
		// the bytes; the error surfaces as *http.MaxBytesError, not
		// as a JSON syntax error.
		_, _ = w.Write([]byte(oversized))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUsage(cmd, usageOptions{
		Since: "30d", Surface: "all", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on oversized response")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4 (unexpected_response); got %v", err)
	}
}

// TestRenderHTTPStatus403SurfacesInsufficientRole pins the 403 →
// insufficient_role mapping from the renderHTTPStatus switch.
// Mirrors the usage verb's role-rejected path.
func TestRenderHTTPStatus403SurfacesInsufficientRole(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusForbidden,
		[]byte(`{"detail":"tenant_filter_requires_tenant_admin"}`), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "tenant_filter_requires_tenant_admin") {
		t.Errorf("expected backend detail in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5 (insufficient_role); got %v", err)
	}
}

// TestRenderHTTPStatus422WrapsValidationDetail — 422 from the
// invalid_request path renders with the `invalid request:` prefix
// and the FastAPI envelope intact (the substrate emits a structured
// list for some 422s; preserving the body lets operators paste it
// into the issue without losing context).
func TestRenderHTTPStatus422WrapsValidationDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	body := `{"detail":[{"loc":["body","surface"],"msg":"value is not a valid enumeration member"}]}`
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusUnprocessableEntity,
		[]byte(body), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("expected `invalid request` prefix; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "enumeration") {
		t.Errorf("expected substrate detail preserved; got %q", stderr.String())
	}
}

// TestDecodeDetailStringFromFastAPI — FastAPI's HTTPException body
// is {"detail": "<string>"}; decodeDetailString must extract it.
func TestDecodeDetailStringFromFastAPI(t *testing.T) {
	body := `{"detail": "tenant_filter_requires_tenant_admin"}`
	if got := decodeDetailString(body); got != "tenant_filter_requires_tenant_admin" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallback — non-FastAPI body returns the raw
// trimmed body rather than swallowing it.
func TestDecodeDetailStringFallback(t *testing.T) {
	body := "  plain text error\n"
	if got := decodeDetailString(body); got != "plain text error" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestNewRootCmdRegistersAllThreeVerbs — every advertised verb has
// a cobra subcommand. The CLI manifest is the contract operators
// build muscle memory around; dropping a verb silently is the
// regression class we want to catch at unit-time.
func TestNewRootCmdRegistersAllThreeVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"eval":             false,
		"usage":            false,
		"retire-checklist": false,
	}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho retrieval`", name)
		}
	}
}

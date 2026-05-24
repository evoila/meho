// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// resolveBackplane / doAuthedRequest will read. Mirrors the same
// helper in cli/internal/cmd/targets/list_test.go — the auth
// package's keyring backend defaults are disabled for tests so the
// file-store path is exercised deterministically.
func seedXDGAndToken(t *testing.T, backplaneURL string) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// attached. The runXxx helpers consume cmd.OutOrStdout /
// cmd.ErrOrStderr; tests inspect the buffers afterwards.
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

// TestNewRootCmdRegistersAllVerbs — AC1: every advertised verb has a
// cobra subcommand. The CLI manifest is the contract operators build
// muscle memory around; dropping a verb silently is the regression
// class we want to catch at unit-time. G8.2-T5 (#1013) added `replay`.
func TestNewRootCmdRegistersAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"query":       false,
		"recent":      false,
		"show":        false,
		"who-touched": false,
		"my-recent":   false,
		"replay":      false,
	}
	for _, sub := range root.Commands() {
		// cobra splits `Use` on the first space to render <args>
		// (so `show <audit-id>` registers as `show`). Mirror that
		// split for the contains-check.
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho audit`", name)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash — the resolver mirrors the
// targets / operation helpers; the trailing slash invariant is
// load-bearing for the request paths assembled on top.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.example/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.example" {
		t.Errorf("trailing slash not stripped: got %q", got)
	}
}

// TestNormaliseURLRejectsHostlessInput — bare paths fail fast rather
// than producing a request against the local filesystem.
func TestNormaliseURLRejectsHostlessInput(t *testing.T) {
	if _, err := backplane.NormaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

// TestNormaliseURLRejectsGarbage — a fundamentally unparseable URL
// surfaces the parse error rather than the silently-empty resolver.
func TestNormaliseURLRejectsGarbage(t *testing.T) {
	if _, err := backplane.NormaliseURL("h ttp://broken"); err == nil {
		t.Errorf("expected error for malformed URL")
	}
}

// TestNormaliseURLRejectsEmpty — empty config slot must reach the
// caller as an error rather than producing a zero-host request.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := backplane.NormaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

// TestDecodeDetailStringPullsString — FastAPI's HTTPException body
// surfaces as `{"detail": "<string>"}`. The audit-API 400 path
// (DurationParseError / InvalidCursorError / UnsupportedFilterError)
// uses that shape, so the CLI's error renderer leans on this helper.
func TestDecodeDetailStringPullsString(t *testing.T) {
	body := `{"detail": "cursor is not valid base64"}`
	if got := decodeDetailString(body); got != "cursor is not valid base64" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallsBackOnNonJSON — operators see the raw
// body when the response isn't FastAPI-shaped (a load balancer 503,
// a stray HTML page) rather than an empty error.
func TestDecodeDetailStringFallsBackOnNonJSON(t *testing.T) {
	body := "Service Unavailable"
	if got := decodeDetailString(body); got != "Service Unavailable" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestTruncateRuneAware — multi-byte UTF-8 stays valid when the
// table renderer truncates a long target name.
func TestTruncateRuneAware(t *testing.T) {
	got := truncate("vörtex-vcenter-prod-eu-central-1", 10)
	if len(got) == 0 || !strings.Contains(got, "…") {
		t.Errorf("truncate did not emit ellipsis: %q", got)
	}
}

// TestStrDerefHandlesNil — defensive nil-deref so the table /
// summary helpers don't panic on backend-null fields.
func TestStrDerefHandlesNil(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Errorf("strDeref(nil): got %q", got)
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Errorf("strDeref(&\"hello\"): got %q", got)
	}
}

// TestDoAuthedRequestRejectsOversizedResponse — the 1 MiB cap on
// “io.LimitReader“ is paired with a +1-byte read so a response
// that fills the cap surfaces as a clear error rather than feeding
// a silently-truncated JSON body into the decoder. Without this
// guard, an oversized audit page would surface as
// "unexpected end of JSON input" — confusing to operators and
// indistinguishable from a malformed backend response.
func TestDoAuthedRequestRejectsOversizedResponse(t *testing.T) {
	// Emit 1 MiB + 1 byte of payload — exactly at the threshold the
	// truncation guard fires. Anything strictly above the cap must
	// fail loud rather than silently decode.
	oversized := strings.Repeat("a", int(responseBodyCap)+1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/test", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(oversized))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := doAuthedRequest(ctx, srv.URL, "GET", "/api/v1/audit/test", nil)
	if err == nil {
		t.Fatalf("expected error on oversized response")
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Errorf("error message does not mention size cap: %v", err)
	}
}

// TestDoAuthedRequestAcceptsResponseExactlyAtCap — the threshold
// itself is allowed through. The +1-byte read distinguishes
// "fits in the cap" from "spilled past the cap"; exactly-at-cap
// must still decode.
func TestDoAuthedRequestAcceptsResponseExactlyAtCap(t *testing.T) {
	exact := strings.Repeat("a", int(responseBodyCap))
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/test", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(exact))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, err := doAuthedRequest(ctx, srv.URL, "GET", "/api/v1/audit/test", nil)
	if err != nil {
		t.Fatalf("exact-cap response should not error: %v", err)
	}
	if int64(len(raw)) != responseBodyCap {
		t.Errorf("expected %d bytes; got %d", responseBodyCap, len(raw))
	}
}

// TestDecodeAuditResponsePreservesLargeIntegers — Unix-millis
// timestamps and other 64-bit integers in audit payloads must
// survive the JSON round-trip without losing precision. Without
// “UseNumber()“ they collapse to “float64“ and any integer above
// 2^53 rounds — silently mangling forensic data.
func TestDecodeAuditResponsePreservesLargeIntegers(t *testing.T) {
	// 1745923128091 is a real Unix-millis timestamp shape; well
	// below 2^53 so the failure mode for ``float64`` would be a
	// trailing-decimal render rather than a precision loss, but the
	// principle covers the larger range too.
	raw := []byte(`{
		"id": "00000000-0000-0000-0000-000000000001",
		"ts": "2026-05-13T00:00:00Z",
		"tenant_id": null,
		"principal_sub": "damir",
		"principal_name": null,
		"target_id": null,
		"target_name": null,
		"method": "GET",
		"path": "/x",
		"status_code": 200,
		"request_id": null,
		"duration_ms": null,
		"payload": {"hit_count": 1745923128091, "ratio": 0.5},
		"op_id": "x.y",
		"op_class": "read",
		"result_status": "ok",
		"parent_audit_id": null,
		"agent_session_id": null,
		"broadcast_event_id": null
	}`)
	var entry Entry
	if err := decodeAuditResponse(raw, &entry); err != nil {
		t.Fatalf("decodeAuditResponse: %v", err)
	}
	// The payload's integer must render exactly — no scientific
	// notation, no trailing ``.0``. With ``UseNumber()`` it lands
	// as ``json.Number("1745923128091")`` and formatPayloadScalar
	// emits the bare digits.
	hit, ok := entry.Payload["hit_count"]
	if !ok {
		t.Fatalf("payload missing hit_count: %+v", entry.Payload)
	}
	got := formatPayloadScalar(hit)
	if got != "1745923128091" {
		t.Errorf("integer precision lost: got %q; want %q", got, "1745923128091")
	}
	// And the float case still renders compactly.
	ratio, ok := entry.Payload["ratio"]
	if !ok {
		t.Fatalf("payload missing ratio: %+v", entry.Payload)
	}
	if got := formatPayloadScalar(ratio); got != "0.5" {
		t.Errorf("float render: got %q; want %q", got, "0.5")
	}
}

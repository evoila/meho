// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func ptrStr(s string) *string { return &s }
func ptrBool(b bool) *bool    { return &b }

// TestRenderHumanHealth pins the human-formatter contract. The
// install.sh smoke test reads the first line to grep for "Logged
// in as"; renaming that label is a wire-compat break.
func TestRenderHumanHealth(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{
			Sub:   "alice-sub-id",
			Email: ptrStr("alice@example.com"),
		},
		Vault: api.VaultStatus{Reachable: true, ReadOk: true, Detail: ptrStr("version=42")},
		Db:    api.DbStatus{Migrated: ptrBool(true)},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	got := buf.String()
	for _, want := range []string{
		"Logged in as alice@example.com (sub: alice-sub-id)",
		"Vault: reachable, read OK (version=42)",
		"DB:    migrated",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in human output:\n%s", want, got)
		}
	}
}

// TestRenderHumanHealth_NoEmail confirms the fallback path (no
// email → name → sub) renders the sub alone without the trailing
// "(sub: ...)" tail.
func TestRenderHumanHealth_NoEmail(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "service-account-1"},
		Vault:    api.VaultStatus{Reachable: false, Detail: ptrStr("login_failed: ConnectionRefused")},
		Db:       api.DbStatus{Migrated: ptrBool(false)},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	got := buf.String()
	if !strings.Contains(got, "Logged in as service-account-1\n") {
		t.Errorf("expected bare-sub identity line, got:\n%s", got)
	}
	if !strings.Contains(got, "Vault: unreachable (login_failed: ConnectionRefused)") {
		t.Errorf("expected unreachable rendering, got:\n%s", got)
	}
	if !strings.Contains(got, "DB:    not migrated") {
		t.Errorf("expected not-migrated rendering, got:\n%s", got)
	}
}

// TestPrintJSON confirms the JSON path emits a parseable document
// terminated by a newline. The install.sh smoke test pipes
// `meho status --json | jq .` — a missing trailing newline merges
// the JSON with the next shell prompt and breaks the pipe.
func TestPrintJSON(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "x"},
		Vault:    api.VaultStatus{Reachable: true, ReadOk: true},
		Db:       api.DbStatus{Migrated: ptrBool(true)},
	}
	var buf bytes.Buffer
	if err := PrintJSON(&buf, resp); err != nil {
		t.Fatalf("PrintJSON: %v", err)
	}
	out := buf.String()
	if !strings.HasSuffix(out, "\n") {
		t.Errorf("expected trailing newline, got: %q", out)
	}
	var got api.HealthResponse
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &got); err != nil {
		t.Fatalf("output is not valid JSON: %v\noutput:\n%s", err, out)
	}
	if got.Operator.Sub != "x" {
		t.Errorf("operator sub roundtrip failed; got %+v", got)
	}
}

// TestRenderError_JSONPath confirms the JSON error envelope: shape
// + stderr-only emission + silent error to suppress cobra's
// default printer + exit-code propagation via ExitCoder.
func TestRenderError_JSONPath(t *testing.T) {
	var stderr bytes.Buffer
	err := RenderError(&stderr, AuthExpired("no creds for backplane"), true)
	if err == nil {
		t.Fatal("expected non-nil error")
	}
	if err.Error() != "" {
		t.Errorf("JSON path should return a silentError with no message; got %q", err.Error())
	}
	var coder ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != ExitAuthExpired {
		t.Errorf("expected exit %d, got %d", ExitAuthExpired, coder.ExitCode())
	}
	var envelope map[string]any
	if jerr := json.Unmarshal(bytes.TrimSpace(stderr.Bytes()), &envelope); jerr != nil {
		t.Fatalf("stderr is not valid JSON: %v\nstderr:\n%s", jerr, stderr.String())
	}
	if envelope["error"] != "auth_expired" {
		t.Errorf("expected error=auth_expired, got %v", envelope["error"])
	}
	if envelope["detail"] != "no creds for backplane" {
		t.Errorf("expected detail prose, got %v", envelope["detail"])
	}
	if envelope["exit_code"] != float64(ExitAuthExpired) {
		t.Errorf("expected exit_code=%d, got %v", ExitAuthExpired, envelope["exit_code"])
	}
}

// TestRenderError_HumanPath confirms the human error path: the
// one-line "meho: <code>: <detail>" rendering goes to stderr, the
// returned error is silent (so cobra's default printer doesn't
// double-render), and ExitCoder propagates the exit code.
func TestRenderError_HumanPath(t *testing.T) {
	var stderr bytes.Buffer
	err := RenderError(&stderr, Unreachable("dial tcp: connect: refused"), false)
	got := stderr.String()
	for _, want := range []string{"unreachable", "dial tcp"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in stderr:\n%s", want, got)
		}
	}
	if err.Error() != "" {
		t.Errorf("human path should return silentError (empty .Error()), got %q", err.Error())
	}
	var coder ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != ExitUnreachable {
		t.Errorf("expected exit %d, got %d", ExitUnreachable, coder.ExitCode())
	}
}

// TestExitCodes pins the v0.1 exit-code contract. install.sh
// branches on these values; renumbering is a wire-compat break.
func TestExitCodes(t *testing.T) {
	cases := []struct {
		name string
		err  *StructuredError
		want int
	}{
		{"auth_expired", AuthExpired(""), 2},
		{"unreachable", Unreachable(""), 3},
		{"unexpected", Unexpected(""), 4},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if c.err.ExitCode() != c.want {
				t.Errorf("ExitCode() = %d, want %d", c.err.ExitCode(), c.want)
			}
		})
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunEditOpSendsDestructiveAndReason pins #4711: `--safety
// destructive` is accepted and `--reason` rides the PATCH body.
func TestRunEditOpSendsDestructiveAndReason(t *testing.T) {
	var got map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vault-1.x/operations/vault.kv.delete": func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(raw, &got); err != nil {
				t.Errorf("decode body: %v", err)
			}
			writeJSON(t, w, http.StatusOK, map[string]any{"warnings": []any{}})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, stdout, _ := newIngestStatusTestCmd()
	err := runEditOp(cmd, editOpOptions{
		ConnectorID:       "vault-1.x",
		OpID:              "vault.kv.delete",
		SafetyFlag:        "destructive",
		Reason:            "deletes data for good",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEditOp: %v", err)
	}
	if got["safety_level"] != "destructive" {
		t.Errorf("safety_level = %v, want destructive", got["safety_level"])
	}
	if got["reason"] != "deletes data for good" {
		t.Errorf("reason = %v", got["reason"])
	}
	if !strings.Contains(stdout.String(), "safety: destructive") {
		t.Errorf("summary misses the level:\n%s", stdout.String())
	}
}

// TestRunEditOpRejectsUnknownSafety keeps the CLI-side enum check.
func TestRunEditOpRejectsUnknownSafety(t *testing.T) {
	cmd, _, stderr := newIngestStatusTestCmd()
	if err := runEditOp(cmd, editOpOptions{ConnectorID: "x-1", OpID: "y", SafetyFlag: "nuclear"}); err == nil {
		t.Fatal("an unknown level must fail")
	}
	if !strings.Contains(stderr.String(), "destructive") {
		t.Errorf("message must list all four levels:\n%s", stderr.String())
	}
}

// TestIsFreshSignInRequired tells the fresh sign-in 401 apart from a
// plain expired token.
func TestIsFreshSignInRequired(t *testing.T) {
	fresh := []byte(`{"detail": "approval_off_needs_fresh_sign_in: sign in again"}`)
	if !isFreshSignInRequired(fresh) {
		t.Error("fresh sign-in body not recognised")
	}
	if isFreshSignInRequired([]byte(`{"detail": "token_expired"}`)) {
		t.Error("an expired token is not a fresh sign-in refusal")
	}
	if httpErrorDetail([]byte("plain text")) != "plain text" {
		t.Error("a body without detail falls back to the raw text")
	}
}

// TestPrintOverridesTable renders the report rows, with "(code)" for a
// field the server leaves out and "no" for approval not required.
func TestPrintOverridesTable(t *testing.T) {
	off := false
	reason := "read-only in practice"
	level := "dangerous"
	rows := []api.OperationOverrideRow{
		{
			ConnectorId: "vault-1.x", OpId: "vault.kv.write", Scope: "install",
			RequiresApproval: &off, Reason: &reason, SetBySub: "admin",
			SetAt: time.Date(2026, 10, 28, 9, 30, 0, 0, time.UTC),
		},
		{
			ConnectorId: "vault-1.x", OpId: "vault.kv.read", Scope: "tenant",
			SafetyLevel: &level, SetBySub: "ops",
			SetAt: time.Date(2026, 10, 28, 9, 31, 0, 0, time.UTC),
		},
	}
	var buf bytes.Buffer
	printOverridesTable(&buf, false, rows)
	out := buf.String()
	for _, want := range []string{"2 operation override(s)", "no", "read-only in practice", "dangerous", "(code)", "2026-10-28 09:30"} {
		if !strings.Contains(out, want) {
			t.Errorf("table misses %q:\n%s", want, out)
		}
	}
	buf.Reset()
	printOverridesTable(&buf, true, nil)
	if !strings.Contains(buf.String(), "0 operation(s) running with reduced approval") {
		t.Errorf("empty report heading wrong:\n%s", buf.String())
	}
}

// TestGetOverridesSendsReducedApproval pins the query flag and the decode.
func TestGetOverridesSendsReducedApproval(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/operation-overrides": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("reduced_approval") != "true" {
				t.Errorf("reduced_approval not sent: %q", r.URL.RawQuery)
			}
			writeJSON(t, w, http.StatusOK, map[string]any{"overrides": []any{map[string]any{
				"connector_id": "vault-1.x", "op_id": "vault.kv.write", "scope": "install",
				"tenant_id": nil, "safety_level": nil, "requires_approval": false,
				"reason": "why", "set_by_sub": "admin", "set_at": "2026-10-28T09:30:00Z",
			}}})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, stdout, _ := newIngestStatusTestCmd()
	if err := runOverrides(cmd, true, false, srv.URL); err != nil {
		t.Fatalf("runOverrides: %v", err)
	}
	if !strings.Contains(stdout.String(), "1 operation(s) running with reduced approval") {
		t.Errorf("unexpected output:\n%s", stdout.String())
	}
}

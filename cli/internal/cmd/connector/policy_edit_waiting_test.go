// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"net/http"
	"strings"
	"testing"
)

const waitingRequestID = "7d3a9a52-1c2b-4f5e-8a6d-0b1c2d3e4f5a"

// TestRunEditOpPrintsTheWaitingRequest: with CUSTOM_POLICY on, the
// backplane answers 202 and the command prints who approves and the
// approval request id.
func TestRunEditOpPrintsTheWaitingRequest(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vault-1.x/operations/vault.kv.delete": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusAccepted, map[string]any{
				"status":              "awaiting_approval",
				"approval_request_id": waitingRequestID,
				"message":             "The edit waits for approval. A platform admin approves it in the web console.",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, stdout, _ := newIngestStatusTestCmd()
	err := runEditOp(cmd, editOpOptions{
		ConnectorID:       "vault-1.x",
		OpID:              "vault.kv.delete",
		ClearApproval:     true,
		Reason:            "read-only in practice",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEditOp: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"not changed yet",
		"A platform admin approves it in the web console.",
		"approval request: " + waitingRequestID,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output misses %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "updated") {
		t.Errorf("a waiting edit must not say it was updated:\n%s", out)
	}
}

// TestRunRemoveOverridePrintsTheWaitingRequest: the same for a removal.
func TestRunRemoveOverridePrintsTheWaitingRequest(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/connectors/vault-1.x/operation-overrides/vault.kv.delete": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusAccepted, map[string]any{
				"status":              "awaiting_approval",
				"approval_request_id": waitingRequestID,
				"message":             "The edit waits for approval. An Admin approves it in the web console.",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, stdout, _ := newIngestStatusTestCmd()
	err := runRemoveOverride(cmd, removeOverrideOptions{
		ConnectorID:       "vault-1.x",
		OpID:              "vault.kv.delete",
		Tenant:            "2f1c6b0e-4f2c-4b7e-9a0e-1c2d3e4f5a6b",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRemoveOverride: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "An Admin approves it in the web console.") ||
		!strings.Contains(out, "approval request: "+waitingRequestID) {
		t.Errorf("output misses who approves or the request id:\n%s", out)
	}
	if strings.Contains(out, "override removed") {
		t.Errorf("a waiting removal must not say it was removed:\n%s", out)
	}
}

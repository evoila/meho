// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// TestNewDeleteCmdShape pins the cobra wiring: `delete` is registered
// flat under `meho connector` (the verb the list hint and the
// connector_legacy_tenant_copy message name, #4889), takes exactly
// one arg, and carries the --confirm / --json / --backplane flags the
// enable / disable siblings share.
func TestNewDeleteCmdShape(t *testing.T) {
	root := NewRootCmd()
	var deleteCmd *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "delete" {
			deleteCmd = c
			break
		}
	}
	if deleteCmd == nil {
		t.Fatal("delete not registered under `meho connector`")
	}
	if deleteCmd.Use != "delete <connector_id>" {
		t.Errorf("unexpected Use: %q", deleteCmd.Use)
	}
	for _, name := range []string{"confirm", "json", "backplane"} {
		if deleteCmd.Flags().Lookup(name) == nil {
			t.Errorf("delete is missing the --%s flag", name)
		}
	}
	if !strings.Contains(deleteCmd.Long, "DELETE /api/v1/connectors/<connector_id>") {
		t.Errorf("delete help does not name its route:\n%s", deleteCmd.Long)
	}
	if err := deleteCmd.Args(deleteCmd, []string{}); err == nil {
		t.Error("delete accepted zero args")
	}
}

// TestRunTransitionDelete204 — the happy path through the shared
// transition runner: DELETE /api/v1/connectors/{id} answers 204, the
// --json envelope reports action=deleted.
func TestRunTransitionDelete204(t *testing.T) {
	var method string
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/connectors/vrli-rest-9.0": func(w http.ResponseWriter, r *http.Request) {
			method = r.Method
			w.Header().Set("Meho-Audit-Id", "delete-audit-id")
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})

	p := transitionParams{Verb: verbDelete, Action: "deleted", ConfirmQuestion: "Delete %s?"}
	err := runTransition(cmd, p, transitionOptions{
		ConnectorID:       "vrli-rest-9.0",
		Confirm:           true,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runTransition delete: %v", err)
	}
	if method != http.MethodDelete {
		t.Fatalf("expected a DELETE request, got %q", method)
	}
	var got transitionResult
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("decode --json envelope: %v\n%s", err, out.String())
	}
	if got.ConnectorID != "vrli-rest-9.0" || got.Action != "deleted" ||
		got.AuditID == nil || *got.AuditID != "delete-audit-id" {
		t.Errorf("unexpected envelope: %+v", got)
	}
}

// TestRunTransitionDelete404RendersError — once the tenant copy is
// gone (or never existed) the route answers 404; the verb must fail
// with the status in the error output instead of reporting success.
func TestRunTransitionDelete404RendersError(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/connectors/vrli-rest-9.0": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 404, map[string]string{"detail": "connector 'vrli-rest-9.0' not found"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var errBuf bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&errBuf)

	p := transitionParams{Verb: verbDelete, Action: "deleted", ConfirmQuestion: "Delete %s?"}
	err := runTransition(cmd, p, transitionOptions{
		ConnectorID:       "vrli-rest-9.0",
		Confirm:           true,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected a non-nil error for HTTP 404")
	}
	if !strings.Contains(errBuf.String(), "404") || !strings.Contains(errBuf.String(), "not found") {
		t.Fatalf("404 output missing status and detail: %q", errBuf.String())
	}
}

// TestRunTransitionDelete409InUseListsUsers — the backplane refuses the
// delete because a sensor and a hosted agent still use the connector
// (#5027). The verb fails and prints each user.
func TestRunTransitionDelete409InUseListsUsers(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/connectors/vrli-rest-9.0": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 409, map[string]any{
				"detail": map[string]any{
					"detail":       "connector_in_use",
					"connector_id": "vrli-rest-9.0",
					"users": []map[string]any{
						{"kind": "sensor", "id": "s-1", "name": "disk-free", "op_id": "GET:/disks"},
						{"kind": "hosted_agent", "id": "a-1", "name": "nightly-check", "op_id": nil},
					},
					"other_tenant_user_count": 2,
					"message":                 "connector 'vrli-rest-9.0' is still used",
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var errBuf bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&errBuf)

	p := transitionParams{Verb: verbDelete, Action: "deleted", ConfirmQuestion: "Delete %s?"}
	err := runTransition(cmd, p, transitionOptions{
		ConnectorID:       "vrli-rest-9.0",
		Confirm:           true,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected a non-nil error for HTTP 409 connector_in_use")
	}
	got := errBuf.String()
	for _, want := range []string{
		"connector_in_use",
		"sensor disk-free (operation GET:/disks)",
		"hosted_agent nightly-check",
		"2 more in other tenants",
		"Remove or change each one",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("output missing %q:\n%s", want, got)
		}
	}
}

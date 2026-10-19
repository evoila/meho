// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// Tests for the --prefer tenant|builtin flag on `connector review` and
// `connector enable-reads`, and for the CLI text of the backplane's
// 409 connector_scope_ambiguous error.

const (
	preferTestConnector = "vmware-rest-9.0"
	reviewPath          = "GET /api/v1/connectors/" + preferTestConnector + "/review"
	enableReadsPath     = "POST /api/v1/connectors/" + preferTestConnector + "/enable-reads"
)

// scopeAmbiguousBody is the 409 body the backplane sends when the id
// matches both a built-in row and a tenant row.
func scopeAmbiguousBody() map[string]any {
	return map[string]any{
		"detail": map[string]any{
			"detail":       "connector_scope_ambiguous",
			"connector_id": preferTestConnector,
			"candidates": []map[string]any{
				{"product": "vmware", "version": "9.0", "impl_id": "vmware-rest", "tenant_id": nil},
				{"product": "vmware", "version": "9.0", "impl_id": "vmware-rest", "tenant_id": "00000000-0000-0000-0000-000000000001"},
			},
			"message": "connector 'vmware-rest-9.0' is ambiguous",
		},
	}
}

// reviewPayloadFor returns a minimal review payload whose impl_id tells
// the test which row the fake backplane picked.
func reviewPayloadFor(implID string) api.ConnectorReviewPayload {
	return api.ConnectorReviewPayload{
		ConnectorId: preferTestConnector,
		Product:     "vmware",
		Version:     "9.0",
		ImplId:      implID,
		Groups:      []api.ConnectorReviewGroup{},
	}
}

// scopeTwinBackplane fakes a backplane where the connector id matches
// both a built-in row and a tenant row: no ?prefer= gives the 409, and
// ?prefer=tenant|builtin picks that row. It records every prefer value
// it saw, so a test can also prove that no request was sent.
func scopeTwinBackplane(t *testing.T, seen *[]string) *httptest.Server {
	t.Helper()
	srv := mockBackplane(t, map[string]mockHandler{
		reviewPath: func(w http.ResponseWriter, r *http.Request) {
			prefer := r.URL.Query().Get("prefer")
			*seen = append(*seen, prefer)
			switch prefer {
			case "tenant":
				writeJSON(t, w, 200, reviewPayloadFor("tenant-row"))
			case "builtin":
				writeJSON(t, w, 200, reviewPayloadFor("builtin-row"))
			default:
				writeJSON(t, w, 409, scopeAmbiguousBody())
			}
		},
		enableReadsPath: func(w http.ResponseWriter, r *http.Request) {
			prefer := r.URL.Query().Get("prefer")
			*seen = append(*seen, prefer)
			switch prefer {
			case "tenant":
				writeJSON(t, w, 200, api.EnableReadsResponse{ConnectorId: preferTestConnector, OpsEnabled: 3})
			case "builtin":
				writeJSON(t, w, 200, api.EnableReadsResponse{ConnectorId: preferTestConnector, OpsEnabled: 9})
			default:
				writeJSON(t, w, 409, scopeAmbiguousBody())
			}
		},
	})
	primeToken(t, srv.URL)
	return srv
}

func newPreferTestCmd() (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	var out, errBuf bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	return cmd, &out, &errBuf
}

// assertNamesPreferRetry checks that the error text tells the operator
// how to retry with --prefer.
func assertNamesPreferRetry(t *testing.T, stderr string) {
	t.Helper()
	for _, want := range []string{errCodeScopeAmbiguous, "--prefer tenant", "--prefer builtin", preferTestConnector} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr missing %q: %q", want, stderr)
		}
	}
}

func TestRunReviewPreferPicksRow(t *testing.T) {
	for _, tc := range []struct{ prefer, wantImpl string }{
		{"tenant", "tenant-row"},
		{"builtin", "builtin-row"},
	} {
		t.Run(tc.prefer, func(t *testing.T) {
			var seen []string
			srv := scopeTwinBackplane(t, &seen)
			defer srv.Close()
			cmd, out, _ := newPreferTestCmd()

			if err := runReview(cmd, reviewOptions{
				ConnectorID:       preferTestConnector,
				JSONOut:           true,
				Prefer:            tc.prefer,
				BackplaneOverride: srv.URL,
			}); err != nil {
				t.Fatalf("runReview --prefer %s: %v", tc.prefer, err)
			}
			if len(seen) != 1 || seen[0] != tc.prefer {
				t.Fatalf("prefer sent to backplane: got %v, want [%s]", seen, tc.prefer)
			}
			var got api.ConnectorReviewPayload
			if err := json.Unmarshal(out.Bytes(), &got); err != nil {
				t.Fatalf("decode --json output %q: %v", out.String(), err)
			}
			if got.ImplId != tc.wantImpl {
				t.Fatalf("impl_id: got %q, want %q", got.ImplId, tc.wantImpl)
			}
		})
	}
}

// Without --prefer the CLI sends no prefer param, so the backplane's
// fail-loud 409 stays the default, and the error names the retry.
func TestRunReviewWithoutPreferKeepsAmbiguousError(t *testing.T) {
	var seen []string
	srv := scopeTwinBackplane(t, &seen)
	defer srv.Close()
	cmd, out, errBuf := newPreferTestCmd()

	err := runReview(cmd, reviewOptions{
		ConnectorID:       preferTestConnector,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected an error for the ambiguous 409")
	}
	if len(seen) != 1 || seen[0] != "" {
		t.Fatalf("prefer sent to backplane: got %v, want none", seen)
	}
	if out.Len() != 0 {
		t.Fatalf("stdout should stay empty on error: %q", out.String())
	}
	assertNamesPreferRetry(t, errBuf.String())
}

func TestRunEnableReadsPreferPicksRow(t *testing.T) {
	for _, tc := range []struct {
		prefer  string
		wantOps int
	}{
		{"tenant", 3},
		{"builtin", 9},
	} {
		t.Run(tc.prefer, func(t *testing.T) {
			var seen []string
			srv := scopeTwinBackplane(t, &seen)
			defer srv.Close()
			cmd, out, _ := newPreferTestCmd()

			if err := runEnableReads(cmd, enableReadsOptions{
				ConnectorID:       preferTestConnector,
				Confirm:           true,
				JSONOut:           true,
				Prefer:            tc.prefer,
				BackplaneOverride: srv.URL,
			}); err != nil {
				t.Fatalf("runEnableReads --prefer %s: %v", tc.prefer, err)
			}
			if len(seen) != 1 || seen[0] != tc.prefer {
				t.Fatalf("prefer sent to backplane: got %v, want [%s]", seen, tc.prefer)
			}
			var got enableReadsResult
			if err := json.Unmarshal(out.Bytes(), &got); err != nil {
				t.Fatalf("decode --json output %q: %v", out.String(), err)
			}
			if got.OpsEnabled != tc.wantOps {
				t.Fatalf("ops_enabled: got %d, want %d", got.OpsEnabled, tc.wantOps)
			}
		})
	}
}

// The --json error envelope carries the stable error code so scripts
// can branch on it.
func TestRunEnableReadsWithoutPreferKeepsAmbiguousErrorJSON(t *testing.T) {
	var seen []string
	srv := scopeTwinBackplane(t, &seen)
	defer srv.Close()
	cmd, _, errBuf := newPreferTestCmd()

	err := runEnableReads(cmd, enableReadsOptions{
		ConnectorID:       preferTestConnector,
		Confirm:           true,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected an error for the ambiguous 409")
	}
	if len(seen) != 1 || seen[0] != "" {
		t.Fatalf("prefer sent to backplane: got %v, want none", seen)
	}
	var envelope struct {
		Error  string `json:"error"`
		Detail string `json:"detail"`
	}
	if jerr := json.Unmarshal(errBuf.Bytes(), &envelope); jerr != nil {
		t.Fatalf("decode --json error %q: %v", errBuf.String(), jerr)
	}
	if envelope.Error != errCodeScopeAmbiguous {
		t.Fatalf("error code: got %q, want %q", envelope.Error, errCodeScopeAmbiguous)
	}
	assertNamesPreferRetry(t, errBuf.String())
}

// A bad --prefer value fails before any request goes out.
func TestPreferFlagRejectsUnknownValue(t *testing.T) {
	var seen []string
	srv := scopeTwinBackplane(t, &seen)
	defer srv.Close()

	cmd, _, errBuf := newPreferTestCmd()
	if err := runReview(cmd, reviewOptions{
		ConnectorID:       preferTestConnector,
		Prefer:            "global",
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatal("review: expected an error for --prefer global")
	}
	if !strings.Contains(errBuf.String(), "--prefer must be one of: tenant, builtin") {
		t.Fatalf("review: stderr missing allowed values: %q", errBuf.String())
	}

	cmd, _, errBuf = newPreferTestCmd()
	if err := runEnableReads(cmd, enableReadsOptions{
		ConnectorID:       preferTestConnector,
		Confirm:           true,
		Prefer:            "Tenant",
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatal("enable-reads: expected an error for --prefer Tenant")
	}
	if !strings.Contains(errBuf.String(), "--prefer must be one of: tenant, builtin") {
		t.Fatalf("enable-reads: stderr missing allowed values: %q", errBuf.String())
	}
	if len(seen) != 0 {
		t.Fatalf("no request should reach the backplane; got %v", seen)
	}
}

// Other 409 bodies (for example an invalid state change on `enable`)
// keep the generic unexpected-response rendering.
func TestRenderHTTPStatusOtherConflictStaysGeneric(t *testing.T) {
	cmd, _, errBuf := newPreferTestCmd()
	body := []byte(`{"detail":{"detail":"invalid_state_transition","message":"already enabled"}}`)
	if err := renderHTTPStatus(cmd, "https://backplane.example", http.StatusConflict, body, false); err == nil {
		t.Fatal("expected a non-nil error")
	}
	stderr := errBuf.String()
	if !strings.Contains(stderr, "HTTP 409") || strings.Contains(stderr, "--prefer") {
		t.Fatalf("unexpected rendering for a non-ambiguity 409: %q", stderr)
	}
}

// Both verbs register the flag on the real command tree.
func TestPreferFlagRegistered(t *testing.T) {
	for _, c := range []*cobra.Command{newReviewCmd(), newEnableReadsCmd()} {
		if c.Flags().Lookup("prefer") == nil {
			t.Errorf("%s: --prefer flag not registered", c.Name())
		}
	}
}

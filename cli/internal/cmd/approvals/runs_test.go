// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

const runID = "8f6c0c9e-3a52-4d3e-9a37-1f0b3c2d4e5f"

func newRunApproval(t *testing.T, status string) api.RunApprovalRead {
	t.Helper()
	product := "vcenter"
	return api.RunApprovalRead{
		Id:           mustUUID(t, runID),
		Status:       api.RunApprovalReadStatus(status),
		WorkRef:      "run-0001",
		PrincipalSub: "svc-sub",
		Reason:       "build one VM",
		ExpiresAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
		Entries: []api.RunApprovalEntry{
			{ConnectorId: "vmware-rest-9.0", OpId: "POST:/vcenter/vm"},
			{ConnectorId: "vmware-rest-9.0", OpId: "PATCH:/vcenter/vm/{vm}", TargetProduct: &product},
		},
	}
}

// TestRunApproveUsesTheApprovalSignIn: the approve goes out with the token
// of the fresh approval sign-in, never the stored login.
func TestRunApproveUsesTheApprovalSignIn(t *testing.T) {
	var auth string
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/service-principals/run-approvals/" + runID + "/approve": func(w http.ResponseWriter, r *http.Request) {
			auth = r.Header.Get("Authorization")
			writeJSON(t, w, http.StatusOK, newRunApproval(t, "approved"))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)
	count := stubSignIn(t)

	run, err := postRunApprove(context.Background(), client, mustUUID(t, runID), newApprovalSignIn(client, nil))
	if err != nil {
		t.Fatalf("postRunApprove: %v", err)
	}
	if auth != "Bearer sign-in-1" || *count != 1 {
		t.Errorf("Authorization = %q after %d sign-ins; want the approval sign-in token", auth, *count)
	}
	if run.Status != "approved" {
		t.Errorf("status = %q, want approved", run.Status)
	}
}

// TestRunApproveRetriesOnceAfterAFreshSignInChallenge: MEHO asks for a newer
// sign-in once; the CLI signs in again and the second try goes through.
func TestRunApproveRetriesOnceAfterAFreshSignInChallenge(t *testing.T) {
	calls := 0
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/service-principals/run-approvals/" + runID + "/approve": func(w http.ResponseWriter, _ *http.Request) {
			calls++
			if calls == 1 {
				w.Header().Set("WWW-Authenticate", `Bearer error="insufficient_user_authentication"`)
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"detail":"fresh_sign_in_required"}`))
				return
			}
			writeJSON(t, w, http.StatusOK, newRunApproval(t, "approved"))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)
	count := stubSignIn(t)

	if _, err := postRunApprove(context.Background(), client, mustUUID(t, runID), newApprovalSignIn(client, nil)); err != nil {
		t.Fatalf("postRunApprove: %v", err)
	}
	if calls != 2 || *count != 2 {
		t.Errorf("calls = %d, sign-ins = %d; want 2 and 2", calls, *count)
	}
}

// TestRunApproveSurfacesConflict: a run approval that is not pending is a 409.
func TestRunApproveSurfacesConflict(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/service-principals/run-approvals/" + runID + "/approve": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"run approval is approved; only a pending one can be approved"}`))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)
	stubSignIn(t)

	_, err := postRunApprove(context.Background(), client, mustUUID(t, runID), newApprovalSignIn(client, nil))
	var he *httpResponseError
	if !errors.As(err, &he) || he.statusCode != http.StatusConflict {
		t.Fatalf("want *httpResponseError 409; got %T %v", err, err)
	}
}

// TestPrintRunApprovalShowsEveryEntry: the approver sees the whole list.
func TestPrintRunApprovalShowsEveryEntry(t *testing.T) {
	run := newRunApproval(t, "pending")
	var buf bytes.Buffer
	printRunApproval(&buf, &run)
	out := buf.String()
	for _, want := range []string{
		"Run:          run-0001",
		"What it does: build one VM",
		"Operations (2):",
		"POST:/vcenter/vm  no target",
		"PATCH:/vcenter/vm/{vm}  every target of kind vcenter",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q; got:\n%s", want, out)
		}
	}
}

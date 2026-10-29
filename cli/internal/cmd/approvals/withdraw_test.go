// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestWithdrawPostsReasonAndReturnsView checks the wire shape: one POST
// to /withdraw with the reason, and the view from its answer (no GET).
func TestWithdrawPostsReasonAndReturnsView(t *testing.T) {
	var seen *api.WithdrawRequestBody
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/withdraw": func(w http.ResponseWriter, r *http.Request) {
			b := readJSONBody[api.WithdrawRequestBody](t, r)
			seen = &b
			view := newApprovalView(t, "withdrawn", "")
			at := "2026-10-26T10:00:00Z"
			view.WithdrawnAt = &at
			view.WithdrawReason = b.Reason
			writeJSON(t, w, http.StatusOK, view)
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	detail, err := postWithdraw(context.Background(), client, mustUUID(t, stubID), "plan changed")
	if err != nil {
		t.Fatalf("postWithdraw: %v", err)
	}
	if seen == nil || seen.Reason == nil || *seen.Reason != "plan changed" {
		t.Fatalf("withdraw body: got %+v want reason=plan changed", seen)
	}
	if detail == nil || detail.Status != api.ApprovalRequestStatusWithdrawn {
		t.Fatalf("decoded view: got %+v want status=withdrawn", detail)
	}
}

// TestWithdrawOmitsReasonWhenEmpty: no --reason sends no `reason` key.
func TestWithdrawOmitsReasonWhenEmpty(t *testing.T) {
	var raw []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/withdraw": func(w http.ResponseWriter, r *http.Request) {
			raw, _ = io.ReadAll(r.Body)
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "withdrawn", ""))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	if _, err := postWithdraw(context.Background(), client, mustUUID(t, stubID), ""); err != nil {
		t.Fatalf("postWithdraw: %v", err)
	}
	if strings.Contains(string(raw), `"reason"`) {
		t.Errorf("empty reason should not marshal a `reason` key; got body=%s", raw)
	}
}

// TestWithdrawSurfaces403And409 checks that both refusals reach the caller.
func TestWithdrawSurfaces403And409(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
	}{
		{http.StatusForbidden, `{"detail":"not_request_sender"}`},
		{http.StatusConflict, `{"detail":"approval_request_already_approved"}`},
	} {
		srv := mockBackplane(t, map[string]mockHandler{
			"POST /api/v1/approvals/" + stubID + "/withdraw": func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			},
		})
		client := newTestClient(t, srv)
		_, err := postWithdraw(context.Background(), client, mustUUID(t, stubID), "")
		srv.Close()
		var he *httpResponseError
		if !errors.As(err, &he) || he.statusCode != tc.status {
			t.Fatalf("expected *httpResponseError %d; got %T %v", tc.status, err, err)
		}
	}
}

// TestPrintWithdrawalRendersTimeAndReason pins the human output.
func TestPrintWithdrawalRendersTimeAndReason(t *testing.T) {
	view := newApprovalView(t, "withdrawn", "")
	at := "2026-10-26T10:00:00Z"
	reason := "plan changed"
	view.WithdrawnAt = &at
	view.WithdrawReason = &reason

	var buf bytes.Buffer
	printWithdrawal(&buf, stubID, &view)
	out := buf.String()
	for _, want := range []string{"withdrawn by the sender", "withdrawn at: " + at, "reason:       plan changed"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q; got:\n%s", want, out)
		}
	}
}

// TestPrintDetailRendersWithdrawal: `meho approvals show` names the
// withdrawal, its time and its reason.
func TestPrintDetailRendersWithdrawal(t *testing.T) {
	view := newApprovalView(t, "withdrawn", "")
	at := "2026-10-26T10:00:00Z"
	reason := "plan changed"
	view.WithdrawnAt = &at
	view.WithdrawReason = &reason

	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &view)
	out := stdout.String()
	for _, want := range []string{"Withdrawn:    by the sender at " + at, "Reason:       plan changed"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q; got:\n%s", want, out)
		}
	}
}

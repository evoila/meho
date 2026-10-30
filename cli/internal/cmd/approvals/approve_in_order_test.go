// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const inOrderRef = "gh:example/repo#7"

// groupRow is one request of the change ticket in a test.
type groupRow struct {
	status   string
	level    string
	result   api.ApprovalResultState // only for approved rows
	sub      string
	created  string
	dispatch string // dispatch_status the decide route answers
	decideHT int    // HTTP status the decide route answers (default 200)
}

func inOrderID(n int) string {
	return fmt.Sprintf("00000000-0000-0000-0000-%012d", n)
}

func groupViews(t *testing.T, rows []groupRow) []api.ApprovalRequestView {
	t.Helper()
	views := make([]api.ApprovalRequestView, 0, len(rows))
	for i, r := range rows {
		sub := r.sub
		if sub == "" {
			sub = "agent-sub"
		}
		created := r.created
		if created == "" {
			created = fmt.Sprintf("2026-10-30T08:00:%02dZ", i+1)
		}
		v := api.ApprovalRequestView{
			Id:             uuid.MustParse(inOrderID(i + 1)),
			TenantId:       uuid.MustParse("22222222-2222-2222-2222-222222222222"),
			Status:         api.ApprovalRequestStatus(r.status),
			ConnectorId:    "linux",
			OpId:           fmt.Sprintf("op.step%d", i+1),
			ParamsHash:     "deadbeef",
			PrincipalSub:   sub,
			CreatedAt:      created,
			ProposedEffect: map[string]interface{}{"safety_level": r.level},
		}
		if r.status == "approved" {
			rs := r.result
			v.ResultState = &rs
		}
		views = append(views, v)
	}
	return views
}

// inOrderServer serves the list, the detail and the decide routes for
// rows, and records which ids were decided, in order.
type inOrderServer struct {
	mu      sync.Mutex
	decided []string
	listQ   string
}

func runInOrderWithMock(t *testing.T, rows []groupRow, stdin string, jsonOut bool) (*inOrderServer, string, string, error) {
	t.Helper()
	views := groupViews(t, rows)
	rec := &inOrderServer{}
	routes := map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, r *http.Request) {
			rec.listQ = r.URL.RawQuery
			writeJSON(t, w, http.StatusOK, views)
		},
	}
	for i := range views {
		v := views[i]
		row := rows[i]
		id := v.Id.String()
		routes["GET /api/v1/approvals/"+id] = func(w http.ResponseWriter, _ *http.Request) {
			summary := "writes a file on the host"
			detail := v
			detail.ReviewerContext = &api.ReviewerContextView{Summary: &summary}
			writeJSON(t, w, http.StatusOK, detail)
		}
		routes["POST /api/v1/approvals/"+id+"/decide"] = func(w http.ResponseWriter, _ *http.Request) {
			rec.mu.Lock()
			rec.decided = append(rec.decided, id)
			rec.mu.Unlock()
			if row.decideHT != 0 && row.decideHT != http.StatusOK {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(row.decideHT)
				_, _ = w.Write([]byte(`{"detail":"self_approval_forbidden: refused"}`))
				return
			}
			status := row.dispatch
			body := api.DecideResponseBody{ApprovalRequestId: v.Id, Decision: "approved", DispatchStatus: &status}
			if status == "error" {
				msg := "connector_error: file not found"
				body.DispatchError = &msg
			}
			writeJSON(t, w, http.StatusOK, body)
		}
	}
	srv := mockBackplane(t, routes)
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{AccessToken: "test-bearer", BackplaneURL: srv.URL}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	cmd := &cobra.Command{Use: "approve"}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetContext(context.Background())
	err = runInOrder(cmd, inOrderOpts{WorkRef: inOrderRef, Reason: "change window", JSONOut: jsonOut, BackplaneOverride: srv.URL})
	return rec, stdout.String(), stderr.String(), err
}

// TestInOrderMixedGroup is the acceptance case: caution steps go on by
// themselves, the dangerous and destructive steps wait for a yes, the
// run stops at the step whose run failed, and the later steps stay
// pending (no decide call).
func TestInOrderMixedGroup(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "dangerous", dispatch: "ok"},
		{status: "pending", level: "destructive", dispatch: "already_resumed"},
		{status: "pending", level: "caution", dispatch: "error"},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, stderr, err := runInOrderWithMock(t, rows, "yes\nyes\n", false)
	if err == nil {
		t.Fatal("want an error after the failed step")
	}
	assertExitCode(t, err, 4)
	want := []string{inOrderID(1), inOrderID(2), inOrderID(3), inOrderID(4)}
	if strings.Join(rec.decided, ",") != strings.Join(want, ",") {
		t.Fatalf("decided: got %v want %v", rec.decided, want)
	}
	if !strings.Contains(rec.listQ, "order=oldest") || !strings.Contains(rec.listQ, "status=all") {
		t.Errorf("list query: got %q, want status=all and order=oldest", rec.listQ)
	}
	if strings.Count(stderr, "Type yes to approve step") != 2 {
		t.Errorf("want two yes prompts (dangerous + destructive); stderr:\n%s", stderr)
	}
	if !strings.Contains(stderr, "Step 2 of 5 is dangerous") || !strings.Contains(stderr, "Step 3 of 5 is destructive") {
		t.Errorf("prompts do not name the steps; stderr:\n%s", stderr)
	}
	if !strings.Contains(stderr, "writes a file on the host") {
		t.Errorf("prompt does not show what the step does; stderr:\n%s", stderr)
	}
	for _, s := range []string{"stopped at step 4 of 5", "connector_error: file not found", "Not run: steps 5 (" + inOrderID(5) + ")"} {
		if !strings.Contains(stdout+stderr, s) {
			t.Errorf("output lacks %q; stdout:\n%s\nstderr:\n%s", s, stdout, stderr)
		}
	}
}

// TestInOrderNoYesStopsAtDangerousStep checks that anything but yes at a
// dangerous step stops the run before that step is approved.
func TestInOrderNoYesStopsAtDangerousStep(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "dangerous", dispatch: "ok"},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, "no\n", false)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 1 || rec.decided[0] != inOrderID(1) {
		t.Fatalf("decided: got %v want only step 1", rec.decided)
	}
	if !strings.Contains(stdout, "you did not type yes") {
		t.Errorf("stdout does not say why it stopped:\n%s", stdout)
	}
}

// TestInOrderRefusalReturnsSingleIDError checks that a refusal on a step
// (here a self-approval 403) stops the run with the same error the
// single-id approve returns (insufficient_role, exit 5).
func TestInOrderRefusalReturnsSingleIDError(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "caution", decideHT: http.StatusForbidden},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, _, stderr, err := runInOrderWithMock(t, rows, "", true)
	assertExitCode(t, err, 5)
	if len(rec.decided) != 2 {
		t.Fatalf("decided: got %v want steps 1 and 2", rec.decided)
	}
	if !strings.Contains(stderr, "insufficient_role") || !strings.Contains(stderr, "self_approval_forbidden") {
		t.Errorf("stderr lacks the single-id error:\n%s", stderr)
	}
}

// TestInOrderConflictStops checks that a 409 (the step was already
// decided or expired) stops the run.
func TestInOrderConflictStops(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", decideHT: http.StatusConflict},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, _, _, err := runInOrderWithMock(t, rows, "", false)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 1 {
		t.Fatalf("decided: got %v want only step 1", rec.decided)
	}
}

// TestInOrderRerunSkipsOnlyApprovedOK checks a second run: a step that
// was approved and ran OK is skipped, but a step that was approved and
// whose run failed stops the run, with no decide call at all.
func TestInOrderRerunSkipsOnlyApprovedOK(t *testing.T) {
	rows := []groupRow{
		{status: "approved", level: "caution", result: api.ApprovalResultStateExecuted},
		{status: "approved", level: "caution", result: api.ApprovalResultStateFailed},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, "", true)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 0 {
		t.Fatalf("decided: got %v want none", rec.decided)
	}
	var res inOrderResult
	if jerr := json.Unmarshal([]byte(stdout), &res); jerr != nil {
		t.Fatalf("stdout is not the JSON result: %v\n%s", jerr, stdout)
	}
	got := []string{res.Steps[0].Outcome, res.Steps[1].Outcome, res.Steps[2].Outcome}
	if strings.Join(got, ",") != "skipped,stopped,not_run" || res.StoppedAt != 2 || res.Completed {
		t.Errorf("result: got %+v", res)
	}
	if !strings.Contains(res.Steps[1].Detail, "send it again") {
		t.Errorf("stopped step does not say what to do: %q", res.Steps[1].Detail)
	}
}

// TestInOrderRerunContinuesAfterSkip checks that a run after a fix goes
// on past the steps that already ran OK.
func TestInOrderRerunContinuesAfterSkip(t *testing.T) {
	rows := []groupRow{
		{status: "approved", level: "caution", result: api.ApprovalResultStateExecuted},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, "", false)
	if err != nil {
		t.Fatalf("runInOrder: %v", err)
	}
	if len(rec.decided) != 1 || rec.decided[0] != inOrderID(2) {
		t.Fatalf("decided: got %v want only step 2", rec.decided)
	}
	if !strings.Contains(stdout, "Done: approved 1 step(s)") {
		t.Errorf("stdout:\n%s", stdout)
	}
}

// TestInOrderStopsAtDecidedPredecessor checks that a rejected, expired
// or withdrawn step stops the run before the steps after it.
func TestInOrderStopsAtDecidedPredecessor(t *testing.T) {
	for _, status := range []string{"rejected", "expired", "withdrawn"} {
		t.Run(status, func(t *testing.T) {
			rows := []groupRow{
				{status: status, level: "caution"},
				{status: "pending", level: "caution", dispatch: "ok"},
			}
			rec, _, _, err := runInOrderWithMock(t, rows, "", false)
			assertExitCode(t, err, 4)
			if len(rec.decided) != 0 {
				t.Fatalf("decided: got %v want none", rec.decided)
			}
		})
	}
}

// TestInOrderWarnsOnMixedGroup checks the two warnings: requests from
// more than one requester, and two steps with the same creation time.
func TestInOrderWarnsOnMixedGroup(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok", sub: "alice", created: "2026-10-30T08:00:01Z"},
		{status: "pending", level: "caution", dispatch: "ok", sub: "bob", created: "2026-10-30T08:00:01Z"},
	}
	_, _, stderr, err := runInOrderWithMock(t, rows, "", false)
	if err != nil {
		t.Fatalf("runInOrder: %v", err)
	}
	if !strings.Contains(stderr, "2 requesters (alice, bob)") {
		t.Errorf("no requester warning; stderr:\n%s", stderr)
	}
	if !strings.Contains(stderr, "steps 1 and 2 were created at the same time") {
		t.Errorf("no time-order warning; stderr:\n%s", stderr)
	}
}

// TestApproveArgs checks the command-line rules for --filter-work-ref and
// --in-order.
func TestApproveArgs(t *testing.T) {
	cases := []struct {
		name    string
		args    []string
		wantErr string
	}{
		{"single id", []string{stubID}, ""},
		{"no id", nil, "one request id"},
		{"in-order", []string{"--filter-work-ref", inOrderRef, "--in-order"}, ""},
		{"in-order without ref", []string{"--in-order"}, "needs --filter-work-ref"},
		{"in-order with id", []string{"--filter-work-ref", inOrderRef, "--in-order", stubID}, "takes no request id"},
		{"ref without in-order", []string{"--filter-work-ref", inOrderRef, stubID}, "needs --in-order"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd := newApproveCmd()
			if err := cmd.ParseFlags(tc.args); err != nil {
				t.Fatalf("ParseFlags: %v", err)
			}
			err := cmd.Args(cmd, cmd.Flags().Args())
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("got %v want no error", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("got %v want %q", err, tc.wantErr)
			}
		})
	}
}

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
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const inOrderRef = "gh:example/repo#7"

// yesAll answers yes to every step of a test run.
var yesAll = strings.Repeat("yes\n", 10)

// groupRow is one request of the change ticket in a test.
type groupRow struct {
	status   string
	level    string
	result   api.ApprovalResultState // only for approved rows
	sub      string
	created  string
	dispatch string // dispatch_status the decide route answers
	decideHT int    // HTTP status the decide route answers (default 200)
	// results the detail route answers as result_state after the decide,
	// one per read; the last one repeats. Plays the waiting requester
	// whose run ends later (dispatch "already_resumed").
	later []api.ApprovalResultState
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
	reads   map[string]int // detail reads per id after its decide
	signIns *int           // approval sign-ins of the run
	bearers []string       // the token of each decide call
}

func runInOrderWithMock(t *testing.T, rows []groupRow, stdin string, jsonOut bool) (*inOrderServer, string, string, error) {
	t.Helper()
	views := groupViews(t, rows)
	rec := &inOrderServer{reads: map[string]int{}}
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
			rec.mu.Lock()
			decided := false
			for _, d := range rec.decided {
				decided = decided || d == id
			}
			if decided && len(row.later) > 0 {
				n := rec.reads[id]
				rec.reads[id] = n + 1
				if n >= len(row.later) {
					n = len(row.later) - 1
				}
				state := row.later[n]
				detail.Status = api.ApprovalRequestStatusApproved
				detail.ResultState = &state
			}
			rec.mu.Unlock()
			writeJSON(t, w, http.StatusOK, detail)
		}
		routes["POST /api/v1/approvals/"+id+"/decide"] = func(w http.ResponseWriter, r *http.Request) {
			rec.mu.Lock()
			rec.decided = append(rec.decided, id)
			rec.bearers = append(rec.bearers, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
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
	rec.signIns = stubSignIn(t)
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
	oldWait, oldPoll := inOrderResultWait, inOrderResultPoll
	inOrderResultWait, inOrderResultPoll = 200*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { inOrderResultWait, inOrderResultPoll = oldWait, oldPoll })
	err = runInOrder(cmd, inOrderOpts{WorkRef: inOrderRef, Reason: "change window", JSONOut: jsonOut, BackplaneOverride: srv.URL})
	return rec, stdout.String(), stderr.String(), err
}

// TestInOrderMixedGroup is the acceptance case: every pending step waits
// for a yes, the run stops at the step whose run failed, and the later
// steps stay pending (no decide call). One sign-in covers the caution
// and dangerous steps; the destructive step gets a sign-in of its own,
// and the step after it a new one (#5515).
func TestInOrderMixedGroup(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "dangerous", dispatch: "ok"},
		{status: "pending", level: "destructive", dispatch: "already_resumed",
			later: []api.ApprovalResultState{api.ApprovalResultStateStarted, api.ApprovalResultStateExecuted}},
		{status: "pending", level: "caution", dispatch: "error"},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, stderr, err := runInOrderWithMock(t, rows, yesAll, false)
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
	if strings.Count(stderr, "Type yes to approve step") != 4 {
		t.Errorf("want a yes prompt for each of the four steps; stderr:\n%s", stderr)
	}
	for _, p := range []string{"Step 1 of 5 is caution", "Step 2 of 5 is dangerous", "Step 3 of 5 is destructive", "Step 4 of 5 is caution"} {
		if !strings.Contains(stderr, p) {
			t.Errorf("prompts do not name step %q; stderr:\n%s", p, stderr)
		}
	}
	for _, p := range []string{"Operation:    op.step1", "Connector:    linux", "Risk level:   caution", "Parameters and preview:"} {
		if !strings.Contains(stderr, p) {
			t.Errorf("prompt does not show %q; stderr:\n%s", p, stderr)
		}
	}
	if *rec.signIns != 3 {
		t.Errorf("sign-ins = %d, want 3 (shared, destructive, after destructive)", *rec.signIns)
	}
	if got := strings.Join(rec.bearers, ","); got != "sign-in-1,sign-in-1,sign-in-2,sign-in-3" {
		t.Errorf("decide tokens = %s", got)
	}
	if !strings.Contains(stderr, "writes a file on the host") {
		t.Errorf("prompt does not show what the step does; stderr:\n%s", stderr)
	}
	for _, s := range []string{"stopped at step 4 of 5", "connector_error: file not found", "Not run: steps 5 (" + inOrderID(5) + ")"} {
		if !strings.Contains(stdout+stderr, s) {
			t.Errorf("output lacks %q; stdout:\n%s\nstderr:\n%s", s, stdout, stderr)
		}
	}
	if rec.reads[inOrderID(3)] < 2 {
		t.Errorf("step 3 (already_resumed): want the run to read the request until the result is stored, got %d reads", rec.reads[inOrderID(3)])
	}
}

// TestInOrderOtherCallerClaimsThenFails checks that "already_resumed" is
// not taken as success: the run waits for the stored result of the run
// the waiting requester claimed, and stops when that run fails. The
// step after it is never decided.
func TestInOrderOtherCallerClaimsThenFails(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "already_resumed",
			later: []api.ApprovalResultState{api.ApprovalResultStateStarted, api.ApprovalResultStateStarted, api.ApprovalResultStateFailed}},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, true)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 1 || rec.decided[0] != inOrderID(1) {
		t.Fatalf("decided: got %v want only step 1", rec.decided)
	}
	if rec.reads[inOrderID(1)] != 3 {
		t.Errorf("want 3 reads of step 1 until the failed result, got %d", rec.reads[inOrderID(1)])
	}
	var res inOrderResult
	if jerr := json.Unmarshal([]byte(stdout), &res); jerr != nil {
		t.Fatalf("stdout is not the JSON result: %v\n%s", jerr, stdout)
	}
	if res.Completed || res.StoppedAt != 1 || res.Steps[1].Outcome != stepNotRun {
		t.Errorf("result: got %+v", res)
	}
	if !strings.Contains(res.Steps[0].Detail, "run by the waiting requester failed") {
		t.Errorf("stopped step does not say the other run failed: %q", res.Steps[0].Detail)
	}
}

// TestInOrderOtherCallerResultUnknownStops checks that a run the waiting
// requester claimed, with no stored result in time, stops the run: the
// outcome is unknown, so the next step is not decided.
func TestInOrderOtherCallerResultUnknownStops(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "already_resumed",
			later: []api.ApprovalResultState{api.ApprovalResultStateStarted}},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, false)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 1 {
		t.Fatalf("decided: got %v want only step 1", rec.decided)
	}
	if !strings.Contains(stdout, "has not ended yet") || !strings.Contains(stdout, "meho approvals show "+inOrderID(1)) {
		t.Errorf("stdout does not say the outcome is unknown and how to check:\n%s", stdout)
	}
}

// TestInOrderRerunStopsAtUnfinishedPredecessor checks that a step that
// was approved earlier and whose run has not ended stops a rerun with a
// "run again later" hint, not with "send it again".
func TestInOrderRerunStopsAtUnfinishedPredecessor(t *testing.T) {
	rows := []groupRow{
		{status: "approved", level: "caution", result: api.ApprovalResultStateStarted},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, false)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 0 {
		t.Fatalf("decided: got %v want none", rec.decided)
	}
	if !strings.Contains(stdout, "has not ended yet") || strings.Contains(stdout, "send it again") {
		t.Errorf("stdout:\n%s", stdout)
	}
}

// TestInOrderNoYesStopsAtDangerousStep checks that anything but yes at a
// step stops the run before that step is approved.
func TestInOrderNoYesStopsAtDangerousStep(t *testing.T) {
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "dangerous", dispatch: "ok"},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, "yes\nno\n", false)
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
	rec, _, stderr, err := runInOrderWithMock(t, rows, yesAll, true)
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
	rec, _, _, err := runInOrderWithMock(t, rows, yesAll, false)
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
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, true)
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
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, false)
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
			rec, _, _, err := runInOrderWithMock(t, rows, yesAll, false)
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
	_, _, stderr, err := runInOrderWithMock(t, rows, yesAll, false)
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
		{"several ids", []string{stubID, inOrderID(2)}, ""},
		{"no id", nil, "one or more request ids"},
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

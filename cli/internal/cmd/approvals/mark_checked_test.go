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

	"github.com/evoila/meho/cli/internal/api"
)

// startedNoResultView is an approved request whose run started long ago and
// stored no result, so it is on the outcome-unknown list (#5026).
func startedNoResultView(t *testing.T) api.ApprovalRequestView {
	t.Helper()
	d := newApprovalView(t, "approved", "T-test")
	started := api.ApprovalResultStateStarted
	// The first run started at 09:00; a re-run started at 10:00.
	resumedAt := "2026-11-07T09:00:00Z"
	runStartedAt := "2026-11-07T10:00:00Z"
	d.ResultState = &started
	d.ResumeOutcome = &api.ApprovalResumeOutcomeView{ResumedAt: &resumedAt}
	d.OutcomeUnknown = &api.OutcomeUnknownView{
		Reason:       api.OutcomeUnknownReasonStartedNoResult,
		Advice:       "The run started and stored no result. MEHO does not know if the change happened. Do not run it again. Check the target first.",
		RunStartedAt: &runStartedAt,
	}
	return d
}

func TestListOutcomeUnknownSendsFilterAndNoStatus(t *testing.T) {
	var query map[string][]string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, r *http.Request) {
			query = r.URL.Query()
			writeJSON(t, w, http.StatusOK, []api.ApprovalRequestView{startedNoResultView(t)})
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	items, err := fetchList(context.Background(), client, listOpts{OutcomeUnknown: true})
	if err != nil {
		t.Fatalf("fetchList: %v", err)
	}
	if got := query["outcome_unknown"]; len(got) != 1 || got[0] != "true" {
		t.Errorf("outcome_unknown on the wire = %v, want [true]", got)
	}
	if _, ok := query["status"]; ok {
		t.Errorf("status must not be sent with --outcome-unknown; query=%v", query)
	}
	if len(items) != 1 || items[0].OutcomeUnknown == nil {
		t.Fatalf("decoded items: %+v", items)
	}
}

func TestListOutcomeUnknownRefusesStatus(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()
	stderr, err := runListWithMock(t, srv, listOpts{OutcomeUnknown: true, StatusFilter: "approved"})
	if err == nil || !strings.Contains(stderr.String(), "cannot be combined with --status") {
		t.Fatalf("want a refusal naming --status; err=%v stderr=%s", err, stderr.String())
	}
}

func TestPrintOutcomeUnknownTable(t *testing.T) {
	d := startedNoResultView(t)
	reviewer := "reviewer-sub"
	reviewerName := "Rita Reviewer"
	d.ReviewedBy = &reviewer
	d.ReviewedByName = &reviewerName
	targetID := mustUUID(t, "55555555-5555-5555-5555-555555555555")
	d.TargetId = &targetID

	var buf bytes.Buffer
	printOutcomeUnknownTable(&buf, []api.ApprovalRequestView{d}, 0)
	out := buf.String()
	for _, want := range []string{
		"WHY", "APPROVED-BY", "no-result", targetID.String(), "2026-11-07T10:00:00Z",
		"Rita", "Do not run them again", "meho approvals mark-checked",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("table missing %q in:\n%s", want, out)
		}
	}
	// STARTED shows the latest run start, the time the list counts from.
	if strings.Contains(out, "2026-11-07T09:00:00Z") {
		t.Errorf("STARTED shows the first run start, want the latest run start:\n%s", out)
	}

	var empty bytes.Buffer
	printOutcomeUnknownTable(&empty, nil, 0)
	if !strings.Contains(empty.String(), "no approved changes with an unknown outcome") {
		t.Errorf("empty table: %q", empty.String())
	}
}

func TestCheckVerdictMapsFlagValues(t *testing.T) {
	if v, err := checkVerdict("happened"); err != nil || v != api.OutcomeCheckVerdictHappened {
		t.Errorf("happened -> %q, %v", v, err)
	}
	if v, err := checkVerdict("did-not-happen"); err != nil || v != api.OutcomeCheckVerdictDidNotHappen {
		t.Errorf("did-not-happen -> %q, %v", v, err)
	}
	for _, bad := range []string{"", "yes", "did_not_happen", "HAPPENED"} {
		if _, err := checkVerdict(bad); err == nil {
			t.Errorf("checkVerdict(%q) accepted a bad value", bad)
		}
	}
}

func TestPostOutcomeCheckSendsVerdictAndNote(t *testing.T) {
	var seen *api.OutcomeCheckRequestBody
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/outcome-check": func(w http.ResponseWriter, r *http.Request) {
			b := readJSONBody[api.OutcomeCheckRequestBody](t, r)
			seen = &b
			view := newApprovalView(t, "approved", "")
			view.OutcomeCheck = &api.OutcomeCheckView{
				Verdict:   b.Verdict,
				Note:      b.Note,
				CheckedBy: "checker-sub",
				CheckedAt: "2026-11-07T11:00:00Z",
			}
			writeJSON(t, w, http.StatusOK, view)
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	detail, err := postOutcomeCheck(context.Background(), client, mustUUID(t, stubID),
		api.OutcomeCheckVerdictDidNotHappen, "the record is not on the server")
	if err != nil {
		t.Fatalf("postOutcomeCheck: %v", err)
	}
	if seen == nil || seen.Verdict != api.OutcomeCheckVerdictDidNotHappen || seen.Note != "the record is not on the server" {
		t.Fatalf("body: %+v", seen)
	}
	var buf bytes.Buffer
	printOutcomeCheckResult(&buf, stubID, detail)
	for _, want := range []string{"marked as checked. MEHO ran nothing.", "Checked:      it did not happen", "the record is not on the server", "checker-sub"} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("output missing %q in:\n%s", want, buf.String())
		}
	}
}

func TestPostOutcomeCheckSurfaces403And409(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
	}{
		{http.StatusForbidden, `{"detail":"insufficient_role"}`},
		{http.StatusConflict, `{"detail":"outcome_already_checked"}`},
		{http.StatusConflict, `{"detail":"outcome_not_unknown"}`},
	} {
		srv := mockBackplane(t, map[string]mockHandler{
			"POST /api/v1/approvals/" + stubID + "/outcome-check": func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			},
		})
		client := newTestClient(t, srv)
		_, err := postOutcomeCheck(context.Background(), client, mustUUID(t, stubID), api.OutcomeCheckVerdictHappened, "n")
		srv.Close()
		var he *httpResponseError
		if !errors.As(err, &he) || he.statusCode != tc.status {
			t.Fatalf("expected *httpResponseError %d; got %T %v", tc.status, err, err)
		}
	}
}

func TestMarkCheckedRefusesMissingNoteAndBadOutcome(t *testing.T) {
	for _, tc := range []struct {
		outcome, note, want string
	}{
		{"happened", "  ", "--note is required"},
		{"maybe", "checked it", "--outcome must be happened or did-not-happen"},
	} {
		cmd, _, stderr := newCapturingCmd(t)
		err := runMarkChecked(cmd, stubID, tc.outcome, tc.note, false, "http://127.0.0.1:1")
		if err == nil || !strings.Contains(stderr.String(), tc.want) {
			t.Errorf("outcome=%q note=%q: err=%v stderr=%s", tc.outcome, tc.note, err, stderr.String())
		}
	}
}

func TestPrintDetailRendersOutcomeUnknownAndCheck(t *testing.T) {
	d := startedNoResultView(t)
	cmd, buf, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	if !strings.Contains(buf.String(), "Warning:      The run started and stored no result.") {
		t.Errorf("show misses the advice of a run with no result:\n%s", buf.String())
	}

	name := "Carl Checker"
	d.OutcomeUnknown = nil
	d.OutcomeCheck = &api.OutcomeCheckView{
		Verdict:       api.OutcomeCheckVerdictHappened,
		Note:          "the VM exists",
		CheckedBy:     "checker-sub",
		CheckedByName: &name,
		CheckedAt:     "2026-11-07T11:00:00Z",
	}
	buf.Reset()
	printDetail(cmd, &d)
	for _, want := range []string{"Checked:      it happened", "Check note:   the VM exists", "Carl Checker (checker-sub)"} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("show missing %q in:\n%s", want, buf.String())
		}
	}
}

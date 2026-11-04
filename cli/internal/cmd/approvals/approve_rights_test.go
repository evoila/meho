// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// runDecisionText runs `approve` or `reject` against a mocked backplane
// and returns stdout and stderr. decide answers the POST; the GET after
// it answers with an approved view.
func runDecisionText(t *testing.T, decide mockHandler, verb string, jsonOut bool) (string, *bytes.Buffer, error) {
	t.Helper()
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": decide,
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "approved", "reviewed"))
		},
	})
	t.Cleanup(srv.Close)
	stubSignIn(t)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{AccessToken: "test-bearer", BackplaneURL: srv.URL}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	cmd := &cobra.Command{Use: verb}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetContext(context.Background())
	err = runDecision(cmd, stubID, verb, "", jsonOut, srv.URL)
	return stdout.String(), &stderr, err
}

func decided(t *testing.T, selfApproved bool, rule string) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, api.DecideResponseBody{
			ApprovalRequestId: mustUUID(t, stubID),
			Decision:          "approved",
			SelfApproved:      &selfApproved,
			ApprovalRule:      &rule,
		})
	}
}

func refused(detail string) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"` + detail + `"}`))
	}
}

// TestApproveOfOwnChangePrintsTheSelfApprovalLine checks that approve
// prints a clear self-approval line when the server says the approval
// was the person's own change.
func TestApproveOfOwnChangePrintsTheSelfApprovalLine(t *testing.T) {
	cases := map[string]string{
		ruleSAOperatorSelf: "self-approved as a MEHO-SA-Operator",
		ruleBreakGlassSelf: "self-approved because APPROVAL_ALLOW_SELF_APPROVAL is on",
	}
	for rule, want := range cases {
		t.Run(rule, func(t *testing.T) {
			stdout, stderr, err := runDecisionText(t, decided(t, true, rule), "approve", false)
			if err != nil {
				t.Fatalf("approve: %v (stderr %s)", err, stderr)
			}
			if !strings.Contains(stdout, want) || !strings.Contains(stdout, "recorded in the audit log") {
				t.Errorf("stdout has no self-approval line %q:\n%s", want, stdout)
			}
		})
	}
}

// TestApproveOfAnotherPersonsChangePrintsNoSelfApprovalLine checks that
// an ordinary approve prints no self-approval line.
func TestApproveOfAnotherPersonsChangePrintsNoSelfApprovalLine(t *testing.T) {
	stdout, stderr, err := runDecisionText(t, decided(t, false, "reviewer"), "approve", false)
	if err != nil {
		t.Fatalf("approve: %v (stderr %s)", err, stderr)
	}
	if strings.Contains(stdout, "self-approved") {
		t.Errorf("ordinary approve printed a self-approval line:\n%s", stdout)
	}
	if !strings.Contains(stdout, "→ approved") {
		t.Errorf("stdout has no decision line:\n%s", stdout)
	}
}

// TestApproveRefusalPrintsTheServerTextAndTheWebHint checks that a 403
// prints the server's text as it is, then says the web interface is the
// normal place to approve, with the insufficient_role exit code.
func TestApproveRefusalPrintsTheServerTextAndTheWebHint(t *testing.T) {
	for _, detail := range []string{
		"self_approval_forbidden: operator 'op-1' may not approve approval_request x: You cannot approve your own change.",
		"approval_group_required: operator 'op-1' may not approve approval_request x: Only a MEHO-SA-Operator or a MEHO-Admin may approve dangerous and destructive changes.",
	} {
		_, stderr, err := runDecisionText(t, refused(detail), "approve", true)
		if !strings.Contains(stderr.String(), detail) {
			t.Errorf("refusal does not print the server text %q: %s", detail, stderr)
		}
		assertRenderedErrorCode(t, stderr, err, output.ErrCodeInsufficientRole, approveWebHint)
		assertExitCode(t, err, output.ExitInsufficientRole)
	}
}

// TestRejectRefusalHasNoApproveHint checks that the web hint is only on
// an approve refusal.
func TestRejectRefusalHasNoApproveHint(t *testing.T) {
	_, stderr, err := runDecisionText(t, refused("insufficient_role"), "reject", true)
	if err == nil {
		t.Fatal("reject: want an error")
	}
	if strings.Contains(stderr.String(), approveWebHint) {
		t.Errorf("reject refusal carries the approve hint: %s", stderr)
	}
}

func ptr[T any](v T) *T { return &v }

// TestPrintListTableShowsTheApproveColumn checks that list shows, for
// each request, the server's answer to "may I approve this?".
func TestPrintListTableShowsTheApproveColumn(t *testing.T) {
	rows := []struct {
		may    *bool
		reason *string
		want   string
	}{
		{ptr(true), ptr("ok"), "yes"},
		{ptr(true), ptr("own_change"), "yes:own_change"},
		{ptr(false), ptr("own_change"), "no:own_change"},
		{ptr(false), ptr("wrong_group"), "no:wrong_group"},
		{ptr(false), ptr("not_pending"), "-"},
		{nil, nil, "-"},
	}
	for _, r := range rows {
		item := newApprovalView(t, "pending", "")
		item.MayApprove, item.MayApproveReason = r.may, r.reason
		var buf bytes.Buffer
		printListTable(&buf, []api.ApprovalRequestView{item}, "", "", 0)
		lines := strings.Split(buf.String(), "\n")
		if !strings.Contains(lines[0], "APPROVE") {
			t.Fatalf("header has no APPROVE column: %q", lines[0])
		}
		fields := strings.Fields(lines[1])
		if fields[3] != r.want {
			t.Errorf("APPROVE for %v/%v: got %q want %q (row %q)", r.may, r.reason, fields[3], r.want, lines[1])
		}
	}
}

// TestPrintDetailShowsMayApprove checks the "May approve" line of show.
func TestPrintDetailShowsMayApprove(t *testing.T) {
	cases := []struct {
		may    *bool
		reason *string
		want   string
	}{
		{ptr(true), ptr("ok"), "May approve:  yes\n"},
		{ptr(true), ptr("own_change"), "May approve:  yes, your own change (a self-approval"},
		{ptr(false), ptr("own_change"), "May approve:  no, it is your own change"},
		{ptr(false), ptr("wrong_group"), "May approve:  no, another person's dangerous or destructive change needs a MEHO-SA-Operator or a MEHO-Admin"},
		{ptr(false), ptr("something_new"), "May approve:  no (something_new)"},
	}
	for _, c := range cases {
		d := newApprovalView(t, "pending", "")
		d.MayApprove, d.MayApproveReason = c.may, c.reason
		cmd, stdout, _ := newCapturingCmd(t)
		printDetail(cmd, &d)
		if !strings.Contains(stdout.String(), c.want) {
			t.Errorf("show missing %q:\n%s", c.want, stdout.String())
		}
	}

	d := newApprovalView(t, "pending", "")
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	if strings.Contains(stdout.String(), "May approve:") {
		t.Errorf("show printed May approve without an answer from the server:\n%s", stdout.String())
	}
}

// TestInOrderStepPrintsTheSelfApprovalLine checks the in-order output.
func TestInOrderStepPrintsTheSelfApprovalLine(t *testing.T) {
	var buf bytes.Buffer
	line := selfApprovalLine(&api.DecideResponseBody{SelfApproved: ptr(true), ApprovalRule: ptr(ruleSAOperatorSelf)})
	printInOrderStep(&buf, inOrderStep{Step: 1, ID: stubID, OpID: "op", Outcome: stepApproved, SelfApproval: line}, 1)
	if !strings.Contains(buf.String(), "self-approved as a MEHO-SA-Operator") {
		t.Errorf("in-order step has no self-approval line:\n%s", buf.String())
	}
}

// TestSelfApprovalLineNeedsTheServerAnswer checks that the line comes
// only from the server's answer, never from a guess.
func TestSelfApprovalLineNeedsTheServerAnswer(t *testing.T) {
	for _, resp := range []*api.DecideResponseBody{
		nil,
		{},
		{SelfApproved: ptr(false), ApprovalRule: ptr("reviewer")},
		{SelfApproved: ptr(true)},
	} {
		if got := selfApprovalLine(resp); got != "" {
			t.Errorf("selfApprovalLine(%+v) = %q, want empty", resp, got)
		}
	}
}

// TestApproveHelpExplainsWhoMayApprove checks the --help texts.
func TestApproveHelpExplainsWhoMayApprove(t *testing.T) {
	for _, want := range []string{"MEHO-SA-Operator may approve their own change", "self-approval line", approveWebHint} {
		if !strings.Contains(newApproveCmd().Long, want) {
			t.Errorf("approve --help is missing %q", want)
		}
	}
	if !strings.Contains(newListCmd().Long, "APPROVE column") {
		t.Error("list --help does not explain the APPROVE column")
	}
	if !strings.Contains(newShowCmd().Long, "May approve") {
		t.Error("show --help does not explain the May approve line")
	}
}

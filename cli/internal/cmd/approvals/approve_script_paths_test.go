// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const secondID = "22222222-2222-2222-2222-222222222222"

// manyRun is the result of one `meho approvals approve <id> <id> ...` run.
type manyRun struct {
	stdout, stderr string
	err            error
	decided        []string
}

// runMany runs the several-ids approve against a mocked MEHO that serves
// views. stdin is what the person types; terminal says whether the input
// counts as a terminal.
func runMany(t *testing.T, views []api.ApprovalRequestView, ids []string, stdin string, terminal bool) manyRun {
	t.Helper()
	var run manyRun
	old := inputIsTerminal
	inputIsTerminal = func(io.Reader) bool { return terminal }
	t.Cleanup(func() { inputIsTerminal = old })
	routes := map[string]mockHandler{}
	for i := range views {
		v := views[i]
		id := v.Id.String()
		routes["GET /api/v1/approvals/"+id] = func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, v)
		}
		routes["POST /api/v1/approvals/"+id+"/decide"] = func(w http.ResponseWriter, _ *http.Request) {
			run.decided = append(run.decided, id)
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{ApprovalRequestId: v.Id, Decision: "approved"})
		}
	}
	srv := mockBackplane(t, routes)
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
	cmd := &cobra.Command{Use: "approve"}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetContext(context.Background())
	run.err = runApproveMany(cmd, ids, "", false, srv.URL)
	run.stdout, run.stderr = stdout.String(), stderr.String()
	return run
}

// twoScriptViews returns two pending requests that run a script whose
// SHA-256 is testScriptSHA.
func twoScriptViews(t *testing.T) []api.ApprovalRequestView {
	first, second := shaScriptView(t), shaScriptView(t)
	second.Id = uuid.MustParse(secondID)
	return []api.ApprovalRequestView{first, second}
}

func TestSeveralIDsApproveOfScriptsTakesTheMatchingSHAOnAPipe(t *testing.T) {
	run := runMany(t, twoScriptViews(t), []string{stubID, secondID}, testScriptSHA+"\n"+testScriptSHA+"\n", false)
	if run.err != nil || len(run.decided) != 2 {
		t.Fatalf("err = %v, decided = %v; want both approved\nstderr:\n%s", run.err, run.decided, run.stderr)
	}
	for _, want := range []string{
		"SHA-256:          " + testScriptSHA,
		"Type yes, or the script's SHA-256, to approve request 1 of 2. Anything else skips it: ",
		"Type yes, or the script's SHA-256, to approve request 2 of 2. Anything else skips it: ",
	} {
		if !strings.Contains(run.stderr, want) {
			t.Errorf("stderr misses %q:\n%s", want, run.stderr)
		}
	}
}

func TestSeveralIDsApproveOfScriptsRefusesYesOrAWrongSHAOnAPipe(t *testing.T) {
	wrong := strings.Repeat("f", 64)
	run := runMany(t, twoScriptViews(t), []string{stubID, secondID}, "yes\n"+wrong+"\n", false)
	if len(run.decided) != 0 {
		t.Fatalf("decided = %v; want nothing approved", run.decided)
	}
	for _, want := range []string{
		"yes is only taken from a terminal. On a pipe, give the script's SHA-256.",
		"That SHA-256 does not match the script of this request.",
		"Approved 0 of 2 request(s).",
	} {
		if !strings.Contains(run.stdout, want) {
			t.Errorf("stdout misses %q:\n%s", want, run.stdout)
		}
	}
}

func TestSeveralIDsApproveOfScriptsTakesYesFromATerminal(t *testing.T) {
	run := runMany(t, twoScriptViews(t), []string{stubID, secondID}, "yes\nno\n", true)
	if len(run.decided) != 1 || run.decided[0] != stubID {
		t.Fatalf("decided = %v; want only the first request", run.decided)
	}
}

func TestSeveralIDsApproveWithoutAScriptStillTakesYesOnAPipe(t *testing.T) {
	plain := newApprovalView(t, "pending", "")
	second := newApprovalView(t, "pending", "")
	second.Id = uuid.MustParse(secondID)
	run := runMany(t, []api.ApprovalRequestView{plain, second}, []string{stubID, secondID}, "yes\nyes\n", false)
	if run.err != nil || len(run.decided) != 2 {
		t.Fatalf("err = %v, decided = %v; want both approved", run.err, run.decided)
	}
}

func TestSeveralIDsApproveRefusesTheSameIDTwice(t *testing.T) {
	run := runMany(t, twoScriptViews(t), []string{stubID, secondID, strings.ToUpper(stubID)}, strings.Repeat(testScriptSHA+"\n", 3), false)
	assertExitCode(t, run.err, 1)
	if len(run.decided) != 0 {
		t.Fatalf("decided = %v; want nothing approved", run.decided)
	}
	if !strings.Contains(run.stderr, "is given twice") || strings.Contains(run.stderr, "Type yes") {
		t.Errorf("stderr:\n%s", run.stderr)
	}
}

func TestInOrderStepThatRunsAScriptTakesTheMatchingSHAOnAPipe(t *testing.T) {
	old := inputIsTerminal
	inputIsTerminal = func(io.Reader) bool { return false }
	t.Cleanup(func() { inputIsTerminal = old })
	rows := []groupRow{
		{status: "pending", level: "caution", dispatch: "ok"},
		{status: "pending", level: "dangerous", dispatch: "ok", script: true},
	}
	rec, _, stderr, err := runInOrderWithMock(t, rows, "yes\n"+testScriptSHA+"\n", false)
	if err != nil || len(rec.decided) != 2 {
		t.Fatalf("err = %v, decided = %v; want both steps approved\nstderr:\n%s", err, rec.decided, stderr)
	}
	if !strings.Contains(stderr, "Type yes, or the script's SHA-256, to approve step 2 and go on. Anything else stops here: ") {
		t.Errorf("stderr:\n%s", stderr)
	}
}

func TestInOrderStepThatRunsAScriptStopsOnYesFromAPipe(t *testing.T) {
	old := inputIsTerminal
	inputIsTerminal = func(io.Reader) bool { return false }
	t.Cleanup(func() { inputIsTerminal = old })
	rows := []groupRow{
		{status: "pending", level: "dangerous", dispatch: "ok", script: true},
		{status: "pending", level: "caution", dispatch: "ok"},
	}
	rec, stdout, _, err := runInOrderWithMock(t, rows, yesAll, false)
	assertExitCode(t, err, 4)
	if len(rec.decided) != 0 {
		t.Fatalf("decided = %v; want nothing approved", rec.decided)
	}
	if !strings.Contains(stdout, "yes is only taken from a terminal") {
		t.Errorf("stdout does not say why it stopped:\n%s", stdout)
	}
}

func TestInOrderStepThatRunsAScriptTakesYesFromATerminal(t *testing.T) {
	old := inputIsTerminal
	inputIsTerminal = func(io.Reader) bool { return true }
	t.Cleanup(func() { inputIsTerminal = old })
	rows := []groupRow{{status: "pending", level: "dangerous", dispatch: "ok", script: true}}
	rec, _, _, err := runInOrderWithMock(t, rows, "yes\n", false)
	if err != nil || len(rec.decided) != 1 {
		t.Fatalf("err = %v, decided = %v; want the step approved", err, rec.decided)
	}
}

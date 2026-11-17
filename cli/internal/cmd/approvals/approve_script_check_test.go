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

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// oneIDRun is the result of one `meho approvals <verb> <id>` run.
type oneIDRun struct {
	stdout, stderr string
	err            error
	decides        int
}

// runOneID runs the one-id approve or reject against a mocked MEHO whose
// request is view. stdin is what the person types; terminal says whether
// the input counts as a terminal.
func runOneID(t *testing.T, verb string, view api.ApprovalRequestView, stdin string, terminal bool) oneIDRun {
	t.Helper()
	var run oneIDRun
	old := inputIsTerminal
	inputIsTerminal = func(io.Reader) bool { return terminal }
	t.Cleanup(func() { inputIsTerminal = old })
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			v := view
			if run.decides > 0 {
				v.Status = api.ApprovalRequestStatusApproved
			}
			writeJSON(t, w, http.StatusOK, v)
		},
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, _ *http.Request) {
			run.decides++
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{ApprovalRequestId: mustUUID(t, stubID), Decision: "approved"})
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
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetContext(context.Background())
	run.err = runDecision(cmd, stubID, verb, "", decisionOpts{BackplaneOverride: srv.URL})
	run.stdout, run.stderr = stdout.String(), stderr.String()
	return run
}

func shaScriptView(t *testing.T) api.ApprovalRequestView {
	return scriptRunView(t, map[string]interface{}{
		"script":        "echo hi\n",
		"script_bytes":  float64(8),
		"script_sha256": testScriptSHA,
		"script_ref":    "app-repo@abc123:check.sh",
		"script_check":  "matches",
	})
}

func hiddenScriptView(t *testing.T) api.ApprovalRequestView {
	return scriptRunView(t, map[string]interface{}{
		"script":               "export DB_[REDACTED:api_key]\n",
		"script_bytes":         float64(40),
		"script_values_hidden": true,
	})
}

func TestOneIDApproveOfAScriptAsksAndYesFromATerminalApproves(t *testing.T) {
	run := runOneID(t, "approve", shaScriptView(t), "yes\n", true)
	if run.err != nil || run.decides != 1 {
		t.Fatalf("err = %v, decides = %d; want approved", run.err, run.decides)
	}
	for _, want := range []string{
		"Size:             8 bytes",
		"SHA-256:          " + testScriptSHA,
		"Reviewed copy:    app-repo@abc123:check.sh",
		"Expected SHA-256: matches",
		"meho approvals show " + stubID,
		"approvals page of the console",
		"Type yes, or the script's SHA-256, to approve. Anything else stops: ",
	} {
		if !strings.Contains(run.stderr, want) {
			t.Errorf("stderr misses %q:\n%s", want, run.stderr)
		}
	}
	if strings.Contains(run.stderr, "echo hi") || strings.Contains(run.stdout, "Type yes") {
		t.Errorf("the block shows the script text, or the question went to stdout:\n%s\n%s", run.stderr, run.stdout)
	}
}

func TestOneIDApproveOfAScriptTakesTheMatchingSHAOnAPipe(t *testing.T) {
	for _, typed := range []string{testScriptSHA + "\n", "  " + strings.ToUpper(testScriptSHA) + "  \n", testScriptSHA} {
		run := runOneID(t, "approve", shaScriptView(t), typed, false)
		if run.err != nil || run.decides != 1 {
			t.Errorf("%q: err = %v, decides = %d; want approved", typed, run.err, run.decides)
		}
	}
}

func TestOneIDApproveOfAScriptRefusesAWrongSHA(t *testing.T) {
	wrong := strings.Repeat("f", 64)
	for _, terminal := range []bool{false, true} {
		run := runOneID(t, "approve", shaScriptView(t), wrong+"\n", terminal)
		if run.err == nil || run.decides != 0 {
			t.Fatalf("terminal=%v: err = %v, decides = %d; want an error and no decision", terminal, run.err, run.decides)
		}
		if !strings.Contains(run.stderr, "does not match the script of this request. Nothing was approved.") {
			t.Errorf("stderr:\n%s", run.stderr)
		}
	}
}

func TestOneIDApproveOfAScriptStopsOnAnythingElse(t *testing.T) {
	cases := map[string]struct {
		stdin    string
		terminal bool
	}{
		"yes on a pipe":    {"yes\n", false},
		"no on a terminal": {"no\n", true},
		"no on a pipe":     {"no\n", false},
		"no answer":        {"", true},
		"no answer, pipe":  {"", false},
		"empty line":       {"\n", true},
	}
	for name, tc := range cases {
		run := runOneID(t, "approve", shaScriptView(t), tc.stdin, tc.terminal)
		if run.err == nil || run.decides != 0 {
			t.Errorf("%s: err = %v, decides = %d; want an error and no decision", name, run.err, run.decides)
		}
		if !strings.Contains(run.stderr, "Nothing was approved") {
			t.Errorf("%s: stderr says nothing plain:\n%s", name, run.stderr)
		}
	}
}

func TestOneIDApproveOfAScriptWithHiddenValuesNeedsYesFromATerminal(t *testing.T) {
	run := runOneID(t, "approve", hiddenScriptView(t), "yes\n", true)
	if run.err != nil || run.decides != 1 {
		t.Fatalf("yes from a terminal: err = %v, decides = %d", run.err, run.decides)
	}
	if !strings.Contains(run.stderr, "SHA-256:          none. Values that look like secrets were hidden") {
		t.Errorf("the block does not say why there is no SHA-256:\n%s", run.stderr)
	}
	for _, tc := range []struct {
		stdin    string
		terminal bool
	}{{"yes\n", false}, {testScriptSHA + "\n", true}, {"no\n", true}, {"", true}} {
		run := runOneID(t, "approve", hiddenScriptView(t), tc.stdin, tc.terminal)
		if run.err == nil || run.decides != 0 {
			t.Errorf("%+v: err = %v, decides = %d; want an error and no decision", tc, run.err, run.decides)
		}
	}
}

func TestOneIDApproveOfAScriptMEHOCouldNotCheckExplainsIt(t *testing.T) {
	view := scriptRunView(t, map[string]interface{}{"script_unavailable": "the script could not be read"})
	run := runOneID(t, "approve", view, "yes\n", true)
	if run.err != nil || run.decides != 1 {
		t.Fatalf("err = %v, decides = %d", run.err, run.decides)
	}
	if !strings.Contains(run.stderr, "MEHO could not check the script: the script could not be read") {
		t.Errorf("stderr:\n%s", run.stderr)
	}
}

func TestOneIDApproveWithoutAScriptAsksNothing(t *testing.T) {
	run := runOneID(t, "approve", newApprovalView(t, "pending", ""), "", false)
	if run.err != nil || run.decides != 1 {
		t.Fatalf("err = %v, decides = %d; want approved without a question", run.err, run.decides)
	}
	if run.stderr != "" {
		t.Errorf("stderr must stay empty without a script:\n%s", run.stderr)
	}
}

func TestOneIDApproveDoesNotAskForARequestThatIsNotPending(t *testing.T) {
	view := shaScriptView(t)
	view.Status = api.ApprovalRequestStatusApproved
	run := runOneID(t, "approve", view, "", false)
	if strings.Contains(run.stderr, "Type yes") {
		t.Errorf("asked about a request that is not pending:\n%s", run.stderr)
	}
}

func TestRejectOfAScriptAsksNothing(t *testing.T) {
	run := runOneID(t, "reject", shaScriptView(t), "", false)
	if run.err != nil || run.decides != 1 {
		t.Fatalf("err = %v, decides = %d; want rejected without a question", run.err, run.decides)
	}
	if strings.Contains(run.stderr, "Type yes") {
		t.Errorf("reject asked a question:\n%s", run.stderr)
	}
}

func TestOneIDApproveFailsWhenItCannotReadTheRequest(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, _ *http.Request) {
			t.Errorf("a decision was sent although the request could not be read")
		},
	})
	t.Cleanup(srv.Close)
	stubSignIn(t)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	store, _ := auth.NewTokenStore()
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{AccessToken: "test-bearer", BackplaneURL: srv.URL}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	cmd := &cobra.Command{Use: "approve"}
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetIn(strings.NewReader("yes\n"))
	cmd.SetContext(context.Background())
	if err := runDecision(cmd, stubID, "approve", "", decisionOpts{BackplaneOverride: srv.URL}); err == nil {
		t.Fatal("want an error when the request cannot be read")
	}
}

func TestApproveHelpDescribesTheScriptStep(t *testing.T) {
	long := newApproveCmd().Long
	for _, want := range []string{
		"Type yes, or the script's SHA-256, to approve",
		"printf '%s\\n' \"$REVIEWED_SHA256\" | meho approvals approve <id>",
		"There is no flag that skips the question",
	} {
		if !strings.Contains(long, want) {
			t.Errorf("approve --help misses %q", want)
		}
	}
}

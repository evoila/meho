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

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// runResultWithMock runs `meho approvals result` against a mocked
// backplane with a stored token and the --backplane override.
func runResultWithMock(t *testing.T, handler mockHandler, jsonOut bool) (stdout, stderr *bytes.Buffer, err error) {
	t.Helper()
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals/" + stubID + "/result": handler,
	})
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	store, serr := auth.NewTokenStore()
	if serr != nil {
		t.Fatalf("NewTokenStore: %v", serr)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if serr := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: srv.URL,
	}); serr != nil {
		t.Fatalf("store.Save: %v", serr)
	}
	cmd := &cobra.Command{Use: "result"}
	stdout, stderr = &bytes.Buffer{}, &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetContext(context.Background())
	return stdout, stderr, runResult(cmd, stubID, jsonOut, srv.URL)
}

// resultView builds the answer of GET /result for a status and an
// optional stored result envelope.
func resultView(t *testing.T, status string, result map[string]interface{}) api.ApprovalResultView {
	t.Helper()
	v := api.ApprovalResultView{
		ApprovalRequestId: mustUUID(t, stubID),
		Status:            api.ApprovalRequestStatus(status),
		Resumed:           result != nil,
	}
	if result != nil {
		v.Result = &result
	}
	return v
}

func answer(t *testing.T, v api.ApprovalResultView) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, v)
	}
}

func assertContains(t *testing.T, out string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q; got:\n%s", want, out)
		}
	}
}

// TestResultPrintsOutputOfApprovedRun: after approval the sender sees
// what the run printed, with one command, and exit 0.
func TestResultPrintsOutputOfApprovedRun(t *testing.T) {
	v := resultView(t, "approved", map[string]interface{}{
		"status":      "ok",
		"op_id":       "linux.script.run",
		"result":      map[string]interface{}{"exit_code": 0, "stdout": "free: 42G"},
		"audit_id":    "44444444-4444-4444-4444-444444444444",
		"duration_ms": 812.0,
	})
	stdout, stderr, err := runResultWithMock(t, answer(t, v), false)
	if err != nil {
		t.Fatalf("runResult: %v (stderr=%s)", err, stderr)
	}
	assertContains(t, stdout.String(),
		"Run: linux.script.run — status=ok (812ms)",
		`"stdout": "free: 42G"`,
		"44444444-4444-4444-4444-444444444444",
	)
}

// TestResultFailedRunExitsOne: a stored result that failed is printed
// like a failed `meho operation call` and exits 1.
func TestResultFailedRunExitsOne(t *testing.T) {
	v := resultView(t, "approved", map[string]interface{}{
		"status":      "error",
		"op_id":       "linux.script.run",
		"error":       "script exited 2",
		"duration_ms": 10.0,
	})
	stdout, _, err := runResultWithMock(t, answer(t, v), false)
	if !errors.Is(err, errRunNotOK) {
		t.Fatalf("want errRunNotOK, got %v", err)
	}
	assertContains(t, stdout.String(), "status=error", "script exited 2")
}

// TestResultNamesResultQueryForHandle: a large result comes back as a
// result handle; the output names the command that reads it.
func TestResultNamesResultQueryForHandle(t *testing.T) {
	v := resultView(t, "approved", map[string]interface{}{
		"status":      "ok",
		"op_id":       "vmware.vm.list",
		"handle":      map[string]interface{}{"handle_id": "h-123", "row_count": 900},
		"duration_ms": 5.0,
	})
	stdout, _, err := runResultWithMock(t, answer(t, v), false)
	if err != nil {
		t.Fatalf("runResult: %v", err)
	}
	assertContains(t, stdout.String(), "result handle:", "meho operation result-query h-123")
}

// TestResultWithoutStoredRunGivesClearMessage: a waiting, rejected,
// expired or withdrawn request, or an approved one whose run stored
// nothing yet, says so in plain words. No error, nothing on stderr.
func TestResultWithoutStoredRunGivesClearMessage(t *testing.T) {
	for status, want := range map[string]string{
		"pending":   "still waits for approval",
		"approved":  "has not stored a result",
		"rejected":  "rejected, so the operation did not run",
		"expired":   "expired before anyone decided",
		"withdrawn": "withdrew the request",
	} {
		t.Run(status, func(t *testing.T) {
			stdout, stderr, err := runResultWithMock(t, answer(t, resultView(t, status, nil)), false)
			if err != nil {
				t.Fatalf("runResult: %v", err)
			}
			if stderr.Len() != 0 {
				t.Errorf("stderr must stay empty; got %q", stderr.String())
			}
			assertContains(t, stdout.String(), "→ "+status, want)
		})
	}
}

// TestResultNotTheSenderIsClearMessage: the server answers 403
// not_request_owner to anyone but the sender. The CLI shows that as a
// clear insufficient_role message with exit 5.
func TestResultNotTheSenderIsClearMessage(t *testing.T) {
	forbidden := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"not_request_owner"}`))
	}
	_, stderr, err := runResultWithMock(t, forbidden, true)
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeInsufficientRole,
		"only the person who sent this request can read its result")
	assertExitCode(t, err, output.ExitInsufficientRole)
}

// TestResultJSONPassesAnswerThrough: --json prints the server's answer.
func TestResultJSONPassesAnswerThrough(t *testing.T) {
	stdout, _, err := runResultWithMock(t, answer(t, resultView(t, "pending", nil)), true)
	if err != nil {
		t.Fatalf("runResult: %v", err)
	}
	assertContains(t, stdout.String(), `"status": "pending"`, `"result": null`)
}

// TestPrintDetailNamesResultCommandWhenRunDone: `meho approvals show`
// names the result command once the run is done, and not before.
func TestPrintDetailNamesResultCommandWhenRunDone(t *testing.T) {
	d := failedRunView(t)
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	assertContains(t, stdout.String(), "Run output:   meho approvals result "+stubID)

	pending := newApprovalView(t, "pending", "")
	cmd, stdout, _ = newCapturingCmd(t)
	printDetail(cmd, &pending)
	if strings.Contains(stdout.String(), "meho approvals result") {
		t.Errorf("pending request must not name the result command:\n%s", stdout.String())
	}
}

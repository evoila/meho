// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// runDecisionWithMock runs `approve` or `reject` against a mocked
// backplane with --json, a stored token, and the --backplane override.
func runDecisionWithMock(t *testing.T, decideHandler mockHandler, verb, reason string) (*bytes.Buffer, error) {
	t.Helper()
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": decideHandler,
	})
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: srv.URL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	cmd := &cobra.Command{Use: verb}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetContext(context.Background())
	return &stderr, runDecision(cmd, stubID, verb, reason, true, srv.URL)
}

// json422 answers 422 with a JSON body and the JSON content type, the
// way the real server does.
func json422(body string) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(body))
	}
}

func assertExitCode(t *testing.T, err error, want int) {
	t.Helper()
	var ec output.ExitCoder
	if !errors.As(err, &ec) {
		t.Fatalf("error %T does not carry an exit code", err)
	}
	if got := ec.ExitCode(); got != want {
		t.Errorf("exit code: got %d want %d", got, want)
	}
}

// TestDecide422ShowsServerText checks that a 422 from the decide route
// prints the server's own text as unexpected_response (exit 4), for
// both detail shapes and both verbs. Before #5134 a string detail sent
// with the JSON content type was reported as unreachable (exit 3).
func TestDecide422ShowsServerText(t *testing.T) {
	const reasonMsg = "delegated_reason_invalid: delegated decision reason is invalid: " +
		"the Instructed-by:, Instruction: and Refs: lines must be one block " +
		"of 'Key: value' lines at the end of the reason, after a blank line"
	cases := []struct {
		name, body, want string
	}{
		{"string detail", fmt.Sprintf(`{"detail":%q}`, reasonMsg), reasonMsg},
		{
			"list detail",
			`{"detail":[{"loc":["body","decision"],"msg":"decision must be approved or rejected","type":"value_error"}]}`,
			"decision must be approved or rejected",
		},
	}
	for _, verb := range []string{"approve", "reject"} {
		for _, tc := range cases {
			t.Run(verb+" "+tc.name, func(t *testing.T) {
				stderr, err := runDecisionWithMock(t, json422(tc.body), verb,
					"Ran the cleanup.\nInstructed-by: lead@example.com\nInstruction: \"go\"\nRefs: #12")
				assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, tc.want)
				assertExitCode(t, err, 4)
			})
		}
	}
}

// TestDecideUnreadableBodyIsUnexpected checks that a body the CLI
// cannot decode maps to unexpected_response, not unreachable, the same
// as the kb package.
func TestDecideUnreadableBodyIsUnexpected(t *testing.T) {
	stderr, err := runDecisionWithMock(t, json422(`{"detail":42}`), "approve", "")
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "")
	assertExitCode(t, err, 4)
}

// TestDecideHelpStatesReasonLayout checks that the help of approve and
// reject explains the layout the server accepts for the three lines.
func TestDecideHelpStatesReasonLayout(t *testing.T) {
	for _, cmd := range []*cobra.Command{newApproveCmd(), newRejectCmd()} {
		for _, want := range []string{
			"Free text may come first",
			"an empty line must separate it",
			"single Key: value line",
			"real line breaks",
			"Instructed-by: <who>",
			"Instruction: \"<their words>\"",
			"Refs: <issue or change ref>",
		} {
			if !strings.Contains(cmd.Long, want) {
				t.Errorf("%s --help is missing %q", cmd.Name(), want)
			}
		}
	}
}

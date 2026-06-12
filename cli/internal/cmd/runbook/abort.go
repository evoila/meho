// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAbortRunCmd returns the `meho runbook abort` command.
//
// CLI shape (per issue #1319):
//
//	meho runbook abort <run_id> [--reason "<text>"] [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/runs/{run_id}/abort. Role: operator
// (assignee) OR any tenant_admin -- the backend's caller_is_admin
// widening admits a senior cleaning up someone else's stuck run.
//
// --reason is required (the backend's Field(min_length=1) rejects
// an empty reason at the wire). When --reason is missing AND stdin
// is a TTY, the CLI prompts interactively (mirrors the `meho kb
// delete` pattern). When --reason is missing AND stdin is NOT a
// TTY (scripted use without an answer), exit 1 with a useful
// message rather than blocking on stdin or sending an empty
// reason.
//
// Exit codes:
//   - 0   run aborted (200)
//   - 1   --reason missing and stdin not a TTY (operator error)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 400 run_already_terminal,
//     404 run_not_found)
//   - 5   insufficient_role (403 not_run_assignee)
func newAbortRunCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "abort <run_id>",
		Short: "Abort an in-progress runbook run (assignee or tenant_admin)",
		Long: "abort calls POST /api/v1/runbooks/runs/{run_id}/abort " +
			"to mark the run abandoned with the supplied reason. The " +
			"reason is persisted to audit_log for senior review (per " +
			"Initiative #1198's abort-with-audit guarantee).\n\n" +
			"Permitted callers: the run's assignee, OR any tenant_admin " +
			"(the admin path is the senior taking over to clean up). " +
			"Operators who aren't the assignee and aren't admins get " +
			"403.\n\n" +
			"--reason is required. When omitted and stdin is a TTY, the " +
			"CLI prompts for one; when omitted and stdin is not a TTY, " +
			"exit 1 -- scripted callers must supply --reason explicitly.\n\n" +
			"This is the only way out of a 'failed' step. There is no " +
			"force_advance, no skip; substrate refuses to advance over " +
			"an unverified step (#1177).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runAbortRun(cmd, abortRunOptions{
				RunID:             args[0],
				Reason:            reason,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "",
		"non-empty reason persisted to audit_log (required; prompts if omitted on a TTY)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AbortRunResponse JSON instead of the human confirmation")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type abortRunOptions struct {
	RunID             string
	Reason            string
	JSONOut           bool
	BackplaneOverride string
}

// errAbortMissingReasonNonTTY is returned when stdin isn't a TTY and
// --reason wasn't supplied. The CLI maps this to a structured error
// that exits 1 -- operator misuse, not a transport / auth failure
// (those reserve exit codes 2-5). 1 is the conventional "caller
// error" exit per the issue body's "structured exit codes" reference.
//
// Exposed as an exported sentinel pattern through the
// structured-error envelope rather than a bare `errors.New` so the
// stderr text stays consistent across the verb tree.
var errAbortMissingReasonNonTTY = output.Unexpected(
	"abort --reason is required when stdin is not a TTY; pass --reason \"<text>\"",
)

// abortExitCode1 wraps the structured error so cobra's RunE chain
// surfaces exit code 1 on the non-TTY path. The output package's
// ExitCoder interface routes any error with ExitCode() to the
// process exit; we wrap to override the default exit (Unexpected
// emits 4) to the conventional caller-error 1.
type abortExitCode1 struct{ inner error }

func (a *abortExitCode1) Error() string { return a.inner.Error() }
func (a *abortExitCode1) ExitCode() int { return 1 }
func (a *abortExitCode1) Unwrap() error { return a.inner }

func runAbortRun(cmd *cobra.Command, opts abortRunOptions) error {
	if opts.RunID == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("abort requires a non-empty <run_id> argument"),
			opts.JSONOut,
		)
	}
	runID, err := uuid.Parse(opts.RunID)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid run_id %q: %v", opts.RunID, err)),
			opts.JSONOut,
		)
	}
	reason := strings.TrimSpace(opts.Reason)
	if reason == "" {
		if !stdinIsTTY() {
			// Non-TTY without --reason. Emit the structured-error
			// envelope so callers see the same shape as other CLI
			// errors, but wrap so the exit code is 1 (caller error)
			// rather than 4 (unexpected_response). See
			// errAbortMissingReasonNonTTY for the message text.
			_ = output.RenderError(cmd.ErrOrStderr(), errAbortMissingReasonNonTTY, opts.JSONOut)
			return &abortExitCode1{inner: errAbortMissingReasonNonTTY}
		}
		prompted, perr := promptForReason(cmd, opts.RunID)
		if perr != nil {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(perr.Error()), opts.JSONOut)
		}
		reason = prompted
	}
	if reason == "" {
		// The interactive prompt accepted an empty answer (operator
		// hit enter). Treat the same as the non-TTY path: the backend
		// would reject an empty reason at 422 anyway; fast-fail with
		// a clear local error.
		_ = output.RenderError(cmd.ErrOrStderr(), errAbortMissingReasonNonTTY, opts.JSONOut)
		return &abortExitCode1{inner: errAbortMissingReasonNonTTY}
	}
	backplaneURL, berr := backplane.Resolve(opts.BackplaneOverride)
	if berr != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(berr), opts.JSONOut)
	}
	resp, rerr := postAbortRun(cmd.Context(), backplaneURL, runID, reason)
	if rerr != nil {
		return renderRequestError(cmd, backplaneURL, rerr, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without an AbortRunResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	state := "abandoned"
	if resp.JSON200.State != nil {
		state = *resp.JSON200.State
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"Aborted run %s (state=%s, abandoned_at=%s)\n",
		resp.JSON200.RunId,
		state,
		resp.JSON200.AbandonedAt.UTC().Format("2006-01-02T15:04:05Z"),
	)
	return nil
}

// promptForReason prompts on stdin for the abort reason and returns
// the trimmed answer. EOF (closed stdin) returns the empty string;
// the caller maps the empty answer to the non-TTY error path.
//
// Honours cmd.InOrStdin() so tests can wire a bytes.Buffer (the
// stdinIsTTY package-level var is overridden separately to claim
// TTY presence). Mirrors `confirmPrompt` in
// `cli/internal/cmd/kb/kb.go` for the EOF-as-no read pattern,
// except we return the string itself rather than a yes/no verdict.
func promptForReason(cmd *cobra.Command, runID string) (string, error) {
	fmt.Fprintf(cmd.OutOrStdout(),
		"Aborting run %s. Reason (recorded to audit_log): ", runID)
	reader := bufio.NewReader(cmd.InOrStdin())
	line, err := reader.ReadString('\n')
	if errors.Is(err, io.EOF) && line == "" {
		// Empty stdin on what looked like a TTY shouldn't fall back
		// to the non-TTY error path silently -- but the prompt path
		// can't recover either. Treat as a clean empty answer; the
		// caller (runAbortRun) maps that to the abortExitCode1 path
		// for consistent exit-code handling across both routes.
		return "", nil
	}
	if err != nil && !errors.Is(err, io.EOF) {
		return "", fmt.Errorf("read reason: %w", err)
	}
	return strings.TrimSpace(line), nil
}

func postAbortRun(
	ctx context.Context,
	backplaneURL string,
	runID uuid.UUID,
	reason string,
) (*api.AbortRunApiV1RunbooksRunsRunIdAbortPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := api.AbortRunRequest{Reason: reason}
	params := &api.AbortRunApiV1RunbooksRunsRunIdAbortPostParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.AbortRunApiV1RunbooksRunsRunIdAbortPostResponse, error) {
			return authed.AbortRunApiV1RunbooksRunsRunIdAbortPostWithResponse(
				ctx, runID, params, body,
			)
		},
		func(r *api.AbortRunApiV1RunbooksRunsRunIdAbortPostResponse) int { return r.StatusCode() },
	)
}

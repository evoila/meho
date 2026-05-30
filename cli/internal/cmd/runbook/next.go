// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bufio"
	"context"
	"encoding/json"
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

// newNextRunCmd returns the `meho runbook next` command.
//
// CLI shape (per issue #1319):
//
//	meho runbook next <run_id> [--verify-response yes|no|escalate]
//	  [--json] [--backplane URL]
//
// Wraps POST /api/v1/runbooks/runs/{run_id}/next. Role: operator
// (assignee). The substrate enforces single-assignee at the service
// layer -- a caller other than the run's `assigned_to` (including a
// tenant_admin) gets 403; the right path for a senior to take over
// is `meho runbook reassign`.
//
// THE LOAD-BEARING VERB. Two non-trivial concerns layered on the
// thin HTTP wrapper:
//
//  1. Interactive verify prompt. When --verify-response is omitted
//     AND the substrate's first answer is 422 VerifyResponseRequiredError
//     (indicating a confirm-typed verify), the CLI prompts the
//     operator on stdin (yes/no/escalate) and re-issues the call
//     with the answer. The substrate is the verify oracle; the
//     prompt is operator UX, not a security boundary.
//  2. Opacity rendering. Whether the response is the next step's
//     body or the RunCompletedResponse marker, the CLI renders ONLY
//     the current step (or the completion banner) -- never a list,
//     never a future-step preview.
//
// The first non-error path (200 with current_step) renders the next
// step body via the shared renderCurrentStep helper, which only
// reads fields under stepBodyDTO. Test #5
// (TestRunNextOpacityRendering) is the regression catch: even if
// the backend response somehow carried other step ids, the CLI
// output would only ever display the current step.
//
// Exit codes:
//   - 0   step advanced + rendered, or run completed
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 400 previous_step_failed /
//     run_already_terminal, 404 run_not_found,
//     422 verify_response_mismatch)
//   - 5   insufficient_role (403 not_run_assignee)
func newNextRunCmd() *cobra.Command {
	var (
		verifyResponse    string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "next <run_id>",
		Short: "Advance an in-progress runbook run by one step",
		Long: "next calls POST /api/v1/runbooks/runs/{run_id}/next " +
			"to advance the run one step. The substrate is the verify " +
			"oracle: a step transitions to verified (and the run " +
			"advances) when the verify predicate matches; otherwise " +
			"the step transitions to failed and the only forward path " +
			"is `meho runbook abort`.\n\n" +
			"--verify-response yes|no|escalate supplies the answer " +
			"for a confirm-typed verify directly (scripted use). " +
			"Without the flag, `next` calls the substrate first; if " +
			"the substrate replies VerifyResponseRequiredError, the " +
			"CLI prompts on stdin and re-issues with the answer.\n\n" +
			"For operation_call-typed verifies, the substrate dispatches " +
			"the verify call itself; the CLI displays the match/mismatch " +
			"verdict and the next step body (or the completion banner).\n\n" +
			"OPACITY: only the current step is rendered. A `next` after " +
			"the last step returns the RunCompletedResponse marker, " +
			"which the CLI surfaces as `Run complete.` and exit 0.\n\n" +
			"SINGLE-ASSIGNEE: you can only advance a run you own. A " +
			"tenant_admin who is not the assignee gets 403; the right " +
			"path is `meho runbook reassign` followed by `next`.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNextRun(cmd, nextRunOptions{
				RunID:             args[0],
				VerifyResponse:    verifyResponse,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&verifyResponse, "verify-response", "",
		"answer for a confirm-typed verify: yes|no|escalate (omit to prompt interactively)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw NextStepResponse JSON instead of the human block")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type nextRunOptions struct {
	RunID             string
	VerifyResponse    string
	JSONOut           bool
	BackplaneOverride string
}

// validVerifyAnswers is the closed set the substrate accepts.
// Mirrors backend.runbooks.runs_schemas.ConfirmVerifyResponse's
// Literal["yes", "no", "escalate"]; the CLI validates locally so a
// typo lands as a clean error rather than a 422 round-trip.
var validVerifyAnswers = map[string]struct{}{
	"yes":      {},
	"no":       {},
	"escalate": {},
}

func runNextRun(cmd *cobra.Command, opts nextRunOptions) error {
	if opts.RunID == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("next requires a non-empty <run_id> argument"),
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
	answer := strings.TrimSpace(opts.VerifyResponse)
	if answer != "" {
		if _, ok := validVerifyAnswers[answer]; !ok {
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"--verify-response must be one of yes, no, escalate; got %q",
					opts.VerifyResponse,
				)),
				opts.JSONOut,
			)
		}
	}
	backplaneURL, berr := backplane.Resolve(opts.BackplaneOverride)
	if berr != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(berr), opts.JSONOut)
	}

	// First call: send the operator-supplied answer (if any), or nil
	// to let the substrate either dispatch the operation_call verify
	// or surface VerifyResponseRequiredError on a confirm step we
	// then prompt for.
	body, fcode, ferr := postNext(cmd.Context(), backplaneURL, runID, answer)
	if ferr != nil {
		return renderRequestError(cmd, backplaneURL, ferr, opts.JSONOut)
	}

	// The 422 VerifyResponseRequiredError branch is the
	// interactive-prompt seam (per issue #1319's decision-tree
	// section, "pick (C) -- error-as-control-flow"). We only enter
	// the prompt path when:
	//   - the substrate said 422,
	//   - the error detail indicates a verify response was required
	//     (not, e.g., a mismatch -- those mean the operator's
	//     supplied answer was wrong-shape, which is a re-prompt
	//     loop a CLI shouldn't drive without operator intent),
	//   - the operator did not pass --verify-response (we don't
	//     prompt over a supplied answer; that's the scripted path
	//     and we'd be drowning out the operator's explicit flag).
	if fcode == http.StatusUnprocessableEntity && answer == "" && verifyResponseRequired(body) {
		return promptAndRetryConfirm(cmd, backplaneURL, runID, body, opts)
	}
	if fcode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, fcode, body, opts.JSONOut)
	}
	return renderNextResponse(cmd, backplaneURL, body, opts)
}

// postNext issues one POST to /next with the given verify-response
// answer ("" means no verify_response; the substrate will either
// dispatch an operation_call verify or surface
// VerifyResponseRequiredError).
//
// Returns the body bytes verbatim, the status code, and any
// transport error. We bypass the generated typed parser
// (ParseAdvanceRunApiV1RunbooksRunsRunIdNextPostResponse) because:
//
//  1. The 200 body is a discriminated union (kind=current_step |
//     completed); the generated client lifts it into an anonymous
//     struct with an unexported `union json.RawMessage` field, so
//     we'd need to re-marshal the raw bytes to read the
//     discriminator anyway. decodeNextStepResponse already does
//     that on the raw bytes.
//  2. The 422 body is FastAPI HTTPException(detail=str(exc)) --
//     `{"detail": "<string>"}` -- which the generated parser
//     rejects with a json.UnmarshalTypeError because the OpenAPI
//     schema declares 422 as the validation-error list shape.
//     Bypassing the typed parser lets the 422 body reach
//     verifyResponseRequired's probe verbatim.
//
// The function reads the response body exactly once (defer Close)
// and surfaces the bytes to the caller for both probing and
// rendering.
func postNext(
	ctx context.Context,
	backplaneURL string,
	runID uuid.UUID,
	answer string,
) ([]byte, int, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, err
	}
	body := buildNextRequestBody(answer)
	params := &api.AdvanceRunApiV1RunbooksRunsRunIdNextPostParams{}
	// retryOn401 is generic over the typed response envelope;
	// reuse it for the 401-refresh dance by routing through the
	// raw *http.Response variant the generated client exposes, then
	// pack it into a tiny shim that exposes StatusCode() the
	// retry helper inspects.
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*rawNextResponse, error) {
			httpResp, herr := authed.AdvanceRunApiV1RunbooksRunsRunIdNextPost(
				ctx, runID, params, body,
			)
			if herr != nil {
				return nil, herr
			}
			defer func() { _ = httpResp.Body.Close() }()
			bodyBytes, rerr := io.ReadAll(io.LimitReader(httpResp.Body, responseBodyCap))
			if rerr != nil {
				return nil, fmt.Errorf("read /next response body: %w", rerr)
			}
			return &rawNextResponse{
				status: httpResp.StatusCode,
				body:   bodyBytes,
			}, nil
		},
		func(r *rawNextResponse) int { return r.status },
	)
	if err != nil {
		return nil, 0, err
	}
	return resp.body, resp.status, nil
}

// rawNextResponse adapts an unparsed raw HTTP response to the
// retryOn401 helper's "has a StatusCode() int" expectation. The
// retry helper only reads the status code; it never decodes the
// body. This keeps the 422-with-string-detail body (which the
// generated typed parser would reject) reachable in the calling
// function.
type rawNextResponse struct {
	status int
	body   []byte
}

// buildNextRequestBody constructs the NextStepRequest payload. The
// `last_verified` field is informational only (the substrate is the
// oracle); we set it to true whenever the operator supplies any
// answer (or runs the operation_call verify path), false when
// neither holds. The substrate's gating is unchanged either way --
// the field exists so the wire log captures the operator's belief
// alongside the substrate's verdict.
func buildNextRequestBody(answer string) api.NextStepRequest {
	if answer == "" {
		// Either the substrate will dispatch an operation_call
		// verify itself, or this is the first call where no prior
		// step exists. Either way we send last_verified=false so
		// the wire log accurately reflects "no claim".
		return api.NextStepRequest{LastVerified: false}
	}
	// The operator answered the confirm prompt. Wire shape:
	//   verify_response: {"type": "confirm", "answer": "yes"|"no"|"escalate"}
	// Encoded through the generated union envelope by marshalling
	// the JSON directly into NextStepRequest_VerifyResponse.union.
	vr := makeConfirmVerifyResponse(answer)
	return api.NextStepRequest{
		LastVerified:   true,
		VerifyResponse: &vr,
	}
}

// makeConfirmVerifyResponse builds the discriminated-union payload
// the generated client expects -- the union field is unexported, so
// we marshal the {type, answer} object via the FromConfirmVerifyResponse
// helper which the codegen emits for OpenAPI oneOf members.
func makeConfirmVerifyResponse(answer string) api.NextStepRequest_VerifyResponse {
	var vr api.NextStepRequest_VerifyResponse
	// FromConfirmVerifyResponse is the codegen-generated setter
	// that marshals a ConfirmVerifyResponse into the union's
	// internal RawMessage. Error path is impossible for the inputs
	// we feed it (a struct with two string fields cannot fail
	// json.Marshal); we swallow it deliberately rather than
	// percolating an error through the public surface for an
	// unreachable case.
	_ = vr.FromConfirmVerifyResponse(api.ConfirmVerifyResponse{
		Type:   "confirm",
		Answer: api.ConfirmVerifyResponseAnswer(answer),
	})
	return vr
}

// verifyResponseRequired probes the 422 body for the substrate's
// VerifyResponseRequiredError signature. We match on the detail
// string ("VerifyResponseRequiredError" emitted by str(exc) in the
// runbook_runs.py route handler -- see _http_for and the engine's
// exception classname). The probe is intentionally string-shaped
// rather than parsing a structured envelope so a backend that
// renames the field but keeps the exception classname continues to
// work.
//
// The fall-back tolerates the FastAPI validation-error shape that
// 422 sometimes carries (a list of {loc, msg, type} dicts) -- a
// real "missing verify response" should mention the message text;
// anything else is a different 422 (verify_response_mismatch,
// missing_params) and should NOT enter the prompt loop.
func verifyResponseRequired(body []byte) bool {
	trim := strings.TrimSpace(string(body))
	if trim == "" {
		return false
	}
	if strings.Contains(trim, "VerifyResponseRequired") {
		return true
	}
	// FastAPI HTTPException(detail=str(exc)) shape: {"detail": "<...>"}
	var env struct {
		Detail any `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err == nil {
		if s, ok := env.Detail.(string); ok && strings.Contains(s, "VerifyResponseRequired") {
			return true
		}
	}
	return false
}

// promptAndRetryConfirm runs the interactive confirm prompt. Called
// only after the substrate returned 422 VerifyResponseRequiredError
// and the operator did NOT supply --verify-response on the command
// line. Reads stdin via cmd.InOrStdin(), validates against the
// closed set {yes, no, escalate}, re-prompts on invalid input,
// re-issues POST /next with the answer.
//
// EOF (closed stdin / piped /dev/null) is treated as the operator
// abandoning the prompt -- we surface a clean error rather than
// hanging or defaulting to a particular answer. Scripted callers
// should pass --verify-response explicitly.
func promptAndRetryConfirm(
	cmd *cobra.Command,
	backplaneURL string,
	runID uuid.UUID,
	body []byte,
	opts nextRunOptions,
) error {
	// The substrate surfaces VerifyResponseRequiredError without
	// the original step's prompt text (the engine's exception
	// carries only the run/step coordinates). The prompt we show
	// is a generic verify instruction; the operator already saw
	// the prompt text on the prior `start` / `next` call when the
	// step body was rendered (renderCurrentStep prints the Prompt
	// line for confirm-typed verifies). Repeating it here would
	// require the CLI to either re-fetch the run state or have
	// kept the prompt across invocations -- both are bigger seams
	// than the issue scopes.
	fmt.Fprintln(cmd.OutOrStdout(), "Verify required: this step has a confirm-typed verify.")
	answer, perr := readVerifyAnswer(cmd)
	if perr != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(perr.Error()), opts.JSONOut)
	}
	retryBody, code, rerr := postNext(cmd.Context(), backplaneURL, runID, answer)
	if rerr != nil {
		return renderRequestError(cmd, backplaneURL, rerr, opts.JSONOut)
	}
	if code != http.StatusOK {
		// Don't recurse into the prompt loop on a second 422 -- a
		// second VerifyResponseRequiredError after a valid answer
		// means the run's state machine is in an unexpected place
		// (the assignee got reassigned mid-prompt, the step
		// concurrently transitioned to failed). Surface the body
		// verbatim and exit.
		return renderHTTPStatus(cmd, backplaneURL, code, retryBody, opts.JSONOut)
	}
	_ = body
	return renderNextResponse(cmd, backplaneURL, retryBody, opts)
}

// readVerifyAnswer reads one line from cmd.InOrStdin(), trims
// whitespace, lowercases, and validates against the closed set.
// Re-prompts up to 3 times on invalid input -- past that, surface
// the error so an automation hooked to a malformed input doesn't
// loop indefinitely.
func readVerifyAnswer(cmd *cobra.Command) (string, error) {
	in := cmd.InOrStdin()
	reader := bufio.NewReader(in)
	const maxAttempts = 3
	for attempt := 0; attempt < maxAttempts; attempt++ {
		fmt.Fprint(cmd.OutOrStdout(), "Answer [yes/no/escalate]: ")
		line, err := reader.ReadString('\n')
		if errors.Is(err, io.EOF) && line == "" {
			return "", fmt.Errorf("verify answer: stdin closed without input; pass --verify-response for scripted use")
		}
		if err != nil && !errors.Is(err, io.EOF) {
			return "", fmt.Errorf("read verify answer: %w", err)
		}
		answer := strings.ToLower(strings.TrimSpace(line))
		if _, ok := validVerifyAnswers[answer]; ok {
			return answer, nil
		}
		fmt.Fprintf(cmd.OutOrStdout(), "  invalid answer %q; expected yes, no, or escalate\n", answer)
	}
	return "", fmt.Errorf("verify answer: too many invalid attempts (3); pass --verify-response yes|no|escalate to retry non-interactively")
}

// renderNextResponse routes the 200 response between the
// current_step and completed shapes. JSON mode prints the raw body;
// table mode prints either the next step body (via the shared
// renderCurrentStep) or the completion banner.
func renderNextResponse(
	cmd *cobra.Command,
	backplaneURL string,
	body []byte,
	opts nextRunOptions,
) error {
	current, completed, err := decodeNextStepResponse(body)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: decode AdvanceRun response: %v", backplaneURL, err,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		// Emit whichever variant was returned -- the CLI doesn't
		// re-wrap the union'd shape; consumers parse on `kind`.
		if current != nil {
			return output.PrintJSON(cmd.OutOrStdout(), current)
		}
		if completed != nil {
			return output.PrintJSON(cmd.OutOrStdout(), completed)
		}
	}
	if current != nil {
		renderCurrentStep(cmd.OutOrStdout(), current)
		return nil
	}
	if completed != nil {
		fmt.Fprintf(cmd.OutOrStdout(), "Run complete. (run_id=%s, state=%s)\n",
			completed.RunID, completed.State)
		if completed.CompletedAt != "" {
			fmt.Fprintf(cmd.OutOrStdout(), "Completed at: %s\n", completed.CompletedAt)
		}
		return nil
	}
	// decodeNextStepResponse returned no error but neither variant
	// -- impossible unless the substrate started emitting a third
	// kind. Be explicit instead of falling through silently.
	return output.RenderError(
		cmd.ErrOrStderr(),
		output.Unexpected(fmt.Sprintf(
			"call %s: HTTP 200 with neither current_step nor completed payload",
			backplaneURL,
		)),
		opts.JSONOut,
	)
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newStartRunCmd returns the `meho runbook start` command.
//
// CLI shape (per issue #1319):
//
//	meho runbook start <slug> --target <name> [--param k=v ...] [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/runs. Role: operator. The caller is
// auto-assigned as the run's assignee server-side; only the assignee
// can advance the run via `meho runbook next` (per Initiative #1198's
// single-assignee discipline).
//
// Default output: a heading-formatted block — the run_id, the run
// coordinates (template slug + version + position), and the current
// step's body and verify gate. `--json` emits the raw
// CurrentStepResponse envelope for jq pipelines.
//
// OPACITY CONTRACT (load-bearing per issue #1319 AC + parent #1313):
// `start` returns the first step's body and nothing else. The CLI
// renders only fields under the response's `current_step` key — even
// if a future backend bug leaked future-step bodies into the
// envelope, the rendering function would still display only the
// current step. The opacity contract is enforced structurally at the
// substrate (`StepBody` shape, #1300); the CLI verifies it at the
// human surface.
//
// Exit codes:
//   - 0   run started, step 1 rendered
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 400 deprecated_template,
//     404 slug_not_found, 422 missing_params)
//   - 5   insufficient_role
func newStartRunCmd() *cobra.Command {
	var (
		target            string
		params            []string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "start <slug>",
		Short: "Start a new runbook run (operator)",
		Long: "start calls POST /api/v1/runbooks/runs to begin a new run " +
			"on the latest non-deprecated published version of <slug>. " +
			"You are auto-assigned as the run's assignee server-side; " +
			"only you (or a tenant_admin via `meho runbook reassign`) " +
			"can advance the run via `meho runbook next`.\n\n" +
			"--target is required: the run subject (the host, cluster, " +
			"cert thumbprint, ...) substituted into the template body " +
			"as ${run.target}.\n\n" +
			"--param k=v sets a value for the ${run.params.k} " +
			"substitution context. Repeat for multiple params. Every " +
			"${run.params.X} the template references must be satisfied " +
			"at start time -- a missing key surfaces as 422.\n\n" +
			"Output: run_id + step 1 body with substitutions applied. " +
			"If the step's verify is type=confirm, the prompt text is " +
			"shown so you know whether to answer yes/no/escalate on " +
			"the next call; if type=operation_call, the op_id is shown " +
			"so you know what the substrate will dispatch.\n\n" +
			"OPACITY: only step 1 is rendered. Future steps are " +
			"opaque-by-construction at the substrate; the CLI replicates " +
			"that discipline at the human surface.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runStartRun(cmd, startRunOptions{
				Slug:              args[0],
				Target:            target,
				Params:            params,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&target, "target", "",
		"required: run subject (host, cluster, cert thumbprint) -- substituted as ${run.target}")
	cmd.Flags().StringArrayVar(&params, "param", nil,
		"k=v substitution context entry for ${run.params.k}; repeat for multiple params")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw CurrentStepResponse JSON instead of the human block")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type startRunOptions struct {
	Slug              string
	Target            string
	Params            []string
	JSONOut           bool
	BackplaneOverride string
}

func runStartRun(cmd *cobra.Command, opts startRunOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("start requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if strings.TrimSpace(opts.Target) == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("start requires --target (the run subject substituted as ${run.target})"),
			opts.JSONOut,
		)
	}
	params, err := parseParamFlags(opts.Params)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, berr := backplane.Resolve(opts.BackplaneOverride)
	if berr != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(berr), opts.JSONOut)
	}
	body := api.StartRunRequest{
		TemplateSlug: opts.Slug,
		Target:       opts.Target,
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	respBody, status, rerr := postStartRun(cmd.Context(), backplaneURL, body)
	if rerr != nil {
		return renderRequestError(cmd, backplaneURL, rerr, opts.JSONOut)
	}
	// The substrate returns 201 on a fresh run with a
	// `CurrentStepResponse` body (kind=current_step). The
	// discriminated-union response in the spec also allows
	// `RunCompletedResponse`, but the service's start path always
	// returns the current_step variant (see
	// backend/src/meho_backplane/api/v1/runbook_runs.py:start_run
	// docstring) -- a fresh run is never terminal. We still defer
	// to the wire payload for the kind discriminator so a future
	// backend behaviour shift surfaces as a render-time error, not
	// a silent skip.
	if status != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, status, respBody, opts.JSONOut)
	}
	current, _, decodeErr := decodeNextStepResponse(respBody)
	if decodeErr != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: decode StartRun response: %v", backplaneURL, decodeErr,
			)),
			opts.JSONOut,
		)
	}
	if current == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a current_step payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), current)
	}
	renderStartHeader(cmd.OutOrStdout(), current)
	renderCurrentStep(cmd.OutOrStdout(), current)
	return nil
}

// postStartRun bypasses the generated typed-response parser for the
// same reasons postNext does (see next.go's docstring): the 201
// body is a discriminated union with an unexported union field,
// and 422 FastAPI HTTPException bodies don't fit the
// HTTPValidationError shape. Reading the raw bytes once keeps both
// paths working through a single render layer.
func postStartRun(
	ctx context.Context,
	backplaneURL string,
	body api.StartRunRequest,
) ([]byte, int, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, err
	}
	params := &api.StartRunApiV1RunbooksRunsPostParams{}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*rawNextResponse, error) {
			httpResp, herr := authed.StartRunApiV1RunbooksRunsPost(ctx, params, body)
			if herr != nil {
				return nil, herr
			}
			defer func() { _ = httpResp.Body.Close() }()
			bodyBytes, rerr := io.ReadAll(io.LimitReader(httpResp.Body, responseBodyCap))
			if rerr != nil {
				return nil, fmt.Errorf("read /runs response body: %w", rerr)
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

// renderStartHeader prints the run coordinates -- the run_id, the
// template slug + version, the start position. Pulled out from
// `renderCurrentStep` so `runbook next` can reuse the body renderer
// without re-printing the coordinates (next just prints "Step n/total"
// and the body).
//
// Operators who scripted around the run_id parse `Run ID: <uuid>` as
// the first line; `--json` callers parse the JSON envelope.
func renderStartHeader(w io.Writer, r *startResponseView) {
	fmt.Fprintf(w, "Run ID:      %s\n", r.RunID)
	fmt.Fprintf(w, "Template:    %s@%d\n", r.TemplateSlug, r.TemplateVersion)
	fmt.Fprintln(w)
}

// parseParamFlags parses the repeated --param k=v flag values into a
// map suitable for the StartRunRequest.params field. Each entry must
// contain at least one `=`; the first `=` is the separator (so values
// may contain `=`). Empty keys are rejected. The values are passed
// through verbatim as strings -- the substrate's substitution engine
// only consumes ${run.params.X} placeholders as plain text, so a
// scoped CLI surface that JSON-parses values would invite shape
// confusion (`--param count=5` would silently become an int, then
// fail substitution into a string field).
//
// Returns nil (not an empty map) when no params were supplied so the
// caller can omit the field from the JSON body -- the backend's
// `params` field is optional and defaults to {} server-side.
func parseParamFlags(raw []string) (map[string]interface{}, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	out := make(map[string]interface{}, len(raw))
	for _, pair := range raw {
		eq := strings.IndexByte(pair, '=')
		if eq < 0 {
			return nil, fmt.Errorf("--param %q must be in k=v form", pair)
		}
		key := strings.TrimSpace(pair[:eq])
		value := pair[eq+1:]
		if key == "" {
			return nil, fmt.Errorf("--param %q has an empty key", pair)
		}
		out[key] = value
	}
	return out, nil
}

// startResponseView is the narrow projection of the
// StartRunApiV1RunbooksRunsPost / AdvanceRunApiV1RunbooksRunsRunIdNextPost
// 200/201 response that the CLI's rendering layer reads from. Built by
// decodeNextStepResponse from the wire JSON; the decoder discards any
// field paths not enumerated here, so even a backend that injected
// future-step contents into the envelope cannot leak them into the
// human surface (the opacity property the AC pins via test #5).
//
// Kind is always "current_step" when this struct is non-nil; the
// terminal-state variant has its own type (runCompletedView).
type startResponseView struct {
	RunID           string          `json:"run_id"`
	TemplateSlug    string          `json:"template_slug"`
	TemplateVersion int             `json:"template_version"`
	Position        stepPositionDTO `json:"position"`
	CurrentStep     stepBodyDTO     `json:"current_step"`
}

// runCompletedView is the terminal-state shape returned by `next`
// when the previous step was the last. Carries the run coordinates
// and the transition timestamp -- no step body (the run is done).
type runCompletedView struct {
	RunID       string `json:"run_id"`
	State       string `json:"state"`
	CompletedAt string `json:"completed_at"`
}

// stepPositionDTO models the {n, total} position hint.
type stepPositionDTO struct {
	N     int `json:"n"`
	Total int `json:"total"`
}

// stepBodyDTO is the narrow projection of the StepBody fields the
// CLI actually renders. The opacity property in the issue body
// (#1319 AC + test #5) requires that the CLI never display fields
// other than these for the current step -- and never reach for a
// "list of all steps" or "next step" field at all. This struct's
// shape IS the contract: any field not enumerated here is not
// rendered, even if the backend sent it.
//
// `Verify` is intentionally a sub-struct (not a json.RawMessage)
// because the renderer surfaces the verify type + prompt / op_id
// inline -- not the substituted params or expect dictionary (those
// are tool-side details the operator doesn't need at the prompt).
type stepBodyDTO struct {
	ID     string             `json:"id"`
	Title  string             `json:"title"`
	Body   string             `json:"body"`
	Type   string             `json:"type"`
	OpID   *string            `json:"op_id,omitempty"`
	Params *map[string]any    `json:"params,omitempty"`
	Verify *stepBodyVerifyDTO `json:"verify,omitempty"`
}

// stepBodyVerifyDTO is the verify-gate projection the renderer reads.
// `Prompt` populated only on confirm; `OpID` populated only on
// operation_call. The decoder leaves the others at zero so the
// renderer can switch cleanly on `Type`.
type stepBodyVerifyDTO struct {
	Type   string  `json:"type"`
	Prompt *string `json:"prompt,omitempty"`
	OpID   *string `json:"op_id,omitempty"`
}

// decodeNextStepResponse decodes the discriminated-union body shared
// by the start (201) and next (200) routes into either a
// startResponseView (kind=current_step) or a runCompletedView
// (kind=completed). The kind discriminator is read first; the
// payload is then re-unmarshalled into the matching projection.
//
// Returns (nil, nil, nil) on an unknown / missing kind so the caller
// can surface "unexpected_response" via output.Unexpected -- a silent
// fall-through to one of the two views would mask a backend that
// landed on a third response shape unannounced (the
// no-third-response-shape contract per #1313 _NEXT_DESCRIPTION).
//
// The decoder is verbatim-shape: it does not silently drop fields,
// it does not coalesce missing optional fields. The opacity
// property is enforced by the struct definition (stepBodyDTO has
// no future-step field path); the decoder just routes on kind.
func decodeNextStepResponse(body []byte) (*startResponseView, *runCompletedView, error) {
	var head struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(body, &head); err != nil {
		return nil, nil, fmt.Errorf("read kind discriminator: %w", err)
	}
	switch head.Kind {
	case "current_step":
		var v startResponseView
		if err := json.Unmarshal(body, &v); err != nil {
			return nil, nil, fmt.Errorf("decode current_step: %w", err)
		}
		return &v, nil, nil
	case "completed":
		var v runCompletedView
		if err := json.Unmarshal(body, &v); err != nil {
			return nil, nil, fmt.Errorf("decode completed: %w", err)
		}
		return nil, &v, nil
	case "":
		return nil, nil, fmt.Errorf("missing kind discriminator")
	default:
		return nil, nil, fmt.Errorf("unknown kind discriminator %q", head.Kind)
	}
}

// renderCurrentStep prints the single-step body in the operator-
// facing format. ONE step body, with substitutions already applied
// (the engine resolved ${run.target} / ${run.params.X} server-side
// per #1301). The renderer reads only fields defined on stepBodyDTO
// -- this struct's shape IS the opacity contract at the human
// surface (regression-tested in start_test.go's opacity tests).
//
// Output shape:
//
//	Step n/total: <title>  (id: <step.id>)
//	─────────────────────────────────────────
//	<body — verbatim, substitutions already applied>
//	─────────────────────────────────────────
//	Verify type: <verify.type>
//	[Prompt: ...]  // confirm
//	[Will dispatch: <op_id>]  // operation_call
func renderCurrentStep(w io.Writer, r *startResponseView) {
	fmt.Fprintf(w, "Step %d/%d: %s  (id: %s)\n",
		r.Position.N, r.Position.Total, r.CurrentStep.Title, r.CurrentStep.ID)
	const rule = "─────────────────────────────────────────────"
	fmt.Fprintln(w, rule)
	for _, line := range strings.Split(strings.TrimRight(r.CurrentStep.Body, "\n"), "\n") {
		fmt.Fprintln(w, line)
	}
	fmt.Fprintln(w, rule)
	if r.CurrentStep.Type == "operation_call" && r.CurrentStep.OpID != nil {
		fmt.Fprintf(w, "Step kind:   operation_call (op_id: %s)\n", *r.CurrentStep.OpID)
	} else {
		fmt.Fprintf(w, "Step kind:   %s\n", r.CurrentStep.Type)
	}
	if r.CurrentStep.Verify == nil {
		return
	}
	fmt.Fprintf(w, "Verify type: %s\n", r.CurrentStep.Verify.Type)
	switch r.CurrentStep.Verify.Type {
	case "confirm":
		if r.CurrentStep.Verify.Prompt != nil && *r.CurrentStep.Verify.Prompt != "" {
			fmt.Fprintf(w, "  Prompt: %s\n", *r.CurrentStep.Verify.Prompt)
		}
		fmt.Fprintln(w, "  Next: `meho runbook next "+r.RunID+" --verify-response yes|no|escalate`")
	case "operation_call":
		if r.CurrentStep.Verify.OpID != nil {
			fmt.Fprintf(w, "  Will dispatch op_id: %s\n", *r.CurrentStep.Verify.OpID)
		}
		fmt.Fprintln(w, "  Next: `meho runbook next "+r.RunID+"` (substrate dispatches the verify call)")
	}
}

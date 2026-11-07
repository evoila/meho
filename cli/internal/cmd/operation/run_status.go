// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

const (
	// runCheckWaitSeconds is how long the backplane may hold one check
	// open under --wait. It stays well under the 60-second read limit
	// of common proxies, so no network limit cuts a check.
	runCheckWaitSeconds = 20
	// runCheckPause is the pause between two checks under --wait. It
	// keeps the loop slow against a backplane that answers at once.
	runCheckPause = 2 * time.Second
)

// runCheckPauseFor is the pause between checks. Tests set it to zero.
var runCheckPauseFor = runCheckPause

// newRunStatusCmd returns the `meho operation run-status` command.
//
//	meho operation run-status <run_id> [--wait] [--json] [--backplane <url>]
//
// Reads a background run started by `meho operation call --async` via
// GET /api/v1/operations/runs/{run_id}. The run is stored on the
// backplane, so this works from any terminal and after the call that
// started it has gone.
func newRunStatusCmd() *cobra.Command {
	var (
		wait              bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run-status <run_id>",
		Short: "Show the state and result of a background operation run",
		Long: "run-status reads a background run started by " +
			"`meho operation call --async`. It calls " +
			"GET /api/v1/operations/runs/{run_id}.\n\n" +
			"While the run waits or runs, it says so and when the run " +
			"started. When the run has ended, it prints the stored result " +
			"the same way `meho operation call` prints one. A run that " +
			"failed or was cancelled has no result: run-status prints the " +
			"reason. A run in the state `uncertain` lost its result: check " +
			"the target before you start the operation again.\n\n" +
			"--wait checks every few seconds until the run ends, then " +
			"prints the result. Each check is a short request, so a VPN or " +
			"proxy limit cannot cut the wait.\n\n" +
			"Exit codes: 0 = the run is still going, or its result is ok; " +
			"1 = the run failed, was cancelled, is uncertain, or its stored " +
			"result is not ok; 4 = the run id is unknown.\n\n" +
			"--json prints the server's answer as it is.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRunStatus(cmd, args[0], wait, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&wait, "wait", false,
		"check every few seconds until the run ends, then print the result")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the server's answer as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRunStatus(cmd *cobra.Command, idArg string, wait, jsonOut bool, backplaneOverride string) error {
	runID, err := uuid.Parse(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid <run_id>: %v", err)), jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	for {
		view, body, err := getRun(cmd.Context(), client, runID, wait)
		if err != nil {
			return renderRunError(cmd, backplaneURL, runID, err, jsonOut)
		}
		if wait && !runEnded(view.Status) {
			select {
			case <-cmd.Context().Done():
				return cmd.Context().Err()
			case <-time.After(runCheckPauseFor):
			}
			continue
		}
		return renderRun(cmd, view, body, jsonOut)
	}
}

// getRun reads the run once, with the one-shot 401 refresh. With
// longCheck the backplane may hold the check open for a few seconds.
// It also returns the raw answer: the generated type decodes the stored
// result into float64 values, which change large integers.
func getRun(
	ctx context.Context,
	client operationsAPI,
	runID uuid.UUID,
	longCheck bool,
) (*api.OperationRunStatusResponse, json.RawMessage, error) {
	params := &api.GetOperationRunStatusApiV1OperationsRunsHandleGetParams{}
	if longCheck {
		w := runCheckWaitSeconds
		params.WaitSeconds = &w
	}
	resp, err := client.GetOperationRunStatusApiV1OperationsRunsHandleGetWithResponse(ctx, runID, params)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.GetOperationRunStatusApiV1OperationsRunsHandleGetWithResponse(ctx, runID, params)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	if resp.JSON200 == nil {
		return nil, nil, fmt.Errorf("backplane returned 200 OK but no run in the answer")
	}
	return resp.JSON200, json.RawMessage(resp.Body), nil
}

func renderRunError(cmd *cobra.Command, backplaneURL string, runID uuid.UUID, err error, jsonOut bool) error {
	var ae *apiResponseError
	if errors.As(err, &ae) && ae.StatusCode == http.StatusNotFound {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("no operation run %s in your tenant", runID)), jsonOut)
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// runEnded reports whether the run is in a final state. An unknown
// state counts as ended, so --wait never loops on a state it cannot read.
func runEnded(s api.OperationRunStatus) bool {
	return s != api.OperationRunStatusPending && s != api.OperationRunStatusRunning
}

// renderRun prints the run. body is the server's raw answer: --json
// prints it, so numbers in it keep every digit.
func renderRun(cmd *cobra.Command, view *api.OperationRunStatusResponse, body json.RawMessage, jsonOut bool) error {
	w := cmd.OutOrStdout()
	// Only a run that stored a result has one to show. A failed or
	// cancelled run is never shown as a success, whatever else it holds.
	if view.Status == api.OperationRunStatusSucceeded && view.Result != nil {
		return renderStoredResult(cmd, view, body, jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(w, body); err != nil {
			return err
		}
	} else {
		printRunSummary(w, view)
	}
	switch view.Status {
	case api.OperationRunStatusPending, api.OperationRunStatusRunning:
		return nil
	case api.OperationRunStatusSucceeded:
		// The run finished but this caller cannot read its result.
		return nil
	default:
		return errOpError
	}
}

// renderStoredResult prints the stored OperationResult like `meho
// operation call` does. The run state `succeeded` only says a result
// was stored; the stored status decides the output and the exit code.
// The result is decoded from the raw answer, not from view.Result, so
// large integers keep every digit, as in `meho operation call`.
func renderStoredResult(
	cmd *cobra.Command,
	view *api.OperationRunStatusResponse,
	body json.RawMessage,
	jsonOut bool,
) error {
	var answer struct {
		Result CallResult `json:"result"`
	}
	if err := json.Unmarshal(body, &answer); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode stored result: %v", err)), jsonOut)
	}
	return renderCallOutcome(cmd, view.ConnectorId, view.OpId, &answer.Result, jsonOut, body)
}

func printRunSummary(w io.Writer, v *api.OperationRunStatusResponse) {
	fmt.Fprintf(w, "run %s — %s %s — state=%s\n",
		v.RunId, v.ConnectorId, v.OpId, output.SafeText(string(v.Status)))
	if v.TargetName != nil {
		fmt.Fprintf(w, "target: %s\n", output.SafeText(*v.TargetName))
	}
	switch v.Status {
	case api.OperationRunStatusPending:
		fmt.Fprintln(w, "The run waits to start. Check again in a moment, or use --wait.")
	case api.OperationRunStatusRunning:
		if v.StartedAt != nil {
			fmt.Fprintf(w, "The run is still going. It started at %s. Check again in a moment, or use --wait.\n",
				v.StartedAt.UTC().Format(time.RFC3339))
		} else {
			fmt.Fprintln(w, "The run is still going. Check again in a moment, or use --wait.")
		}
	case api.OperationRunStatusSucceeded:
		fmt.Fprintln(w, "The run ended, but no result is shown to you. "+
			"Only the person who started the run can read its result.")
	case api.OperationRunStatusFailed:
		fmt.Fprintln(w, "The run failed. It has no result.")
		printRunReason(w, v)
	case api.OperationRunStatusCancelled:
		fmt.Fprintln(w, "The run was cancelled. It has no result.")
		printRunReason(w, v)
	case api.OperationRunStatusUncertain:
		fmt.Fprintln(w, "MEHO lost the result of this run. The operation may or may not have finished. "+
			"Check the target before you start it again.")
		printRunReason(w, v)
	default:
		fmt.Fprintln(w, "The run is in a state this CLI does not know. The backplane may be newer.")
		printRunReason(w, v)
	}
	if v.EndedAt != nil {
		fmt.Fprintf(w, "ended at: %s\n", v.EndedAt.UTC().Format(time.RFC3339))
	}
}

func printRunReason(w io.Writer, v *api.OperationRunStatusResponse) {
	if v.Error != nil && *v.Error != "" {
		fmt.Fprintf(w, "reason: %s\n", output.SafeText(*v.Error))
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/cmd/operation"
	"github.com/evoila/meho/cli/internal/output"
)

// errRunNotOK makes `meho approvals result` exit 1 when the approved
// run stored a result whose status is not "ok", the same exit code
// `meho operation call` uses for a failed call. The result is already
// printed, so the error has no text of its own.
var errRunNotOK = errors.New("approved run status not ok")

// newResultCmd returns the `meho approvals result` command (#5313).
//
//	meho approvals result <id> [--json] [--backplane <url>]
//
// Reads the stored result of an approved run via
// GET /api/v1/approvals/{id}/result. Only the person who sent the
// request may read it; the server answers 403 for anyone else.
func newResultCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "result <id>",
		Short: "Show the output of an approved operation",
		Long: "result shows what an operation printed after a second person " +
			"approved it and MEHO ran it. It calls " +
			"GET /api/v1/approvals/{id}/result. Only the person who sent " +
			"the request can read its result.\n\n" +
			"When the request still waits for approval, was rejected, " +
			"expired or was withdrawn, or the run has not stored a result " +
			"yet, result says so and exits 0. When the run stored a " +
			"result, it is printed the same way `meho operation call` " +
			"prints one. A large result comes back as a result handle: read " +
			"it with `meho operation result-query <handle_id>`.\n\n" +
			"Exit codes: 0 = run ok or no result yet, 1 = the run failed, " +
			"5 = you are not the person who sent the request.\n\n" +
			"--json prints the server's answer as it is.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runResult(cmd, args[0], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw JSON answer instead of the human-readable view")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func runResult(cmd *cobra.Command, idArg string, jsonOut bool, backplaneOverride string) error {
	requestID, err := parseRequestID(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, jsonOut)
	if cerr != nil {
		return cerr
	}
	view, err := fetchResult(cmd.Context(), client, requestID)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) && he.statusCode == http.StatusForbidden {
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(
					"not_request_owner: only the person who sent this request can read its result"),
				jsonOut,
			)
		}
		return routeRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if view == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalResultView"),
			jsonOut,
		)
	}
	run, err := decodeRunResult(view)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode stored result: %v", err)), jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), view); err != nil {
			return err
		}
	} else {
		printResult(cmd.OutOrStdout(), idArg, view, run)
	}
	if run != nil && run.Status != "ok" {
		return errRunNotOK
	}
	return nil
}

// fetchResult drives GetApprovalResult with the same one-shot
// 401-retry shape as fetchDetail.
func fetchResult(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
) (*api.ApprovalResultView, error) {
	resp, err := client.GetApprovalResultApiV1ApprovalsRequestIdResultGetWithResponse(ctx, requestID, nil)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetApprovalResultApiV1ApprovalsRequestIdResultGetWithResponse(ctx, requestID, nil)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

// decodeRunResult turns the stored result envelope into the shape
// `meho operation call` renders. Returns nil when no result is stored.
func decodeRunResult(v *api.ApprovalResultView) (*operation.CallResult, error) {
	if v.Result == nil {
		return nil, nil
	}
	raw, err := json.Marshal(*v.Result)
	if err != nil {
		return nil, err
	}
	var r operation.CallResult
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

func printResult(w io.Writer, idArg string, v *api.ApprovalResultView, run *operation.CallResult) {
	if run == nil {
		fmt.Fprintf(w, "approval_request %s → %s\n", idArg, string(v.Status))
		fmt.Fprintln(w, noResultMessage(v))
		return
	}
	operation.PrintCallResult(w, "Run:", run.OpID, run)
	// A delivery=partial result already names the result-query command.
	partial := run.Delivery != nil && *run.Delivery == "partial"
	if id := operation.ResultHandleID(run.Handle); id != "" && !partial {
		fmt.Fprintf(w, "read the full result with `meho operation result-query %s`.\n", id)
	}
}

// noResultMessage says, in plain words, why a request has no stored
// result yet and whether waiting will help.
func noResultMessage(v *api.ApprovalResultView) string {
	switch v.Status {
	case api.ApprovalRequestStatusPending:
		return "No output yet: the request still waits for approval."
	case api.ApprovalRequestStatusApproved:
		return "No output yet: the request was approved, but the run has not stored a result. " +
			"It may still be running. Try again in a moment."
	case api.ApprovalRequestStatusRejected:
		return "No output: the request was rejected, so the operation did not run."
	case api.ApprovalRequestStatusExpired:
		return "No output: the request expired before anyone decided, so the operation did not run."
	case api.ApprovalRequestStatusWithdrawn:
		return "No output: the sender withdrew the request, so the operation did not run."
	default:
		return "No output is stored for this request."
	}
}

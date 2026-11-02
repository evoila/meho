// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bufio"
	"errors"
	"fmt"
	"io"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// Outcomes of one request of a several-ids approve.
const (
	manyApproved = "approved"
	manySkipped  = "skipped"
)

// manyResult is one request of a several-ids approve, for --json.
type manyResult struct {
	ID      string                   `json:"id"`
	Outcome string                   `json:"outcome"`
	Detail  string                   `json:"detail,omitempty"`
	Request *api.ApprovalRequestView `json:"request,omitempty"`
}

// runApproveMany approves several requests in one run, with one approval
// sign-in (#5515). It shows each request and asks for a yes for each
// one: each approval is a separate decision about one request. A request
// that is not pending, or that the person does not say yes to, is
// skipped. The run stops at the first request MEHO refuses.
func runApproveMany(cmd *cobra.Command, idArgs []string, reason string, jsonOut bool, backplaneOverride string) error {
	ids := make([]uuid.UUID, 0, len(idArgs))
	for _, a := range idArgs {
		id, err := parseRequestID(a)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
		}
		ids = append(ids, id)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, jsonOut)
	if cerr != nil {
		return cerr
	}

	ctx := cmd.Context()
	signIn := newApprovalSignIn(client, cmd.ErrOrStderr())
	in := bufio.NewReader(cmd.InOrStdin())
	body := api.DecideRequestBody{Decision: "approved"}
	if reason != "" {
		r := reason
		body.Reason = &r
	}
	results := make([]manyResult, 0, len(ids))
	for i, id := range ids {
		res := manyResult{ID: id.String()}
		detail, err := fetchDetail(ctx, client, id)
		if err != nil {
			return routeRequestError(cmd, backplaneURL, err, jsonOut)
		}
		if detail == nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView"),
				jsonOut)
		}
		switch {
		case detail.Status != api.ApprovalRequestStatusPending:
			res.Outcome, res.Detail = manySkipped, fmt.Sprintf("it is %s, not pending", detail.Status)
		default:
			showRequest(cmd.ErrOrStderr(), fmt.Sprintf("Request %d of %d:", i+1, len(ids)), detail)
			prompt := fmt.Sprintf("Type yes to approve request %d of %d. Anything else skips it: ", i+1, len(ids))
			if !askYes(in, cmd.ErrOrStderr(), prompt) {
				res.Outcome, res.Detail = manySkipped, "you did not type yes"
				break
			}
			if _, err := sendDecide(ctx, client, id, body, signIn, isDestructive(detail)); err != nil {
				printManySummary(cmd, results, jsonOut)
				var noBrowser *auth.NoBrowserError
				if errors.As(err, &noBrowser) {
					return output.RenderError(cmd.ErrOrStderr(), noBrowserError(noBrowser.URL), jsonOut)
				}
				return routeRequestError(cmd, backplaneURL, err, jsonOut)
			}
			res.Outcome = manyApproved
			res.Request, err = fetchDetail(ctx, client, id)
			if err != nil {
				return routeRequestError(cmd, backplaneURL, err, jsonOut)
			}
		}
		results = append(results, res)
		if !jsonOut {
			printManyResult(cmd.OutOrStdout(), res)
		}
	}
	printManySummary(cmd, results, jsonOut)
	return nil
}

func printManyResult(w io.Writer, res manyResult) {
	if res.Outcome == manyApproved && res.Request != nil {
		printDecision(w, res.ID, res.Request)
		return
	}
	fmt.Fprintf(w, "approval_request %s skipped: %s\n", res.ID, res.Detail)
}

// printManySummary prints the results: the JSON list, or one line with
// the counts.
func printManySummary(cmd *cobra.Command, results []manyResult, jsonOut bool) {
	if jsonOut {
		_ = output.PrintJSON(cmd.OutOrStdout(), results)
		return
	}
	approved := 0
	for _, r := range results {
		if r.Outcome == manyApproved {
			approved++
		}
	}
	fmt.Fprintf(cmd.OutOrStdout(), "\nApproved %d of %d request(s).\n", approved, len(results))
}

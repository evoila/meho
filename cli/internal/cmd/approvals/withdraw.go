// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"fmt"
	"io"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newWithdrawCmd returns the `meho approvals withdraw` command (#4987).
//
//	meho approvals withdraw <id> [--reason TEXT] [--json] [--backplane <url>]
//
// The sender takes back its own pending request via
// POST /api/v1/approvals/{id}/withdraw. This is not a decision, so any
// kind of principal may run it, but only for a request it sent.
func newWithdrawCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "withdraw <id>",
		Short: "Withdraw your own pending approval request",
		Long: "withdraw takes back an approval request that you sent and " +
			"that still waits for approval. The request then shows " +
			"\"withdrawn\", nobody can approve it any more, and the " +
			"operation does not run. MEHO writes one audit row for it. " +
			"Only the sender can withdraw a request (others get 403), " +
			"and only while it is pending (a closed request gets 409). " +
			"A person, an agent or a service account can each withdraw " +
			"its own request. --reason says why; it is stored with the " +
			"request and in the audit row.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runWithdraw(cmd, args[0], reason, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional reason for the withdrawal")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON response instead of summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func runWithdraw(
	cmd *cobra.Command,
	idArg, reason string,
	jsonOut bool,
	backplaneOverride string,
) error {
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
	detail, err := postWithdraw(cmd.Context(), client, requestID, reason)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if detail == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView after withdraw"),
			jsonOut,
		)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	printWithdrawal(cmd.OutOrStdout(), idArg, detail)
	return nil
}

// printWithdrawal renders the result: the new status, when, and why.
func printWithdrawal(w io.Writer, idArg string, detail *api.ApprovalRequestView) {
	fmt.Fprintf(w, "approval_request %s → %s (withdrawn by the sender)\n", idArg, string(detail.Status))
	if detail.WithdrawnAt != nil {
		fmt.Fprintf(w, "withdrawn at: %s\n", *detail.WithdrawnAt)
	}
	if detail.WithdrawReason != nil && *detail.WithdrawReason != "" {
		fmt.Fprintf(w, "reason:       %s\n", *detail.WithdrawReason)
	}
}

// postWithdraw calls POST /api/v1/approvals/{id}/withdraw. The route
// returns the updated request, so no second GET is needed (the sender
// may not have access to the approvals read routes). A non-2xx answer
// comes back as an `*httpResponseError`.
func postWithdraw(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	reason string,
) (*api.ApprovalRequestView, error) {
	body := api.WithdrawRequestBody{}
	if reason != "" {
		r := reason
		body.Reason = &r
	}
	resp, err := client.WithdrawApprovalRequestApiV1ApprovalsRequestIdWithdrawPostWithResponse(ctx, requestID, nil, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.WithdrawApprovalRequestApiV1ApprovalsRequestIdWithdrawPostWithResponse(ctx, requestID, nil, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

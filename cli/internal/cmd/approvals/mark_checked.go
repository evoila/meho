// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newMarkCheckedCmd returns the `meho approvals mark-checked` command (#5026).
//
//	meho approvals mark-checked <id> --outcome happened|did-not-happen --note TEXT [--json] [--backplane <url>]
//
// Records what a person found on the target of an approved run whose
// outcome MEHO does not know, via POST /api/v1/approvals/{id}/outcome-check.
// Nothing runs.
func newMarkCheckedCmd() *cobra.Command {
	var (
		outcome           string
		note              string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "mark-checked <id>",
		Short: "Record what you found on the target of a run with an unknown outcome",
		Long: "mark-checked records what you found when you checked the target " +
			"of an approved change whose outcome MEHO does not know (see " +
			"`meho approvals list --outcome-unknown`). --outcome is happened " +
			"or did-not-happen. --note says what you checked; it is required. " +
			"MEHO writes one audit row, keeps the original result, and runs " +
			"nothing. The request then leaves the outcome-unknown list. You " +
			"need the right to decide approvals (403 otherwise). A request " +
			"that is already checked, or that is not on the list, gets 409.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runMarkChecked(cmd, args[0], outcome, note, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&outcome, "outcome", "", "what you found: happened or did-not-happen (required)")
	cmd.Flags().StringVar(&note, "note", "", "what you checked (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON response instead of summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// checkVerdict turns the --outcome value into the verdict the backplane takes.
func checkVerdict(outcome string) (api.OutcomeCheckVerdict, error) {
	switch outcome {
	case "happened":
		return api.OutcomeCheckVerdictHappened, nil
	case "did-not-happen":
		return api.OutcomeCheckVerdictDidNotHappen, nil
	default:
		return "", fmt.Errorf("--outcome must be happened or did-not-happen; got %q", outcome)
	}
}

func runMarkChecked(
	cmd *cobra.Command,
	idArg, outcome, note string,
	jsonOut bool,
	backplaneOverride string,
) error {
	requestID, err := parseRequestID(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	verdict, err := checkVerdict(outcome)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	if strings.TrimSpace(note) == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--note is required: say what you checked"), jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, jsonOut)
	if cerr != nil {
		return cerr
	}
	detail, err := postOutcomeCheck(cmd.Context(), client, requestID, verdict, note)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if detail == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView after mark-checked"),
			jsonOut,
		)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	printOutcomeCheckResult(cmd.OutOrStdout(), idArg, detail)
	return nil
}

// printOutcomeCheckResult renders the recorded check.
func printOutcomeCheckResult(w io.Writer, idArg string, detail *api.ApprovalRequestView) {
	fmt.Fprintf(w, "approval_request %s marked as checked. MEHO ran nothing.\n", idArg)
	printOutcomeCheck(w, detail)
}

// printOutcomeCheck prints what a person recorded after they checked the
// target (#5026). Prints nothing for a request nobody checked.
func printOutcomeCheck(w io.Writer, d *api.ApprovalRequestView) {
	c := d.OutcomeCheck
	if c == nil {
		return
	}
	found := "it happened"
	if c.Verdict == api.OutcomeCheckVerdictDidNotHappen {
		found = "it did not happen"
	}
	fmt.Fprintf(w, "Checked:      %s\n", found)
	fmt.Fprintf(w, "Check note:   %s\n", output.SafeText(c.Note))
	fmt.Fprintf(w, "Checked by:   %s\n", principalLabel(c.CheckedBy, c.CheckedByName))
	fmt.Fprintf(w, "Checked at:   %s\n", output.SafeText(c.CheckedAt))
}

// postOutcomeCheck calls POST /api/v1/approvals/{id}/outcome-check. A
// non-2xx answer comes back as an `*httpResponseError`.
func postOutcomeCheck(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	verdict api.OutcomeCheckVerdict,
	note string,
) (*api.ApprovalRequestView, error) {
	body := api.OutcomeCheckRequestBody{Verdict: verdict, Note: note}
	resp, err := client.MarkApprovalOutcomeCheckedApiV1ApprovalsRequestIdOutcomeCheckPostWithResponse(ctx, requestID, nil, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.MarkApprovalOutcomeCheckedApiV1ApprovalsRequestIdOutcomeCheckPostWithResponse(ctx, requestID, nil, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

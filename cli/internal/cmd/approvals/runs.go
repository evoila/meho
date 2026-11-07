// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// runsHelp explains what a run approval is.
const runsHelp = "An automation add-on can ask MEHO to approve the operation list " +
	"of one automation run. You read the list and approve it once. Then each " +
	"call of that run that is on the list runs without a new approval; every " +
	"call still goes through policy and the audit log, and each audit row " +
	"names the run approval. A call that is not on the list waits for " +
	"approval as before. A destructive operation is never covered. You can " +
	"revoke a run approval while the run is going; it also ends when the run " +
	"ends or at its time limit."

// newRunsCmd returns `meho approvals runs`: run approvals (#4126).
func newRunsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "runs",
		Short:        "Approve the operation list of one automation run once (list / show / approve / revoke)",
		Long:         runsHelp,
		SilenceUsage: true,
	}
	cmd.AddCommand(newRunsListCmd(), newRunsShowCmd(), newRunsApproveCmd(), newRunsRevokeCmd())
	return cmd
}

func newRunsListCmd() *cobra.Command {
	var status, principal string
	var limit int
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List run approvals (default: pending ones)",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			url, client, err := runsClient(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			params := &api.ListRunApprovalsApiV1ServicePrincipalsRunApprovalsGetParams{Limit: &limit}
			if status != "all" {
				s := api.ListRunApprovalsApiV1ServicePrincipalsRunApprovalsGetParamsStatus(status)
				params.Status = &s
			}
			if principal != "" {
				params.PrincipalSub = &principal
			}
			resp, err := client.ListRunApprovalsApiV1ServicePrincipalsRunApprovalsGetWithResponse(cmd.Context(), params)
			if err != nil {
				return routeRunsError(cmd, url, err, jsonOut)
			}
			if resp.JSON200 == nil {
				return routeRunsError(cmd, url, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printRunApprovalList(cmd.OutOrStdout(), resp.JSON200.RunApprovals)
			return nil
		},
	}
	cmd.Flags().StringVar(&status, "status", "pending", "pending, approved, revoked, ended, expired or all")
	cmd.Flags().StringVar(&principal, "principal", "", "only the run approvals of this service principal (token subject)")
	cmd.Flags().IntVar(&limit, "limit", 100, "the most rows to show (1-500)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw RunApprovalListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func newRunsShowCmd() *cobra.Command {
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:           "show <run-approval-id>",
		Short:         "Show one run approval and its operation list",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseRunApprovalID(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			url, client, err := runsClient(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			run, err := fetchRunApproval(cmd.Context(), client, id)
			if err != nil {
				return routeRunsError(cmd, url, err, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), run)
			}
			printRunApproval(cmd.OutOrStdout(), run)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw RunApprovalRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func newRunsApproveCmd() *cobra.Command {
	var confirm, jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:   "approve <run-approval-id>",
		Short: "Approve the whole operation list of one run, once",
		Long: "approve shows the operation list of the run and asks you to type yes. " +
			"--confirm skips the question.\n\n" + approveSignInHelp,
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseRunApprovalID(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			url, client, err := runsClient(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			if !confirm {
				run, err := fetchRunApproval(cmd.Context(), client, id)
				if err != nil {
					return routeRunsError(cmd, url, err, jsonOut)
				}
				printRunApproval(cmd.OutOrStdout(), run)
				in := bufio.NewReader(cmd.InOrStdin())
				if !askYes(in, cmd.OutOrStdout(), "\nApprove every operation on this list for this run? Type yes: ") {
					fmt.Fprintln(cmd.OutOrStdout(), "not approved")
					return nil
				}
			}
			run, err := postRunApprove(cmd.Context(), client, id, newApprovalSignIn(client, cmd.ErrOrStderr()))
			var noBrowser *auth.NoBrowserError
			if errors.As(err, &noBrowser) {
				return output.RenderError(cmd.ErrOrStderr(), noBrowserError(noBrowser.URL), jsonOut)
			}
			if err != nil {
				return routeRunsError(cmd, url, err, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), run)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "run approval %s → %s\n", run.Id, output.SafeText(string(run.Status)))
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "approve without showing the list and asking")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw RunApprovalRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func newRunsRevokeCmd() *cobra.Command {
	var reason string
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:           "revoke <run-approval-id>",
		Short:         "Revoke a run approval; the next listed call waits for approval",
		Long:          "revoke ends a pending or approved run approval at once. Use it to turn down a request too.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseRunApprovalID(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			url, client, err := runsClient(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			body := api.RunApprovalRevoke{}
			if reason != "" {
				body.Reason = &reason
			}
			resp, err := client.RevokeRunApprovalApiV1ServicePrincipalsRunApprovalsRunApprovalIdRevokePostWithResponse(cmd.Context(), id, nil, body)
			if err != nil {
				return routeRunsError(cmd, url, err, jsonOut)
			}
			if resp.JSON200 == nil {
				return routeRunsError(cmd, url, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "run approval %s → %s\n", resp.JSON200.Id, output.SafeText(string(resp.JSON200.Status)))
			return nil
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional reason, stored with the revoke")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw RunApprovalRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// routeRunsError is routeRequestError with the right not-found text.
func routeRunsError(cmd *cobra.Command, url string, err error, jsonOut bool) error {
	var he *httpResponseError
	if errors.As(err, &he) && he.statusCode == http.StatusNotFound {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("run_approval_not_found"), jsonOut)
	}
	return routeRequestError(cmd, url, err, jsonOut)
}

func runsClient(cmd *cobra.Command, backplaneOverride string, jsonOut bool) (string, *api.AuthedClient, error) {
	url, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return "", nil, output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, err := newAuthedClient(cmd.Context(), cmd, url, jsonOut)
	if err != nil {
		return "", nil, err
	}
	return url, client, nil
}

func parseRunApprovalID(arg string) (uuid.UUID, error) {
	id, err := uuid.Parse(arg)
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("run-approval-id is not a valid UUID: %s", arg)
	}
	return id, nil
}

func fetchRunApproval(ctx context.Context, client *api.AuthedClient, id uuid.UUID) (*api.RunApprovalRead, error) {
	resp, err := client.ShowRunApprovalApiV1ServicePrincipalsRunApprovalsRunApprovalIdGetWithResponse(ctx, id, nil)
	if err != nil {
		return nil, err
	}
	if resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

// postRunApprove sends the approve with the token of the approval sign-in,
// never the stored login. When MEHO asks for a newer sign-in, it opens one
// new sign-in and tries once more.
func postRunApprove(ctx context.Context, client *api.AuthedClient, id uuid.UUID, signIn *approvalSignIn) (*api.RunApprovalRead, error) {
	post := func() (*api.ApproveRunApprovalApiV1ServicePrincipalsRunApprovalsRunApprovalIdApprovePostResponse, error) {
		token, err := signIn.bearer(ctx, false)
		if err != nil {
			return nil, err
		}
		withSignIn := func(_ context.Context, req *http.Request) error {
			req.Header.Set("Authorization", "Bearer "+token)
			return nil
		}
		return client.ApproveRunApprovalApiV1ServicePrincipalsRunApprovalsRunApprovalIdApprovePostWithResponse(ctx, id, nil, withSignIn)
	}
	resp, err := post()
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized && needsFreshSignIn(resp.HTTPResponse) {
		signIn.forget()
		if resp, err = post(); err != nil {
			return nil, err
		}
	}
	if resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	signIn.approved(false)
	return resp.JSON200, nil
}

func printRunApprovalList(w io.Writer, runs []api.RunApprovalRead) {
	if len(runs) == 0 {
		fmt.Fprintln(w, "no run approvals")
		return
	}
	fmt.Fprintf(w, "%-36s  %-8s  %-7s  %-24s  %s\n", "ID", "STATUS", "ENTRIES", "RUN", "PRINCIPAL")
	for _, run := range runs {
		fmt.Fprintf(w, "%-36s  %-8s  %-7d  %-24s  %s\n", run.Id, output.SafeText(string(run.Status)), len(run.Entries), output.SafeText(run.WorkRef), output.SafeText(run.PrincipalSub))
	}
}

func printRunApproval(w io.Writer, run *api.RunApprovalRead) {
	fmt.Fprintf(w, "Run approval: %s\n", run.Id)
	fmt.Fprintf(w, "  Status:       %s\n", output.SafeText(string(run.Status)))
	fmt.Fprintf(w, "  Run:          %s\n", output.SafeText(run.WorkRef))
	fmt.Fprintf(w, "  Requested by: %s\n", output.SafeText(run.PrincipalSub))
	fmt.Fprintf(w, "  What it does: %s\n", output.SafeText(run.Reason))
	fmt.Fprintf(w, "  Ends at:      %s\n", run.ExpiresAt.UTC().Format("2006-01-02T15:04:05Z07:00"))
	if run.ApprovedBySub != nil {
		fmt.Fprintf(w, "  Approved by:  %s\n", output.SafeText(*run.ApprovedBySub))
	}
	fmt.Fprintf(w, "  Operations (%d):\n", len(run.Entries))
	for _, entry := range run.Entries {
		fmt.Fprintf(w, "    - %s  %s  %s\n", output.SafeText(entry.ConnectorId), output.SafeText(entry.OpId), describeRunEntryTarget(entry))
	}
}

func describeRunEntryTarget(entry api.RunApprovalEntry) string {
	switch {
	case entry.TargetId != nil:
		return "target " + entry.TargetId.String()
	case entry.TargetProduct != nil:
		return "every target of kind " + output.SafeText(*entry.TargetProduct)
	default:
		return "no target"
	}
}

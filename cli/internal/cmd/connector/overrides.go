// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newOverridesCmd returns the `meho connector overrides` command (#4711).
//
// CLI shape:
//
//	meho connector overrides [--reduced-approval] [--json] [--backplane <url>]
//
// Hits GET /api/v1/connectors/operation-overrides. tenant_admin role
// required.
func newOverridesCmd() *cobra.Command {
	var (
		reducedApproval   bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "overrides",
		Short: "List operator changes to risk levels and approval, or the operations running with reduced approval",
		Long: "overrides calls GET /api/v1/connectors/operation-overrides and lists\n" +
			"every change an operator made to an operation's risk level or approval\n" +
			"flag (with `meho connector edit-op`, the console or the MCP edit tool).\n\n" +
			"Each row shows the connector, the operation, the risk level and\n" +
			"approval that apply in the row's scope (the value in code with every\n" +
			"change there), the scope (one tenant or the whole install), who made\n" +
			"the change, when, and the reason.\n\n" +
			"--reduced-approval lists only the operations a platform admin turned\n" +
			"approval off for, where approval is still off: the report\n" +
			"\"operations running with reduced\n" +
			"approval\". The console shows the same report at /ui/connectors/overrides.\n\n" +
			"A tenant admin sees the install-wide changes and their own tenant's.\n" +
			"A platform admin sees every tenant's.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runOverrides(cmd, reducedApproval, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&reducedApproval, "reduced-approval", false,
		"list only the operations a platform admin turned approval off for")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOverrides(cmd *cobra.Command, reducedApproval, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	result, err := getOverrides(cmd.Context(), backplaneURL, reducedApproval)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, jsonOut)
		}
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printOverridesTable(cmd.OutOrStdout(), reducedApproval, result.Overrides)
	return nil
}

// getOverrides drives the typed-client report endpoint with the usual
// one-shot 401-retry. Non-200 surfaces as *httpResponseError.
func getOverrides(ctx context.Context, backplaneURL string, reducedApproval bool) (*api.OperationOverrideList, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.ListOperationOverridesApiV1ConnectorsOperationOverridesGetParams{}
	if reducedApproval {
		params.ReducedApproval = &reducedApproval
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListOperationOverridesApiV1ConnectorsOperationOverridesGetResponse, error) {
			return authed.ListOperationOverridesApiV1ConnectorsOperationOverridesGetWithResponse(ctx, params)
		},
		func(r *api.ListOperationOverridesApiV1ConnectorsOperationOverridesGetResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

func printOverridesTable(w io.Writer, reducedApproval bool, rows []api.OperationOverrideRow) {
	label := "operation override(s)"
	if reducedApproval {
		label = "operation(s) running with reduced approval"
	}
	fmt.Fprintf(w, "%d %s\n", len(rows), label)
	if len(rows) == 0 {
		return
	}
	fmt.Fprintf(w, "%-24s %-36s %-11s %-9s %-8s %-20s %-16s %s\n",
		"connector_id", "op_id", "risk", "approval", "scope", "who", "when", "reason")
	for _, row := range rows {
		risk := "(code)"
		if row.SafetyLevel != nil {
			risk = *row.SafetyLevel
		}
		approval := "(code)"
		if row.RequiresApproval != nil {
			approval = "required"
			if !*row.RequiresApproval {
				approval = "no"
			}
		}
		reason := ""
		if row.Reason != nil {
			reason = *row.Reason
		}
		fmt.Fprintf(w, "%-24s %-36s %-11s %-9s %-8s %-20s %-16s %s\n",
			truncate(row.ConnectorId, 24),
			truncate(row.OpId, 36),
			risk,
			approval,
			string(row.Scope),
			truncate(row.SetBySub, 20),
			row.SetAt.UTC().Format("2006-01-02 15:04"),
			reason,
		)
	}
}

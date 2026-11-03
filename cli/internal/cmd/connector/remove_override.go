// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newRemoveOverrideCmd returns the `meho connector remove-override` command (#4711).
//
// CLI shape:
//
//	meho connector remove-override <connector_id> <op_id> \
//	  [--tenant <uuid>] [--reason <text>] [--json] [--backplane <url>]
//
// Hits DELETE /api/v1/connectors/<connector_id>/operation-overrides/<op_id>.
// tenant_admin role required.
func newRemoveOverrideCmd() *cobra.Command {
	var (
		tenant            string
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "remove-override <connector_id> <op_id>",
		Short: "Remove one operator change to an operation's risk level or approval",
		Long: "remove-override calls DELETE /api/v1/connectors/<id>/operation-overrides/<op_id>\n" +
			"and removes one change an operator made to an operation's risk level or\n" +
			"approval flag. The value in code applies again in that scope.\n\n" +
			"Pick the change with `meho connector overrides`: --tenant <uuid> names a\n" +
			"tenant's change; without --tenant the install-wide change is removed.\n" +
			"A platform admin can remove any tenant's change. A tenant admin can\n" +
			"remove only their own tenant's.\n\n" +
			"When the removal ends approval in that scope, it needs platform_admin,\n" +
			"a --reason, and a fresh sign-in: run `meho login` and retry within five\n" +
			"minutes. That is the same rule as turning approval off.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRemoveOverride(cmd, removeOverrideOptions{
				ConnectorID:       args[0],
				OpID:              args[1],
				Tenant:            tenant,
				Reason:            reason,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"the tenant whose change to remove (a UUID); leave it out for the install-wide change")
	cmd.Flags().StringVar(&reason, "reason", "",
		"why you remove it; written to the audit row and required when the removal ends approval")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type removeOverrideOptions struct {
	ConnectorID       string
	OpID              string
	Tenant            string
	Reason            string
	JSONOut           bool
	BackplaneOverride string
}

// removeOverrideResult is the --json envelope. The route returns 204 No
// Content, so the echoed request is the record of what was removed.
type removeOverrideResult struct {
	ConnectorID string  `json:"connector_id"`
	OpID        string  `json:"op_id"`
	Scope       string  `json:"scope"`
	TenantID    *string `json:"tenant_id"`
	Reason      *string `json:"reason"`
}

func runRemoveOverride(cmd *cobra.Command, opts removeOverrideOptions) error {
	params := &api.RemoveOpOverrideEndpointApiV1ConnectorsConnectorIdOperationOverridesOpIdDeleteParams{}
	result := removeOverrideResult{ConnectorID: opts.ConnectorID, OpID: opts.OpID, Scope: "install"}
	if opts.Tenant != "" {
		id, err := uuid.Parse(opts.Tenant)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--tenant %q is not a UUID", opts.Tenant)),
				opts.JSONOut,
			)
		}
		params.TenantId = &id
		tenant := id.String()
		result.Scope = "tenant"
		result.TenantID = &tenant
	}
	if opts.Reason != "" {
		reason := opts.Reason
		params.Reason = &reason
		result.Reason = &reason
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	if err := deleteOverride(cmd.Context(), backplaneURL, opts.ConnectorID, opts.OpID, params); err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			if he.statusCode == http.StatusUnauthorized && isFreshSignInRequired(he.body) {
				return output.RenderError(cmd.ErrOrStderr(),
					output.AuthExpired(fmt.Sprintf(
						"%s Run `meho login %s` and retry.",
						strings.TrimSpace(httpErrorDetail(he.body)), backplaneURL,
					)),
					opts.JSONOut,
				)
			}
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	scope := "the whole install"
	if result.TenantID != nil {
		scope = "tenant " + *result.TenantID
	}
	fmt.Fprintf(cmd.OutOrStdout(), "%s/%s — override removed for %s; the value in code applies again\n",
		opts.ConnectorID, opts.OpID, scope)
	return nil
}

// deleteOverride drives the typed-client remove-override endpoint with
// a one-shot 401-retry. The route returns 204 No Content; any other
// status surfaces as *httpResponseError.
func deleteOverride(
	ctx context.Context,
	backplaneURL, connectorID, opID string,
	params *api.RemoveOpOverrideEndpointApiV1ConnectorsConnectorIdOperationOverridesOpIdDeleteParams,
) error {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RemoveOpOverrideEndpointApiV1ConnectorsConnectorIdOperationOverridesOpIdDeleteResponse, error) {
			return authed.RemoveOpOverrideEndpointApiV1ConnectorsConnectorIdOperationOverridesOpIdDeleteWithResponse(
				ctx, connectorID, opID, params,
			)
		},
		func(r *api.RemoveOpOverrideEndpointApiV1ConnectorsConnectorIdOperationOverridesOpIdDeleteResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return err
	}
	if resp.StatusCode() != http.StatusNoContent {
		return &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return nil
}

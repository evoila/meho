// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package serviceprincipal

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewGrantsCmd returns `meho service-principals grants`, the operator surface
// for standing service-principal grants. The backend remains authoritative for
// descriptor classification; this CLI rejects malformed or plainly
// ungrantable requests before it makes a network call.
func NewGrantsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "grants",
		Short:        "Manage service-principal permission grants (operator)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd(), newShowCmd(), newCreateCmd(), newRevokeCmd())
	return cmd
}

func newListCmd() *cobra.Command {
	var principal, backplaneOverride string
	var includeExpired, jsonOut bool
	cmd := &cobra.Command{
		Use: "list", Short: "List service-principal grants in your tenant (operator)", Args: cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := listGrants(cmd.Context(), url, grantListParams(principal, includeExpired))
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printGrantList(cmd.OutOrStdout(), resp.JSON200.Grants)
			return nil
		},
	}
	cmd.Flags().StringVar(&principal, "principal", "", "filter by service-principal JWT sub")
	cmd.Flags().BoolVar(&includeExpired, "include-expired", false, "include expired or revoked grants")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceGrantListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func grantListParams(principal string, includeExpired bool) *api.ListGrantsApiV1ServicePrincipalsGrantsGetParams {
	params := &api.ListGrantsApiV1ServicePrincipalsGrantsGetParams{}
	if principal != "" {
		params.PrincipalSub = &principal
	}
	if includeExpired {
		params.IncludeRevoked = &includeExpired
	}
	return params
}

func listGrants(ctx context.Context, url string, params *api.ListGrantsApiV1ServicePrincipalsGrantsGetParams) (*api.ListGrantsApiV1ServicePrincipalsGrantsGetResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.ListGrantsApiV1ServicePrincipalsGrantsGetResponse, error) {
		return authed.ListGrantsApiV1ServicePrincipalsGrantsGetWithResponse(ctx, params)
	}, func(r *api.ListGrantsApiV1ServicePrincipalsGrantsGetResponse) int { return r.StatusCode() })
}

func newShowCmd() *cobra.Command {
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{Use: "show <grant-id>", Short: "Show one service-principal grant (operator)", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf("invalid <grant-id>: %v", err)), jsonOut)
			}
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := showGrant(cmd.Context(), url, id)
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printGrantDetail(cmd.OutOrStdout(), resp.JSON200)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func showGrant(ctx context.Context, url string, id uuid.UUID) (*api.ShowGrantApiV1ServicePrincipalsGrantsGrantIdGetResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.ShowGrantApiV1ServicePrincipalsGrantsGrantIdGetResponse, error) {
		return authed.ShowGrantApiV1ServicePrincipalsGrantsGrantIdGetWithResponse(ctx, id, nil)
	}, func(r *api.ShowGrantApiV1ServicePrincipalsGrantsGrantIdGetResponse) int { return r.StatusCode() })
}

func newCreateCmd() *cobra.Command {
	var principal, opID, connectorID, target, targetNamePattern, reason, expires, backplaneOverride string
	var jsonOut bool
	cmd := &cobra.Command{Use: "create", Short: "Create a service-principal grant (operator)", Args: cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			body, err := buildGrantCreateBody(principal, opID, connectorID, target, targetNamePattern, reason, expires)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := createGrant(cmd.Context(), url, body)
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusCreated {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			return renderGrantEntry(cmd.OutOrStdout(), resp.JSON201, jsonOut, "created")
		},
	}
	cmd.Flags().StringVar(&principal, "principal", "", "JWT sub of the service principal (required)")
	cmd.Flags().StringVar(&opID, "op-id", "", "exact operation id; globs and delete-shaped ops are refused (required)")
	cmd.Flags().StringVar(&connectorID, "connector-id", "", "exact connector id; globs are refused (required)")
	cmd.Flags().StringVar(&target, "target", "", "target UUID (optional; targetless is not a wildcard)")
	cmd.Flags().StringVar(&targetNamePattern, "target-name-pattern", "", "explicit fnmatch target-name selector (optional)")
	cmd.Flags().StringVar(&reason, "reason", "", "operator justification for this standing grant (required)")
	cmd.Flags().StringVar(&expires, "expires", "", "optional ISO 8601 UTC expiry")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func buildGrantCreateBody(principal, opID, connectorID, target, targetNamePattern, reason, expires string) (api.ServiceGrantCreate, error) {
	if principal == "" || opID == "" || connectorID == "" || reason == "" {
		return api.ServiceGrantCreate{}, fmt.Errorf("--principal, --op-id, --connector-id, and --reason are required")
	}
	if hasGlob(principal) || hasGlob(opID) || hasGlob(connectorID) {
		return api.ServiceGrantCreate{}, fmt.Errorf("--principal, --op-id, and --connector-id must be exact values; globs are not allowed")
	}
	if target != "" && targetNamePattern != "" {
		return api.ServiceGrantCreate{}, fmt.Errorf("--target and --target-name-pattern are mutually exclusive")
	}
	if isDeleteShaped(opID) {
		return api.ServiceGrantCreate{}, fmt.Errorf("--op-id %q is delete-shaped and can never be granted", opID)
	}
	if strings.Contains(opID, ".composite.") {
		return api.ServiceGrantCreate{}, fmt.Errorf("--op-id %q is a composite; create one grant for each governed child operation instead", opID)
	}
	body := api.ServiceGrantCreate{PrincipalSub: principal, OpId: opID, ConnectorId: connectorID, Reason: reason}
	if target != "" {
		id, err := uuid.Parse(target)
		if err != nil {
			return api.ServiceGrantCreate{}, fmt.Errorf("--target %q is not a valid UUID: %w", target, err)
		}
		body.TargetId = &id
	}
	if targetNamePattern != "" {
		body.TargetNamePattern = &targetNamePattern
	}
	if expires != "" {
		parsed, err := time.Parse(time.RFC3339, expires)
		if err != nil {
			return api.ServiceGrantCreate{}, fmt.Errorf("--expires %q is not a valid ISO 8601 date-time: %w", expires, err)
		}
		body.ExpiresAt = &parsed
	}
	return body, nil
}

func hasGlob(value string) bool { return strings.ContainsAny(value, "*?[") }

func isAffirmative(answer string) bool {
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

func isDeleteShaped(opID string) bool {
	return strings.HasPrefix(opID, "DELETE:") || strings.HasSuffix(opID, ".delete") || strings.HasSuffix(opID, ".destroy") || strings.HasSuffix(opID, ".remove") || strings.HasSuffix(opID, ".purge")
}

func createGrant(ctx context.Context, url string, body api.ServiceGrantCreate) (*api.CreateGrantApiV1ServicePrincipalsGrantsPostResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.CreateGrantApiV1ServicePrincipalsGrantsPostResponse, error) {
		return authed.CreateGrantApiV1ServicePrincipalsGrantsPostWithResponse(ctx, nil, body)
	}, func(r *api.CreateGrantApiV1ServicePrincipalsGrantsPostResponse) int { return r.StatusCode() })
}

func newRevokeCmd() *cobra.Command {
	var confirm, jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{Use: "revoke <grant-id>", Short: "Revoke a service-principal grant (operator)", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf("invalid <grant-id>: %v", err)), jsonOut)
			}
			if !confirm {
				fmt.Fprintf(cmd.OutOrStdout(), "Revoke grant %q. Continue? [y/N]: ", args[0])
				var answer string
				if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil || !isAffirmative(answer) {
					if jsonOut {
						return output.PrintJSON(cmd.OutOrStdout(), map[string]string{"grant_id": args[0], "status": "declined"})
					}
					fmt.Fprintf(cmd.OutOrStdout(), "declined: grant %q not revoked\n", args[0])
					return nil
				}
			}
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := revokeGrant(cmd.Context(), url, id)
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusNoContent {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), map[string]string{"grant_id": args[0], "status": "revoked"})
			}
			fmt.Fprintf(cmd.OutOrStdout(), "revoked grant %q\n", args[0])
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "confirm revocation")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable result JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func revokeGrant(ctx context.Context, url string, id uuid.UUID) (*api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse, error) {
		return authed.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteWithResponse(ctx, id, nil)
	}, func(r *api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse) int { return r.StatusCode() })
}

func printGrantList(w io.Writer, grants []api.ServiceGrantRead) {
	if len(grants) == 0 {
		fmt.Fprintln(w, "no service-principal grants in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-28s %-28s %-22s %s\n", "ID", "PRINCIPAL", "OP_ID", "CONNECTOR", "EXPIRES_AT")
	for _, grant := range grants {
		exp := "-"
		if grant.ExpiresAt != nil {
			exp = grant.ExpiresAt.UTC().Format(time.RFC3339)
		}
		fmt.Fprintf(w, "%-36s %-28s %-28s %-22s %s\n", grant.Id.String(), grant.PrincipalSub, grant.OpId, grant.ConnectorId, exp)
	}
}

func printGrantDetail(w io.Writer, grant *api.ServiceGrantRead) {
	if grant == nil {
		return
	}
	target := "targetless"
	if grant.TargetId != nil {
		target = grant.TargetId.String()
	} else if grant.TargetNamePattern != nil {
		target = "name:" + *grant.TargetNamePattern
	}
	exp := "permanent"
	if grant.ExpiresAt != nil {
		exp = grant.ExpiresAt.UTC().Format(time.RFC3339)
	}
	fmt.Fprintf(w, "id:                  %s\nprincipal_sub:       %s\nop_id:               %s\nconnector_id:        %s\ntarget:              %s\nreason:              %s\nexpires_at:          %s\ncreated_by:          %s\n", grant.Id.String(), grant.PrincipalSub, grant.OpId, grant.ConnectorId, target, grant.Reason, exp, grant.CreatedBySub)
}

func renderGrantEntry(w io.Writer, grant *api.ServiceGrantRead, jsonOut bool, verb string) error {
	if grant == nil {
		return fmt.Errorf("backend returned an empty grant body")
	}
	if jsonOut {
		return output.PrintJSON(w, grant)
	}
	fmt.Fprintf(w, "%s grant %s: principal=%s op_id=%s connector_id=%s\n", verb, grant.Id.String(), grant.PrincipalSub, grant.OpId, grant.ConnectorId)
	return nil
}

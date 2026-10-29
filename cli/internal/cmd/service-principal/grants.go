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
	var includeExpired, includeRevoked, jsonOut bool
	var limit, offset int
	cmd := &cobra.Command{
		Use: "list", Short: "List service-principal grants in your tenant (operator)", Args: cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := listGrants(cmd.Context(), url, grantListParams(principal, includeExpired, includeRevoked, limit, offset))
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
	cmd.Flags().BoolVar(&includeExpired, "include-expired", false, "include expired grants (default: active grants only)")
	cmd.Flags().BoolVar(&includeRevoked, "include-revoked", false, "include revoked grant history")
	cmd.Flags().IntVar(&limit, "limit", 0, "max grants per page (1..500, server default 100)")
	cmd.Flags().IntVar(&offset, "offset", 0, "page offset (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceGrantListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func grantListParams(principal string, includeExpired, includeRevoked bool, limit, offset int) *api.ListGrantsApiV1ServicePrincipalsGrantsGetParams {
	params := &api.ListGrantsApiV1ServicePrincipalsGrantsGetParams{}
	// Send false explicitly: the REST API defaults true for backwards
	// compatibility, while the CLI default is active-only.
	params.IncludeExpired = &includeExpired
	if principal != "" {
		params.PrincipalSub = &principal
	}
	if includeRevoked {
		params.IncludeRevoked = &includeRevoked
	}
	if limit > 0 {
		params.Limit = &limit
	}
	if offset > 0 {
		params.Offset = &offset
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
	var principal, opID, connectorID, target, targetProduct, targetNamePattern, reason, expires, backplaneOverride string
	var jsonOut bool
	cmd := &cobra.Command{Use: "create", Short: "Create a service-principal grant (operator)", Args: cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			body, err := buildGrantCreateBody(principal, opID, connectorID, target, targetProduct, targetNamePattern, reason, expires)
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
			if err := renderGrantEntry(cmd.OutOrStdout(), resp.JSON201, jsonOut, "created"); err != nil {
				return err
			}
			printCompositeGrantNote(cmd, body, jsonOut)
			return nil
		},
	}
	cmd.Flags().StringVar(&principal, "principal", "", "JWT sub of the service principal (required)")
	cmd.Flags().StringVar(&opID, "op-id", "", "exact operation id; globs are refused. A delete operation needs a delete grant: only in sandbox mode, by a tenant admin, with --expires within 30 days. REST and the CLI refuse it unless MEHO_SANDBOX_DELETE_GRANTS_FROM_API is on; the server decides (required)")
	cmd.Flags().StringVar(&connectorID, "connector-id", "", "exact connector id; globs are refused (required)")
	cmd.Flags().StringVar(&target, "target", "", "target UUID (optional; targetless is not a wildcard)")
	cmd.Flags().StringVar(&targetProduct, "target-product", "", "exact target-product selector (optional)")
	cmd.Flags().StringVar(&targetNamePattern, "target-name-pattern", "", "explicit fnmatch target-name selector (optional)")
	cmd.Flags().StringVar(&reason, "reason", "", "operator justification for this standing grant (required)")
	cmd.Flags().StringVar(&expires, "expires", "", "optional ISO 8601 UTC expiry")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func buildGrantCreateBody(principal, opID, connectorID, target, targetProduct, targetNamePattern, reason, expires string) (api.ServiceGrantCreate, error) {
	if principal == "" || opID == "" || connectorID == "" || reason == "" {
		return api.ServiceGrantCreate{}, fmt.Errorf("--principal, --op-id, --connector-id, and --reason are required")
	}
	if hasGlob(principal) || hasGlob(opID) || hasGlob(connectorID) {
		return api.ServiceGrantCreate{}, fmt.Errorf("--principal, --op-id, and --connector-id must be exact values; globs are not allowed")
	}
	if target != "" && (targetProduct != "" || targetNamePattern != "") {
		return api.ServiceGrantCreate{}, fmt.Errorf("--target is mutually exclusive with --target-product and --target-name-pattern")
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
	if targetProduct != "" {
		body.TargetProduct = &targetProduct
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

// isCompositeOp reports whether opID names a multi-step operation. MEHO checks
// such an operation twice: once on the operation itself, before any step
// runs, and once on each write step. A service principal needs a grant for
// both, so the grant on the operation is accepted and a note names the
// second kind.
func isCompositeOp(opID string) bool { return strings.Contains(opID, ".composite.") }

// compositeGrantNote tells the operator that each write step of a multi-step
// operation needs its own grant, and where to list the steps.
func compositeGrantNote(opID, connectorID string) string {
	return fmt.Sprintf("This is a multi-step operation. Each of its write steps needs its own grant too. List them with GET /api/v1/operations/governed-subops?op_id=%s&connector_id=%s", opID, connectorID)
}

// printCompositeGrantNote prints the note after a grant on a multi-step
// operation. With --json the note goes to stderr so stdout stays valid JSON.
func printCompositeGrantNote(cmd *cobra.Command, body api.ServiceGrantCreate, jsonOut bool) {
	if !isCompositeOp(body.OpId) {
		return
	}
	w := cmd.OutOrStdout()
	if jsonOut {
		w = cmd.ErrOrStderr()
	}
	fmt.Fprintln(w, compositeGrantNote(body.OpId, body.ConnectorId))
}

// hasGlob rejects glob operators that cannot be part of an exact identifier.
// A literal '?' is permitted: GET-style operation identifiers commonly carry
// query strings and the server accepts it as an exact character.
func hasGlob(value string) bool { return strings.ContainsAny(value, "*[") }

func isAffirmative(answer string) bool {
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
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
	var reason, backplaneOverride string
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
			resp, err := revokeGrant(cmd.Context(), url, id, reason)
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
	cmd.Flags().StringVar(&reason, "reason", "", "optional reason for the revoke, stored on the grant and the audit row")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable result JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func revokeGrant(ctx context.Context, url string, id uuid.UUID, reason string) (*api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse, error) {
		return authed.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteWithResponse(ctx, id, revokeGrantParams(reason))
	}, func(r *api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteResponse) int { return r.StatusCode() })
}

// revokeGrantParams sends the reason only when the operator gave one, so a
// revoke without --reason keeps the plain DELETE with no query string.
func revokeGrantParams(reason string) *api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteParams {
	if reason == "" {
		return nil
	}
	return &api.RevokeGrantApiV1ServicePrincipalsGrantsGrantIdDeleteParams{Reason: &reason}
}

func printGrantList(w io.Writer, grants []api.ServiceGrantRead) {
	if len(grants) == 0 {
		fmt.Fprintln(w, "no service-principal grants in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-28s %-28s %-22s %-25s %s\n", "ID", "PRINCIPAL", "OP_ID", "CONNECTOR", "EXPIRES_AT", "ORPHANED")
	orphans := 0
	for _, grant := range grants {
		exp := "-"
		if grant.ExpiresAt != nil {
			exp = grant.ExpiresAt.UTC().Format(time.RFC3339)
		}
		orphaned := "-"
		if isOrphaned(grant) {
			orphaned = "yes"
			orphans++
		}
		fmt.Fprintf(w, "%-36s %-28s %-28s %-22s %-25s %s\n", grant.Id.String(), grant.PrincipalSub, grant.OpId, grant.ConnectorId, exp, orphaned)
	}
	if orphans > 0 {
		fmt.Fprintf(w, "\n%d grant(s) are orphaned: their target was deleted or registered again under a new id, so they never match.\nRun `meho service-principals grants show <grant-id>` to see how to fix each one.\n", orphans)
	}
}

// isOrphaned reports the server's read-time orphan flag: the grant's concrete
// target no longer exists, so the grant can never match a dispatch.
func isOrphaned(grant api.ServiceGrantRead) bool {
	return grant.Orphaned != nil && *grant.Orphaned
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
	if isOrphaned(*grant) {
		fmt.Fprintln(w, "orphaned:            yes")
		if grant.OrphanRemediation != nil {
			fmt.Fprintf(w, "remediation:         %s\n", *grant.OrphanRemediation)
		}
	}
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

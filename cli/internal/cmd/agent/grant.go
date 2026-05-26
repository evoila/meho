// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewGrantRootCmd returns the `meho agent grant` parent command and its
// sub-commands (G11.2-T6 #819). Grafted onto the `meho agent` tree by
// NewRootCmd alongside list / show / create / edit / delete / run.
//
// Sub-commands:
//
//   - meho agent grant list [--principal <sub>] [--include-expired] [--json]
//   - meho agent grant show <grant-id> [--json]
//   - meho agent grant create --principal <sub> --op <pattern> --verdict V [--target T] [--expires <iso8601>] [--json]
//   - meho agent grant elevate --principal <sub> --op <pattern> --verdict V --expires <iso8601> [--target T] [--json]
//   - meho agent grant revoke <grant-id> [--confirm] [--json]
func NewGrantRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "grant",
		Short: "Manage agent permission grants (tenant_admin)",
		Long: "Manage per-(principal, op-pattern, target-scope) permission " +
			"grants for agents in your tenant. All sub-commands require " +
			"tenant_admin. A new agent has no write permissions until a " +
			"grant is issued. Use 'elevate' for time-bounded change " +
			"windows — the grant-expiry sweeper removes it automatically " +
			"after the window ends.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newGrantListCmd())
	cmd.AddCommand(newGrantShowCmd())
	cmd.AddCommand(newGrantCreateCmd())
	cmd.AddCommand(newGrantElevateCmd())
	cmd.AddCommand(newGrantRevokeCmd())
	return cmd
}

// ---------------------------------------------------------------------------
// Wire types mirroring the backend Pydantic shapes
// ---------------------------------------------------------------------------

// grantEntry mirrors backend AgentGrantRead.
type grantEntry struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	PrincipalSub string  `json:"principal_sub"`
	OpPattern    string  `json:"op_pattern"`
	TargetScope  *string `json:"target_scope"`
	Verdict      string  `json:"verdict"`
	CreatedBySub string  `json:"created_by_sub"`
	ExpiresAt    *string `json:"expires_at"`
	CreatedAt    string  `json:"created_at"`
	UpdatedAt    string  `json:"updated_at"`
}

// grantListResponse mirrors backend AgentGrantListResponse.
type grantListResponse struct {
	Grants []grantEntry `json:"grants"`
}

// grantCreateRequest mirrors backend AgentGrantCreate.
type grantCreateRequest struct {
	PrincipalSub string  `json:"principal_sub"`
	OpPattern    string  `json:"op_pattern"`
	TargetScope  *string `json:"target_scope,omitempty"`
	Verdict      string  `json:"verdict"`
	ExpiresAt    *string `json:"expires_at,omitempty"`
}

const _grantsBasePath = "/api/v1/agents/grants"

func buildGrantListPath(principalSub string, includeExpired bool, limit, offset int) string {
	q := url.Values{}
	if principalSub != "" {
		q.Set("principal_sub", principalSub)
	}
	if includeExpired {
		q.Set("include_expired", "true")
	}
	if limit > 0 {
		q.Set("limit", fmt.Sprintf("%d", limit))
	}
	if offset > 0 {
		q.Set("offset", fmt.Sprintf("%d", offset))
	}
	path := _grantsBasePath
	if enc := q.Encode(); enc != "" {
		path = path + "?" + enc
	}
	return path
}

func buildGrantShowPath(grantID string) string {
	return _grantsBasePath + "/" + grantID
}

// ---------------------------------------------------------------------------
// meho agent grant list
// ---------------------------------------------------------------------------

func newGrantListCmd() *cobra.Command {
	var (
		principalSub      string
		includeExpired    bool
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List agent permission grants in your tenant (tenant_admin)",
		Long: "list calls GET /api/v1/agents/grants and renders the " +
			"grants in the operator's tenant. --principal filters to one " +
			"agent's grants. --include-expired shows past elevations. " +
			"--limit and --offset control paging.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, "GET",
				buildGrantListPath(principalSub, includeExpired, limit, offset), nil)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			var resp grantListResponse
			if err := json.Unmarshal(raw, &resp); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("decode grant list: %v", err)), jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp)
			}
			printGrantListTable(cmd.OutOrStdout(), resp.Grants)
			return nil
		},
	}
	cmd.Flags().StringVar(&principalSub, "principal", "",
		"filter by agent principal JWT sub")
	cmd.Flags().BoolVar(&includeExpired, "include-expired", false,
		"include expired elevations (default: active only)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max grants per page (1..500, server default 100)")
	cmd.Flags().IntVar(&offset, "offset", 0, "page offset (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func printGrantListTable(w io.Writer, grants []grantEntry) {
	if len(grants) == 0 {
		fmt.Fprintln(w, "no permission grants in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-36s %-30s %-12s %-22s %s\n",
		"ID", "PRINCIPAL", "OP_PATTERN", "VERDICT", "EXPIRES_AT", "CREATED_BY")
	for _, g := range grants {
		exp := "-"
		if g.ExpiresAt != nil {
			exp = *g.ExpiresAt
		}
		fmt.Fprintf(w, "%-36s %-36s %-30s %-12s %-22s %s\n",
			g.ID, g.PrincipalSub, g.OpPattern, g.Verdict, exp, g.CreatedBySub)
	}
}

// ---------------------------------------------------------------------------
// meho agent grant show
// ---------------------------------------------------------------------------

func newGrantShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <grant-id>",
		Short: "Fetch one permission grant by id (tenant_admin)",
		Long: "show calls GET /api/v1/agents/grants/{grant_id}. " +
			"Returns grant details. 404 if absent or cross-tenant.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, "GET",
				buildGrantShowPath(args[0]), nil)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			var entry grantEntry
			if err := json.Unmarshal(raw, &entry); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("decode grant show: %v", err)), jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), entry)
			}
			printGrantDetail(cmd.OutOrStdout(), entry)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw grant JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func printGrantDetail(w io.Writer, g grantEntry) {
	exp := "-"
	if g.ExpiresAt != nil {
		exp = *g.ExpiresAt
	}
	scope := "*"
	if g.TargetScope != nil {
		scope = *g.TargetScope
	}
	fmt.Fprintf(w, "id:            %s\n", g.ID)
	fmt.Fprintf(w, "principal_sub: %s\n", g.PrincipalSub)
	fmt.Fprintf(w, "op_pattern:    %s\n", g.OpPattern)
	fmt.Fprintf(w, "target_scope:  %s\n", scope)
	fmt.Fprintf(w, "verdict:       %s\n", g.Verdict)
	fmt.Fprintf(w, "expires_at:    %s\n", exp)
	fmt.Fprintf(w, "created_by:    %s\n", g.CreatedBySub)
	fmt.Fprintf(w, "created_at:    %s\n", g.CreatedAt)
}

// ---------------------------------------------------------------------------
// meho agent grant create
// ---------------------------------------------------------------------------

func newGrantCreateCmd() *cobra.Command {
	var (
		principalSub      string
		opPattern         string
		verdict           string
		targetScope       string
		expiresAt         string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a permission grant for an agent principal (tenant_admin)",
		Long: "create calls POST /api/v1/agents/grants to create one " +
			"permission grant. Specify --expires for a time-bounded " +
			"elevation (ISO 8601 UTC, e.g. 2026-05-25T18:00:00Z). " +
			"Omit --expires for a permanent grant. " +
			"--verdict is one of: auto-execute | needs-approval | deny.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if principalSub == "" || opPattern == "" || verdict == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("--principal, --op, and --verdict are required"), jsonOut)
			}
			body, err := buildGrantCreateBody(principalSub, opPattern, verdict, targetScope, expiresAt)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, "POST", _grantsBasePath, body)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return renderGrantResult(cmd.OutOrStdout(), raw, jsonOut, "created")
		},
	}
	cmd.Flags().StringVar(&principalSub, "principal", "", "JWT sub of the agent principal (required)")
	cmd.Flags().StringVar(&opPattern, "op", "", "fnmatch op-pattern, e.g. '*' or 'vault.kv.*' (required)")
	cmd.Flags().StringVar(&verdict, "verdict", "",
		"auto-execute | needs-approval | deny (required)")
	cmd.Flags().StringVar(&targetScope, "target", "",
		"target UUID or '*' for any target (default: any)")
	cmd.Flags().StringVar(&expiresAt, "expires", "",
		"ISO 8601 UTC expiry for a time-bounded elevation, e.g. 2026-05-25T18:00:00Z")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw grant JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// ---------------------------------------------------------------------------
// meho agent grant elevate
// ---------------------------------------------------------------------------

func newGrantElevateCmd() *cobra.Command {
	var (
		principalSub      string
		opPattern         string
		verdict           string
		targetScope       string
		expiresAt         string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "elevate",
		Short: "Create a time-bounded elevation grant (tenant_admin)",
		Long: "elevate calls POST /api/v1/agents/grants/elevate to create " +
			"a time-bounded permission elevation. --expires is required. " +
			"The grant-expiry sweeper removes it automatically after the " +
			"window ends, reverting the agent to its baseline permissions.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if principalSub == "" || opPattern == "" || verdict == "" || expiresAt == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("--principal, --op, --verdict, and --expires are all required for elevate"),
					jsonOut)
			}
			body, err := buildGrantCreateBody(principalSub, opPattern, verdict, targetScope, expiresAt)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, "POST",
				_grantsBasePath+"/elevate", body)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return renderGrantResult(cmd.OutOrStdout(), raw, jsonOut, "elevated")
		},
	}
	cmd.Flags().StringVar(&principalSub, "principal", "", "JWT sub of the agent principal (required)")
	cmd.Flags().StringVar(&opPattern, "op", "", "fnmatch op-pattern (required)")
	cmd.Flags().StringVar(&verdict, "verdict", "", "auto-execute | needs-approval | deny (required)")
	cmd.Flags().StringVar(&targetScope, "target", "", "target UUID or '*' for any target (optional)")
	cmd.Flags().StringVar(&expiresAt, "expires", "",
		"required ISO 8601 UTC expiry, e.g. 2026-06-01T00:00:00Z")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw grant JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// ---------------------------------------------------------------------------
// meho agent grant revoke
// ---------------------------------------------------------------------------

func newGrantRevokeCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "revoke <grant-id>",
		Short: "Revoke a permission grant by id (tenant_admin)",
		Long: "revoke calls DELETE /api/v1/agents/grants/{grant_id}. " +
			"Without --confirm the verb prompts for a y/N confirmation. " +
			"Returns 404 when the grant is absent or cross-tenant.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			grantID := args[0]
			if !confirm {
				prompt := fmt.Sprintf("Revoke grant %q. Continue?", grantID)
				if !confirmPrompt(cmd, prompt) {
					if jsonOut {
						return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
							"grant_id": grantID, "status": "declined",
						})
					}
					fmt.Fprintf(cmd.OutOrStdout(), "declined: grant %q not revoked\n", grantID)
					return nil
				}
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			if err := callGrantRevoke(cmd.Context(), backplaneURL, grantID); err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
					"grant_id": grantID, "status": "revoked",
				})
			}
			fmt.Fprintf(cmd.OutOrStdout(), "revoked grant %q\n", grantID)
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "skip the stdin confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable result JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func callGrantRevoke(ctx context.Context, backplaneURL, grantID string) error {
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildGrantShowPath(grantID), nil)
	return err
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

// buildGrantCreateBody builds the JSON request body for create / elevate.
// Validates the expiresAt format when supplied.
func buildGrantCreateBody(
	principalSub, opPattern, verdict, targetScope, expiresAt string,
) ([]byte, error) {
	req := grantCreateRequest{
		PrincipalSub: principalSub,
		OpPattern:    opPattern,
		Verdict:      verdict,
	}
	if targetScope != "" {
		req.TargetScope = &targetScope
	}
	if expiresAt != "" {
		// Validate format — must parse as RFC 3339 / ISO 8601.
		if _, err := time.Parse(time.RFC3339, expiresAt); err != nil {
			return nil, fmt.Errorf("--expires %q is not a valid ISO 8601 date-time: %w", expiresAt, err)
		}
		req.ExpiresAt = &expiresAt
	}
	return json.Marshal(req)
}

// renderGrantResult decodes the raw backend response (a grantEntry JSON
// object), prints a human summary or raw JSON, and returns any error.
func renderGrantResult(w io.Writer, raw []byte, jsonOut bool, verb string) error {
	var entry grantEntry
	if err := json.Unmarshal(raw, &entry); err != nil {
		return fmt.Errorf("decode grant response: %w", err)
	}
	if jsonOut {
		return output.PrintJSON(w, entry)
	}
	exp := "permanent"
	if entry.ExpiresAt != nil {
		exp = "expires " + *entry.ExpiresAt
	}
	fmt.Fprintf(w, "%s grant %s: principal=%s op=%s verdict=%s (%s)\n",
		verb, entry.ID, entry.PrincipalSub, entry.OpPattern, entry.Verdict, exp)
	return nil
}

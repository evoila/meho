// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
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
			params := grantListParams(principalSub, includeExpired, limit, offset)
			resp, err := getGrantList(cmd.Context(), backplaneURL, params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printGrantListTable(cmd.OutOrStdout(), resp.JSON200.Grants)
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
		"emit raw AgentGrantListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func grantListParams(principalSub string, includeExpired bool, limit, offset int) *api.ListGrantsApiV1AgentsGrantsGetParams {
	params := &api.ListGrantsApiV1AgentsGrantsGetParams{}
	if principalSub != "" {
		sub := principalSub
		params.PrincipalSub = &sub
	}
	if includeExpired {
		expired := true
		params.IncludeExpired = &expired
	}
	if limit > 0 {
		l := limit
		params.Limit = &l
	}
	if offset > 0 {
		o := offset
		params.Offset = &o
	}
	return params
}

func getGrantList(ctx context.Context, backplaneURL string, params *api.ListGrantsApiV1AgentsGrantsGetParams) (*api.ListGrantsApiV1AgentsGrantsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListGrantsApiV1AgentsGrantsGetResponse, error) {
			return authed.ListGrantsApiV1AgentsGrantsGetWithResponse(ctx, params)
		},
		func(r *api.ListGrantsApiV1AgentsGrantsGetResponse) int { return r.StatusCode() },
	)
}

func printGrantListTable(w io.Writer, grants []api.AgentGrantRead) {
	if len(grants) == 0 {
		fmt.Fprintln(w, "no permission grants in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-36s %-30s %-12s %-22s %s\n",
		"ID", "PRINCIPAL", "OP_PATTERN", "VERDICT", "EXPIRES_AT", "CREATED_BY")
	for _, g := range grants {
		exp := "-"
		if g.ExpiresAt != nil {
			exp = g.ExpiresAt.UTC().Format(time.RFC3339)
		}
		fmt.Fprintf(w, "%-36s %-36s %-30s %-12s %-22s %s\n",
			g.Id.String(), g.PrincipalSub, g.OpPattern, g.Verdict, exp, g.CreatedBySub)
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
			grantID, err := uuid.Parse(args[0])
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("invalid <grant-id>: %v", err)), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := getGrant(cmd.Context(), backplaneURL, grantID)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printGrantDetail(cmd.OutOrStdout(), resp.JSON200)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func getGrant(ctx context.Context, backplaneURL string, grantID uuid.UUID) (*api.ShowGrantApiV1AgentsGrantsGrantIdGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowGrantApiV1AgentsGrantsGrantIdGetResponse, error) {
			return authed.ShowGrantApiV1AgentsGrantsGrantIdGetWithResponse(ctx, grantID, nil)
		},
		func(r *api.ShowGrantApiV1AgentsGrantsGrantIdGetResponse) int { return r.StatusCode() },
	)
}

func printGrantDetail(w io.Writer, g *api.AgentGrantRead) {
	if g == nil {
		return
	}
	exp := "-"
	if g.ExpiresAt != nil {
		exp = g.ExpiresAt.UTC().Format(time.RFC3339)
	}
	scope := "*"
	if g.TargetScope != nil {
		scope = *g.TargetScope
	}
	fmt.Fprintf(w, "id:            %s\n", g.Id.String())
	fmt.Fprintf(w, "principal_sub: %s\n", g.PrincipalSub)
	fmt.Fprintf(w, "op_pattern:    %s\n", g.OpPattern)
	fmt.Fprintf(w, "target_scope:  %s\n", scope)
	fmt.Fprintf(w, "verdict:       %s\n", g.Verdict)
	fmt.Fprintf(w, "expires_at:    %s\n", exp)
	fmt.Fprintf(w, "created_by:    %s\n", g.CreatedBySub)
	fmt.Fprintf(w, "created_at:    %s\n", g.CreatedAt.UTC().Format(time.RFC3339))
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
			resp, err := postGrantCreate(cmd.Context(), backplaneURL, body)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusCreated {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			return renderGrantEntry(cmd.OutOrStdout(), resp.JSON201, jsonOut, "created")
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
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func postGrantCreate(ctx context.Context, backplaneURL string, body api.AgentGrantCreate) (*api.CreateGrantApiV1AgentsGrantsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.CreateGrantApiV1AgentsGrantsPostResponse, error) {
			return authed.CreateGrantApiV1AgentsGrantsPostWithResponse(ctx, nil, body)
		},
		func(r *api.CreateGrantApiV1AgentsGrantsPostResponse) int { return r.StatusCode() },
	)
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
			body, err := buildGrantElevateBody(principalSub, opPattern, verdict, targetScope, expiresAt)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := postGrantElevate(cmd.Context(), backplaneURL, body)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusCreated {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			return renderGrantEntry(cmd.OutOrStdout(), resp.JSON201, jsonOut, "elevated")
		},
	}
	cmd.Flags().StringVar(&principalSub, "principal", "", "JWT sub of the agent principal (required)")
	cmd.Flags().StringVar(&opPattern, "op", "", "fnmatch op-pattern (required)")
	cmd.Flags().StringVar(&verdict, "verdict", "", "auto-execute | needs-approval | deny (required)")
	cmd.Flags().StringVar(&targetScope, "target", "", "target UUID or '*' for any target (optional)")
	cmd.Flags().StringVar(&expiresAt, "expires", "",
		"required ISO 8601 UTC expiry, e.g. 2026-06-01T00:00:00Z")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentGrantRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func postGrantElevate(ctx context.Context, backplaneURL string, body api.AgentElevationCreate) (*api.ElevateGrantApiV1AgentsGrantsElevatePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ElevateGrantApiV1AgentsGrantsElevatePostResponse, error) {
			return authed.ElevateGrantApiV1AgentsGrantsElevatePostWithResponse(ctx, nil, body)
		},
		func(r *api.ElevateGrantApiV1AgentsGrantsElevatePostResponse) int { return r.StatusCode() },
	)
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
			grantIDArg := args[0]
			grantID, err := uuid.Parse(grantIDArg)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("invalid <grant-id>: %v", err)), jsonOut)
			}
			if !confirm {
				prompt := fmt.Sprintf("Revoke grant %q. Continue?", grantIDArg)
				if !confirmPrompt(cmd, prompt) {
					if jsonOut {
						return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
							"grant_id": grantIDArg, "status": "declined",
						})
					}
					fmt.Fprintf(cmd.OutOrStdout(), "declined: grant %q not revoked\n", grantIDArg)
					return nil
				}
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := callGrantRevoke(cmd.Context(), backplaneURL, grantID)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusNoContent {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
					"grant_id": grantIDArg, "status": "revoked",
				})
			}
			fmt.Fprintf(cmd.OutOrStdout(), "revoked grant %q\n", grantIDArg)
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "skip the stdin confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable result JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func callGrantRevoke(ctx context.Context, backplaneURL string, grantID uuid.UUID) (*api.RevokeGrantApiV1AgentsGrantsGrantIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RevokeGrantApiV1AgentsGrantsGrantIdDeleteResponse, error) {
			return authed.RevokeGrantApiV1AgentsGrantsGrantIdDeleteWithResponse(ctx, grantID, nil)
		},
		func(r *api.RevokeGrantApiV1AgentsGrantsGrantIdDeleteResponse) int { return r.StatusCode() },
	)
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

// buildGrantCreateBody assembles the generated AgentGrantCreate body
// for the /grants create endpoint. ExpiresAt is `*time.Time` (omit for
// a permanent grant); a non-empty --expires must parse as RFC 3339 /
// ISO 8601.
func buildGrantCreateBody(
	principalSub, opPattern, verdict, targetScope, expiresAt string,
) (api.AgentGrantCreate, error) {
	body := api.AgentGrantCreate{
		PrincipalSub: principalSub,
		OpPattern:    opPattern,
		Verdict:      api.PermissionVerdict(verdict),
	}
	if targetScope != "" {
		scope := targetScope
		body.TargetScope = &scope
	}
	if expiresAt != "" {
		parsed, err := time.Parse(time.RFC3339, expiresAt)
		if err != nil {
			return api.AgentGrantCreate{}, fmt.Errorf("--expires %q is not a valid ISO 8601 date-time: %w", expiresAt, err)
		}
		body.ExpiresAt = &parsed
	}
	return body, nil
}

// buildGrantElevateBody assembles the generated AgentElevationCreate
// body. ExpiresAt is required and `time.Time` (not a pointer) — the
// elevate endpoint refuses an open-ended elevation.
func buildGrantElevateBody(
	principalSub, opPattern, verdict, targetScope, expiresAt string,
) (api.AgentElevationCreate, error) {
	parsed, err := time.Parse(time.RFC3339, expiresAt)
	if err != nil {
		return api.AgentElevationCreate{}, fmt.Errorf("--expires %q is not a valid ISO 8601 date-time: %w", expiresAt, err)
	}
	body := api.AgentElevationCreate{
		PrincipalSub: principalSub,
		OpPattern:    opPattern,
		Verdict:      api.PermissionVerdict(verdict),
		ExpiresAt:    parsed,
	}
	if targetScope != "" {
		scope := targetScope
		body.TargetScope = &scope
	}
	return body, nil
}

// renderGrantEntry prints a single AgentGrantRead — either as raw JSON
// (with --json) or as the existing "<verb> grant <id>: ..." human line.
// Shared between the create and elevate verbs.
func renderGrantEntry(w io.Writer, entry *api.AgentGrantRead, jsonOut bool, verb string) error {
	if entry == nil {
		return fmt.Errorf("backend returned an empty grant body")
	}
	if jsonOut {
		return output.PrintJSON(w, entry)
	}
	exp := "permanent"
	if entry.ExpiresAt != nil {
		exp = "expires " + entry.ExpiresAt.UTC().Format(time.RFC3339)
	}
	fmt.Fprintf(w, "%s grant %s: principal=%s op=%s verdict=%s (%s)\n",
		verb, entry.Id.String(), entry.PrincipalSub, entry.OpPattern, entry.Verdict, exp)
	return nil
}

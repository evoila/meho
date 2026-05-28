// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewForgetCmd returns the top-level `meho forget` command (issue
// #424).
//
// CLI shape:
//
//	meho forget <scope>/<slug> [--target NAME] [--confirm] [--json] [--backplane <url>]
//
// Role: any authenticated operator. The service-layer
// MemoryRbacResolver further restricts `tenant` forgets to
// `tenant_admin`; that surfaces as 403 insufficient_role.
//
// Default behaviour prompts for a y/N confirmation on stdin;
// `--confirm` skips the prompt for scripted use. Delete is
// **idempotent** at the backend: a forget against an absent slug
// returns 204 (not 404), matching the route's info-leak avoidance.
// The CLI mentions idempotency in the success line so operators
// rerunning the command after a previous successful run don't
// mistake the no-op for an error.
//
// Exit codes:
//   - 0   delete succeeded (204) or operator declined the prompt
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response
//   - 5   insufficient_role (e.g. operator forgetting `tenant` scope)
func NewForgetCmd() *cobra.Command {
	var (
		confirm           bool
		targetFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "forget <scope>/<slug>",
		Short: "Delete one memory by natural key (DELETE /api/v1/memory)",
		Long: "forget calls DELETE /api/v1/memory/{scope}/{slug}. " +
			"Idempotent server-side: a forget against an absent slug " +
			"returns 204 (the conflation prevents enumerating other " +
			"tenants via status-code differential).\n\n" +
			"Without --confirm, the verb prompts on stdin for a y/N " +
			"confirmation; --confirm skips the prompt for scripted use " +
			"(CI pipelines, etc.). Declining the prompt exits 0 " +
			"without calling the backend.\n\n" +
			"--target NAME is required when --scope=target or " +
			"user-target. The check fires client-side so a forgotten " +
			"flag surfaces without a round-trip.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runForget(cmd, forgetOptions{
				ScopeSlugArg:      args[0],
				TargetArg:         targetFlag,
				Confirm:           confirm,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false,
		"skip the stdin confirmation prompt")
	cmd.Flags().StringVar(&targetFlag, "target", "",
		"target name (required when --scope=target or user-target)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a machine-readable success envelope instead of the human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}

type forgetOptions struct {
	ScopeSlugArg      string
	TargetArg         string
	Confirm           bool
	JSONOut           bool
	BackplaneOverride string
}

// forgetResult is the structure printed in --json mode. Kept small
// (scope + slug + status) so operators piping into jq get a stable
// envelope regardless of whether the row existed server-side (the
// substrate doesn't surface that distinction back to the CLI).
type forgetResult struct {
	Scope  string `json:"scope"`
	Slug   string `json:"slug"`
	Status string `json:"status"`
}

func runForget(cmd *cobra.Command, opts forgetOptions) error {
	scope, slug, err := parseScopeSlugArg(opts.ScopeSlugArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	if err := requireTargetForScope(scope, opts.TargetArg); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	// Confirm BEFORE resolving the backplane so that an operator who
	// declines (or hits ^D / EOF on a piped /dev/null) exits 0
	// regardless of whether `meho login` has been run. Resolving
	// first would surface auth_expired on a no-config workstation
	// even when the operator was about to type `n` — the prompt
	// would never appear, and the "ask before doing destructive
	// things" promise would be violated. Same shape `meho kb
	// delete` adopted.
	if !opts.Confirm {
		prompt := fmt.Sprintf(
			"Forget memory %s/%s — idempotent (no-op if already absent). Continue?",
			scope, slug,
		)
		// Route the prompt to stderr in --json mode so the JSON
		// envelope on stdout stays parseable for `jq` consumers; the
		// human-facing mode keeps the prompt on stdout for the
		// familiar interactive shape.
		promptW := cmd.OutOrStdout()
		if opts.JSONOut {
			promptW = cmd.ErrOrStderr()
		}
		if !confirmPrompt(cmd, promptW, prompt) {
			result := forgetResult{
				Scope:  string(scope),
				Slug:   slug,
				Status: "declined",
			}
			if opts.JSONOut {
				return output.PrintJSON(cmd.OutOrStdout(), result)
			}
			fmt.Fprintf(cmd.OutOrStdout(),
				"declined: memory %s/%s not deleted\n", scope, slug)
			return nil
		}
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := callForget(cmd.Context(), backplaneURL, scope, slug, opts.TargetArg)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// The forget route is idempotent server-side: a delete against
	// an absent slug returns 204. Treat anything other than 204 as
	// a non-success and route through renderHTTPStatus — the
	// pre-migration ladder rejected non-2xx via the local httpError
	// sentinel, and the typed-client equivalent is to gate on the
	// 204 status code (the only success code the substrate emits
	// for this route).
	if resp.StatusCode() != http.StatusNoContent {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	result := forgetResult{
		Scope:  string(scope),
		Slug:   slug,
		Status: "deleted",
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"forgot memory %s/%s (idempotent: no-op if already absent)\n", scope, slug)
	return nil
}

// buildForgetParams maps the CLI flags onto the generated query-
// param shape. `TargetName` is set only when the operator supplied
// `--target` so an unset flag stays absent on the wire (user /
// user-tenant / tenant forgets carry no target_name).
func buildForgetParams(targetName string) *api.ForgetApiV1MemoryScopeSlugDeleteParams {
	params := &api.ForgetApiV1MemoryScopeSlugDeleteParams{}
	if targetName != "" {
		t := targetName
		params.TargetName = &t
	}
	return params
}

func callForget(
	ctx context.Context,
	backplaneURL string,
	scope Scope,
	slug, targetName string,
) (*api.ForgetApiV1MemoryScopeSlugDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := buildForgetParams(targetName)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ForgetApiV1MemoryScopeSlugDeleteResponse, error) {
			return authed.ForgetApiV1MemoryScopeSlugDeleteWithResponse(ctx, scope, slug, params)
		},
		func(r *api.ForgetApiV1MemoryScopeSlugDeleteResponse) int { return r.StatusCode() },
	)
}

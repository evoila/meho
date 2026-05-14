// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// TransitionResponse mirrors the backend response from POST
// /api/v1/connectors/{id}/{enable,disable}. The route is idempotent
// — calling enable on an already-enabled connector returns
// review_status=enabled + 0/0 counts.
type TransitionResponse struct {
	ConnectorID       string `json:"connector_id"`
	ReviewStatus      string `json:"review_status"`
	GroupsUpdated     int    `json:"groups_updated"`
	OperationsUpdated int    `json:"operations_updated"`
}

// newEnableCmd returns the `meho connector enable` command.
//
// CLI shape:
//
//	meho connector enable <connector_id> [--confirm] [--json] [--backplane <url>]
//
// Hits POST /api/v1/connectors/<connector_id>/enable. Without
// --confirm, the verb prompts on stdin for "y/yes"; --confirm skips
// the prompt for scripted use. tenant_admin role required.
func newEnableCmd() *cobra.Command {
	return newTransitionCmd(transitionParams{
		Verb:            "enable",
		Short:           "Flip a staged or disabled connector to enabled (operations dispatchable)",
		Action:          "enabled",
		ConfirmQuestion: "Enable connector %s — all ops with is_enabled=true become dispatchable. Continue?",
		Path:            "/api/v1/connectors/%s/enable",
	})
}

// newDisableCmd is filed alongside enable because both verbs share
// the same flag set, run shape, and rendering. The only differences
// are the URL suffix and the prompt prose.
func newDisableCmd() *cobra.Command {
	return newTransitionCmd(transitionParams{
		Verb:            "disable",
		Short:           "Flip an enabled connector back to disabled (rollback; per-op overrides preserved)",
		Action:          "disabled",
		ConfirmQuestion: "Disable connector %s — operations become non-dispatchable. Per-op overrides preserved. Continue?",
		Path:            "/api/v1/connectors/%s/disable",
	})
}

type transitionParams struct {
	Verb            string
	Short           string
	Action          string
	ConfirmQuestion string
	Path            string
}

func newTransitionCmd(p transitionParams) *cobra.Command {
	var (
		confirmFlag       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   p.Verb + " <connector_id>",
		Short: p.Short,
		Long: fmt.Sprintf("%s calls POST /api/v1/connectors/<connector_id>/%s.\n\n"+
			"The route is idempotent: calling it against an already-%s connector\n"+
			"returns the current state without re-running the transition.\n\n"+
			"Without --confirm, the verb prompts on stdin for confirmation;\n"+
			"--confirm skips the prompt for scripted use (CI pipelines, etc.).\n\n"+
			"Role: tenant_admin.",
			p.Verb, p.Verb, p.Action,
		),
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runTransition(cmd, p, transitionOptions{
				ConnectorID:       args[0],
				Confirm:           confirmFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&confirmFlag, "confirm", false,
		"skip the interactive confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type transitionOptions struct {
	ConnectorID       string
	Confirm           bool
	JSONOut           bool
	BackplaneOverride string
}

func runTransition(cmd *cobra.Command, p transitionParams, opts transitionOptions) error {
	// Prompt before resolving the backplane so the operator's only
	// interaction is the confirmation — if they hit 'n', they
	// haven't made a network call yet.
	if !opts.Confirm {
		prompt := fmt.Sprintf(p.ConfirmQuestion, opts.ConnectorID)
		if !confirm(cmd, prompt) {
			fmt.Fprintln(cmd.OutOrStdout(), "Aborted.")
			return errTransitionAborted
		}
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	path := fmt.Sprintf(p.Path, pathEscapeOpID(opts.ConnectorID))
	result, err := postTransition(cmd.Context(), backplaneURL, path)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printTransitionResult(cmd.OutOrStdout(), p.Verb, result)
	return nil
}

func postTransition(ctx context.Context, backplaneURL, path string) (*TransitionResponse, error) {
	respBody, err := doAuthedRequest(ctx, backplaneURL, "POST", path, []byte("{}"))
	if err != nil {
		return nil, err
	}
	var out TransitionResponse
	if err := decodeJSON(respBody, "transition", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// errTransitionAborted is the sentinel returned when the operator
// answered "no" at the confirmation prompt. The cobra command has
// SilenceErrors=true so the empty Error() doesn't double-print
// after the explicit "Aborted." line.
var errTransitionAborted = &silentTransitionError{}

type silentTransitionError struct{}

func (silentTransitionError) Error() string    { return "" }
func (s *silentTransitionError) ExitCode() int { return 1 }

func printTransitionResult(w io.Writer, verb string, r *TransitionResponse) {
	fmt.Fprintf(w, "%s %s — review_status=%s (%d groups, %d ops updated)\n",
		verb, r.ConnectorID, r.ReviewStatus,
		r.GroupsUpdated, r.OperationsUpdated,
	)
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newDeleteCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <session-id>",
		Short: "Delete one stored agent session and its files (DELETE /api/v1/sessions/{session_id})",
		Long: "delete removes one session: its files in the object storage and its " +
			"index rows. It cannot be undone. Without --confirm the command asks " +
			"first. Needs tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, args[0], confirm, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "do not ask before deleting")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print the result as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	return cmd
}

type deleteResult struct {
	SessionID string `json:"session_id"`
	Status    string `json:"status"`
}

func confirmPrompt(in io.Reader, w io.Writer, prompt string) bool {
	fmt.Fprintf(w, "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(in, &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

func runDelete(cmd *cobra.Command, sessionID string, confirm, jsonOut bool, backplaneOverride string) error {
	if !confirm {
		promptW := cmd.OutOrStdout()
		if jsonOut {
			promptW = cmd.ErrOrStderr()
		}
		prompt := fmt.Sprintf("Delete session %s and all its files? This cannot be undone.", sessionID)
		if !confirmPrompt(cmd.InOrStdin(), promptW, prompt) {
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), deleteResult{SessionID: sessionID, Status: "declined"})
			}
			fmt.Fprintf(cmd.OutOrStdout(), "declined: session %s not deleted\n", sessionID)
			return nil
		}
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := retryOn401(cmd.Context(), authed,
		func(ctx context.Context) (*api.DeleteAgentSessionApiV1SessionsSessionIdDeleteResponse, error) {
			return authed.DeleteAgentSessionApiV1SessionsSessionIdDeleteWithResponse(ctx, sessionID, nil)
		},
		func(r *api.DeleteAgentSessionApiV1SessionsSessionIdDeleteResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if resp.StatusCode() != http.StatusNoContent {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), deleteResult{SessionID: sessionID, Status: "deleted"})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted session %s and its files\n", sessionID)
	return nil
}

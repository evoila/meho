// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package tools hosts `meho admin tools ...`: admin work on the tool
// logins of the people of a tenant. Unlike the other `meho admin` verbs,
// it runs through the backplane like every operation: the call waits
// for approval, and MEHO writes the audit rows.
package tools

import (
	"errors"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the synthetic connector of the tools.* operations.
const ConnectorID = "tools-admin-1.x"

// SignOutOpID is the operation `meho admin tools sign-out` runs.
const SignOutOpID = "tools.sign_out"

var conn = dispatch.New(ConnectorID)

// NewRootCmd returns the `meho admin tools` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tools",
		Short:        "Admin work on the tool logins of people in your tenant",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSignOutCmd())
	return cmd
}

func newSignOutCmd() *cobra.Command {
	var (
		user              string
		tool              string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "sign-out",
		Short: "Sign out one tool of a person; their other tools keep working",
		Long: "sign-out runs the operation tools.sign_out. It ends the logins of\n" +
			"one person for one tool kind, for example Claude Desktop after a\n" +
			"laptop is lost. The person's other tools keep working.\n\n" +
			"--user is the person's Keycloak user id or exact username in MEHO's\n" +
			"own sign-in realm. Only people of your tenant are found.\n" +
			"--tool is one of: cli, claude-code, claude-desktop, other-mcp.\n" +
			"The web console cannot be signed out here.\n\n" +
			"You need the tenant_admin role. Each call waits for approval;\n" +
			"approve it in the web console or with `meho approvals approve`.\n" +
			"The tool's last access token works for a few more minutes, and\n" +
			"the tool can sign in again.\n\n" +
			"Exit codes: 0=ok or waiting for approval, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho admin tools sign-out --user <username> --tool claude-desktop",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if user == "" || tool == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("both --user and --tool are required"), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			params := map[string]any{"user": user, "tool": tool}
			r, err := conn.Call(cmd.Context(), backplaneURL, SignOutOpID, "", params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return conn.Render(cmd, SignOutOpID, r, jsonOut, nil)
		},
	}
	cmd.Flags().StringVar(&user, "user", "",
		"the person: Keycloak user id or exact username (required)")
	cmd.Flags().StringVar(&tool, "tool", "",
		"the tool kind: cli, claude-code, claude-desktop or other-mcp (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

// renderRequestError maps an error of the call to the right exit code.
func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	if api.IsTokenNotFound(err) || api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("no usable login for %s; run `meho login %s`",
				backplaneURL, backplaneURL)),
			jsonOut)
	}
	var apiErr *dispatch.APIResponseError
	if errors.As(err, &apiErr) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
}

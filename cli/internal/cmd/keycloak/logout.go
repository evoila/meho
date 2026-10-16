// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newUserLogoutCmd returns the `meho keycloak user logout` command
// (keycloak.user.logout — approval-gated, #3851). It ends every live
// session of one user, so their refresh tokens stop working at once. Keys
// on the user UUID (--id) or --username for resolution. It signs out one
// user only, never the whole realm.
func newUserLogoutCmd() *cobra.Command {
	var (
		f        writeFlags
		userUUID string
		username string
	)
	cmd := &cobra.Command{
		Use:   "logout",
		Short: "End all live sessions of one Keycloak user (approval-gated)",
		Long: "logout dispatches keycloak.user.logout. Keycloak ends every live\n" +
			"session of the user, so their refresh tokens stop working at once.\n" +
			"Pass --id (the user's UUID) or --username. Requires approval. Safe\n" +
			"to run twice: a user with no sessions is still a success. This does\n" +
			"not change the password (use reset-password for that) and never\n" +
			"signs out other users.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak user logout --target my-keycloak --username event-guest",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if userUUID == "" && username == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --id or --username is required"), f.jsonOut)
			}
			params := map[string]any{}
			if userUUID != "" {
				params["id"] = userUUID
			}
			if username != "" {
				params["username"] = username
			}
			return dispatchWrite(cmd, "keycloak.user.logout", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&userUUID, "id", "",
		"the user's internal UUID (skips name→UUID resolution)")
	cmd.Flags().StringVar(&username, "username", "",
		"the username (resolved to UUID when --id is absent)")
	return cmd
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newUserCmd returns the `meho keycloak user` parent with one sub-verb:
// `list` (keycloak.user.list).
func newUserCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "user",
		Short:        "Keycloak user sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newUserListCmd())
	return cmd
}

// newUserListCmd returns the `meho keycloak user list` command.
//
// Maps to op_id `keycloak.user.list`. GETs
// /admin/realms/{realm}/users and returns the users as {rows, total}.
// Optional --username maps to Keycloak's ?username= filter; --max caps
// the result count. User credential material is redacted from every row.
func newUserListCmd() *cobra.Command {
	var (
		targetName        string
		username          string
		maxResults        int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Keycloak users in the managed realm (credentials redacted)",
		Long: "list dispatches keycloak.user.list and renders the realm's users\n" +
			"(username / enabled / emailVerified / internal id). --username\n" +
			"filters to matching users; --max caps the result count. User\n" +
			"credential material is never surfaced. --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Use `meho keycloak role-mapping get --id <uuid>` for a user's\n" +
			"role assignments; the internal uuid is the `id` field of a row.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak user list --target rdc-keycloak\n" +
			"  meho keycloak user list --target rdc-keycloak --username operator-a\n" +
			"  meho keycloak user list --target rdc-keycloak --json | jq '.result.rows[].id'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runUserList(cmd, targetName, username, maxResults, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&username, "username", "",
		"filter to matching users by username (Keycloak ?username=)")
	cmd.Flags().IntVar(&maxResults, "max", 0,
		"cap on the number of users returned (0 = no cap)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runUserList(
	cmd *cobra.Command,
	targetName, username string,
	maxResults int,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{}
	if username != "" {
		params["username"] = username
	}
	if maxResults > 0 {
		params["max"] = maxResults
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.user.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.user.list", r, jsonOut, printUserList)
}

func printUserList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.user.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, total, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-28s %-8s %-14s %s\n", "USERNAME", "ENABLED", "EMAIL_VERIFIED", "INTERNAL_ID")
	for _, row := range rows {
		username := truncate(stringField(row, "username"), 28)
		enabled := fmt.Sprintf("%t", boolField(row, "enabled"))
		verified := fmt.Sprintf("%t", boolField(row, "emailVerified"))
		id := stringField(row, "id")
		fmt.Fprintf(w, "  %-28s %-8s %-14s %s\n", username, enabled, verified, id)
	}
	fmt.Fprintf(w, "  (%d users)\n", total)
}

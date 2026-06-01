// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newUserCmd returns the `meho keycloak user` parent with the read verb
// `list` (keycloak.user.list) and the approval-gated write verbs `create`
// (keycloak.user.create) and `reset-password`
// (keycloak.user.reset_password).
func newUserCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "user",
		Short:        "Keycloak user sub-verbs (list, create, reset-password)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newUserListCmd())
	cmd.AddCommand(newUserCreateCmd())
	cmd.AddCommand(newUserResetPasswordCmd())
	return cmd
}

// passwordSecretFlags is the shared flag bundle for the Vault-sourced
// password: the path (--password-secret-ref) plus the optional mount /
// key / temporary overrides. The password is NEVER a command-line flag —
// only its Vault location is — so it never lands in shell history, ps
// output, or the op params.
type passwordSecretFlags struct {
	ref       string
	mount     string
	key       string
	temporary bool
}

func (p *passwordSecretFlags) bind(cmd *cobra.Command, required bool) {
	cmd.Flags().StringVar(&p.ref, "password-secret-ref", "",
		"Vault KV-v2 path the password is read from (the password is never passed inline)")
	cmd.Flags().StringVar(&p.mount, "password-secret-mount", "",
		"Vault KV-v2 mount point (default 'secret')")
	cmd.Flags().StringVar(&p.key, "password-secret-key", "",
		"field within the Vault secret payload (default 'password')")
	cmd.Flags().BoolVar(&p.temporary, "temporary", false,
		"force a password change on first login")
	if required {
		if err := cmd.MarkFlagRequired("password-secret-ref"); err != nil {
			panic(err) // programmer error: the flag is defined directly above
		}
	}
}

// apply writes the password-secret params into the dispatch param map.
// Only non-empty values are written so the connector's defaults apply.
func (p *passwordSecretFlags) apply(params map[string]any) {
	if p.ref != "" {
		params["password_secret_ref"] = p.ref
	}
	if p.mount != "" {
		params["password_secret_mount"] = p.mount
	}
	if p.key != "" {
		params["password_secret_key"] = p.key
	}
	if p.temporary {
		params["temporary"] = true
	}
}

// newUserCreateCmd returns the `meho keycloak user create` command
// (keycloak.user.create — approval-gated). POSTs
// /admin/realms/{realm}/users with the UserRepresentation from
// --representation-file. The password is read from Vault
// (--password-secret-ref) and set as a credential — NEVER passed inline. A
// 409 already-exists is idempotent.
func newUserCreateCmd() *cobra.Command {
	var (
		f       writeFlags
		pw      passwordSecretFlags
		repFile string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a Keycloak user with a Vault-sourced password (approval-gated)",
		Long: "create dispatches keycloak.user.create with the UserRepresentation\n" +
			"from --representation-file (JSON). The password is read from Vault\n" +
			"(--password-secret-ref, optional --password-secret-mount /\n" +
			"--password-secret-key) and set as a credential — it is NEVER passed\n" +
			"on the command line or in op params. Requires approval; a 409\n" +
			"already-exists is an idempotent success. The password is never\n" +
			"returned.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak user create --target rdc-keycloak -f user-operator-a.json --password-secret-ref rdc-hetzner-dc/keycloak/operator-a",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			rep, serr := loadRepresentation(repFile)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			params := map[string]any{"representation": rep}
			pw.apply(params)
			return dispatchWrite(cmd, "keycloak.user.create", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	pw.bind(cmd, false) // password optional on create (a user may be SSO-only)
	cmd.Flags().StringVarP(&repFile, "representation-file", "f", "",
		"path to a JSON file with the UserRepresentation body (required)")
	if err := cmd.MarkFlagRequired("representation-file"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// newUserResetPasswordCmd returns the `meho keycloak user reset-password`
// command (keycloak.user.reset_password — approval-gated). PUTs
// .../users/{id}/reset-password with a CredentialRepresentation whose
// value is read from Vault — NEVER passed inline. Keys on the user UUID
// (--id) or --username for resolution.
func newUserResetPasswordCmd() *cobra.Command {
	var (
		f        writeFlags
		pw       passwordSecretFlags
		userUUID string
		username string
	)
	cmd := &cobra.Command{
		Use:   "reset-password",
		Short: "Reset a Keycloak user's password from Vault (approval-gated)",
		Long: "reset-password dispatches keycloak.user.reset_password. The new\n" +
			"password is read from Vault (--password-secret-ref) — NEVER passed\n" +
			"on the command line. Keys on the user UUID — pass --id directly, or\n" +
			"pass --username for name→UUID resolution. Requires approval.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak user reset-password --target rdc-keycloak --username operator-a --password-secret-ref rdc-hetzner-dc/keycloak/operator-a",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if userUUID == "" && username == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --id or --username is required"), f.jsonOut)
			}
			params := map[string]any{}
			pw.apply(params)
			if userUUID != "" {
				params["id"] = userUUID
			}
			if username != "" {
				params["username"] = username
			}
			return dispatchWrite(cmd, "keycloak.user.reset_password", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	pw.bind(cmd, true) // password required on reset
	cmd.Flags().StringVar(&userUUID, "id", "",
		"the user's internal UUID (skips name→UUID resolution)")
	cmd.Flags().StringVar(&username, "username", "",
		"the username (resolved to UUID when --id is absent)")
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

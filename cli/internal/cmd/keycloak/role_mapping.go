// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRoleMappingCmd returns the `meho keycloak role-mapping` parent with
// the read verb `get` (keycloak.role_mapping.get) and the approval-gated
// write verb `assign` (keycloak.role_mapping.assign — a privilege grant).
func newRoleMappingCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "role-mapping",
		Short:        "Keycloak role-mapping sub-verbs (get, assign)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRoleMappingGetCmd())
	cmd.AddCommand(newRoleMappingAssignCmd())
	return cmd
}

// newRoleMappingAssignCmd returns the `meho keycloak role-mapping assign`
// command (keycloak.role_mapping.assign — approval-gated, dangerous: a
// privilege grant). POSTs .../users/{id}/role-mappings/realm with the
// resolved RoleRepresentations for the named --role values. Keys on the
// user UUID (--id) or --username for resolution.
func newRoleMappingAssignCmd() *cobra.Command {
	var (
		f        writeFlags
		userUUID string
		username string
		roles    []string
	)
	cmd := &cobra.Command{
		Use:   "assign",
		Short: "Grant realm roles to a Keycloak user (approval-gated, privilege grant)",
		Long: "assign dispatches keycloak.role_mapping.assign — a privilege grant\n" +
			"(dangerous, requires approval). Keys on the user UUID — pass --id\n" +
			"directly, or pass --username for resolution. Pass --role once per\n" +
			"realm role name to grant. Each role name is resolved to its\n" +
			"representation; an unknown role errors. Re-assigning a held role is\n" +
			"a server-side no-op.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak role-mapping assign --target rdc-keycloak --username operator-a --role tenant_admin",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if userUUID == "" && username == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --id or --username is required"), f.jsonOut)
			}
			if len(roles) == 0 {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("at least one --role is required"), f.jsonOut)
			}
			rolesAny := make([]any, len(roles))
			for i, r := range roles {
				rolesAny[i] = r
			}
			params := map[string]any{"roles": rolesAny}
			if userUUID != "" {
				params["id"] = userUUID
			}
			if username != "" {
				params["username"] = username
			}
			return dispatchWrite(cmd, "keycloak.role_mapping.assign", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&userUUID, "id", "",
		"the user's internal UUID (skips name→UUID resolution)")
	cmd.Flags().StringVar(&username, "username", "",
		"the username (resolved to UUID when --id is absent)")
	cmd.Flags().StringArrayVar(&roles, "role", nil,
		"realm role name to grant (repeatable)")
	return cmd
}

// newRoleMappingGetCmd returns the `meho keycloak role-mapping get`
// command.
//
// Maps to op_id `keycloak.role_mapping.get`. GETs
// /admin/realms/{realm}/users/{id}/role-mappings where --id is the
// user's internal UUID (from `keycloak user list`). Returns the
// MappingsRepresentation: realmMappings (realm-level roles) and
// clientMappings (per-client roles).
func newRoleMappingGetCmd() *cobra.Command {
	var (
		targetName        string
		userUUID          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get",
		Short: "Read a Keycloak user's realm + client role mappings by UUID",
		Long: "get dispatches keycloak.role_mapping.get for the user whose\n" +
			"internal UUID is --id (the `id` field from\n" +
			"`meho keycloak user list`). Renders the realm-level role names\n" +
			"and the per-client role names. --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak role-mapping get --target rdc-keycloak --id 22222222-2222-2222-2222-222222222222\n" +
			"  meho keycloak role-mapping get --target rdc-keycloak --id <uuid> --json | jq '.result.role_mappings'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRoleMappingGet(cmd, targetName, userUUID, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&userUUID, "id", "",
		"the user's internal UUID (from `meho keycloak user list`) (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("id"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func runRoleMappingGet(
	cmd *cobra.Command,
	targetName, userUUID string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"id": userUUID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.role_mapping.get", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.role_mapping.get", r, jsonOut, printRoleMappingGet)
}

func printRoleMappingGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.role_mapping.get — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	mappings, err := decodeWrappedObject(r.Result, "role_mappings")
	if err != nil || mappings == nil {
		fallbackResultRender(w, r)
		return
	}
	realmRoles := roleNames(mappings["realmMappings"])
	fmt.Fprintf(w, "  realm roles:   %s\n", joinOrNone(realmRoles))

	if clientMappings, ok := mappings["clientMappings"].(map[string]any); ok && len(clientMappings) > 0 {
		fmt.Fprintln(w, "  client roles:")
		for client, raw := range clientMappings {
			cm, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			names := roleNames(cm["mappings"])
			fmt.Fprintf(w, "    %-24s %s\n", client+":", joinOrNone(names))
		}
	} else {
		fmt.Fprintln(w, "  client roles:  (none)")
	}
}

// roleNames pulls the `name` field from a JSON array of role-mapping
// objects, returning the names in order.
func roleNames(v any) []string {
	arr, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, item := range arr {
		if m, ok := item.(map[string]any); ok {
			if name := stringField(m, "name"); name != "" {
				out = append(out, name)
			}
		}
	}
	return out
}

// joinOrNone renders a comma-joined list, or "(none)" when empty.
func joinOrNone(names []string) string {
	if len(names) == 0 {
		return "(none)"
	}
	return strings.Join(names, ", ")
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newClientScopeCmd returns the `meho keycloak client-scope` parent with
// the read verb `list` (keycloak.client_scope.list) and the approval-gated
// write verb `create` (keycloak.client_scope.create).
func newClientScopeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "client-scope",
		Short:        "Keycloak client-scope sub-verbs (list, create)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newClientScopeListCmd())
	cmd.AddCommand(newClientScopeCreateCmd())
	return cmd
}

// newClientScopeCreateCmd returns the `meho keycloak client-scope create`
// command (keycloak.client_scope.create — approval-gated). POSTs
// /admin/realms/{realm}/client-scopes with the ClientScopeRepresentation
// from --representation-file. A 409 already-exists is idempotent.
func newClientScopeCreateCmd() *cobra.Command {
	var (
		f       writeFlags
		repFile string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a Keycloak client scope (approval-gated)",
		Long: "create dispatches keycloak.client_scope.create with the\n" +
			"ClientScopeRepresentation (its protocolMappers ride in the body)\n" +
			"read from --representation-file (JSON). Requires approval; a 409\n" +
			"already-exists is an idempotent success.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak client-scope create --target rdc-keycloak -f scope-roles.json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			rep, serr := loadRepresentation(repFile)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			return dispatchWrite(cmd, "keycloak.client_scope.create", f.targetName,
				map[string]any{"representation": rep}, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVarP(&repFile, "representation-file", "f", "",
		"path to a JSON file with the ClientScopeRepresentation body (required)")
	if err := cmd.MarkFlagRequired("representation-file"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// newClientScopeListCmd returns the `meho keycloak client-scope list`
// command.
//
// Maps to op_id `keycloak.client_scope.list`. GETs
// /admin/realms/{realm}/client-scopes and returns the scopes as
// {rows, total}. Each row is a ClientScopeRepresentation carrying the
// scope's protocol mappers and attributes.
func newClientScopeListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Keycloak client scopes in the managed realm",
		Long: "list dispatches keycloak.client_scope.list and renders the realm's\n" +
			"client scopes (name / protocol / protocol-mapper count) — the\n" +
			"reusable mapper/role bundles clients attach as default or\n" +
			"optional scopes. --json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak client-scope list --target rdc-keycloak\n" +
			"  meho keycloak client-scope list --target rdc-keycloak --json | jq '.result.rows[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClientScopeList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runClientScopeList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.client_scope.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.client_scope.list", r, jsonOut, printClientScopeList)
}

func printClientScopeList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.client_scope.list — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-30s %-18s %s\n", "NAME", "PROTOCOL", "MAPPERS")
	for _, row := range rows {
		name := truncate(stringField(row, "name"), 30)
		protocol := stringField(row, "protocol")
		mapperCount := 0
		if mappers, ok := row["protocolMappers"].([]any); ok {
			mapperCount = len(mappers)
		}
		fmt.Fprintf(w, "  %-30s %-18s %d\n", name, protocol, mapperCount)
	}
	fmt.Fprintf(w, "  (%d client scopes)\n", total)
}

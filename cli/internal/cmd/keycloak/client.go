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

// newClientCmd returns the `meho keycloak client` parent with two
// sub-verbs: `list` (keycloak.client.list) and `get`
// (keycloak.client.get).
func newClientCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "client",
		Short:        "Keycloak client sub-verbs (list, get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newClientListCmd())
	cmd.AddCommand(newClientGetCmd())
	return cmd
}

// newClientListCmd returns the `meho keycloak client list` command.
//
// Maps to op_id `keycloak.client.list`. GETs
// /admin/realms/{realm}/clients and returns the clients as
// {rows, total}. Optional --client-id maps to Keycloak's ?clientId=
// exact-match filter; --max caps the result count. Confidential-client
// secrets are redacted from every row.
func newClientListCmd() *cobra.Command {
	var (
		targetName        string
		clientID          string
		maxResults        int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Keycloak clients in the managed realm (secrets redacted)",
		Long: "list dispatches keycloak.client.list and renders the clients as\n" +
			"a table of clientId / enabled / publicClient / internal id.\n" +
			"--client-id filters to a single client by its human clientId;\n" +
			"--max caps the result count. Each row's secret is redacted.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Use `meho keycloak client get --id <uuid>` for one client's full\n" +
			"config; the internal uuid is the `id` field of a list row.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak client list --target rdc-keycloak\n" +
			"  meho keycloak client list --target rdc-keycloak --client-id meho-backplane\n" +
			"  meho keycloak client list --target rdc-keycloak --json | jq '.result.rows[].id'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClientList(cmd, targetName, clientID, maxResults, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&clientID, "client-id", "",
		"filter to a single client by its human clientId (Keycloak ?clientId=)")
	cmd.Flags().IntVar(&maxResults, "max", 0,
		"cap on the number of clients returned (0 = no cap)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runClientList(
	cmd *cobra.Command,
	targetName, clientID string,
	maxResults int,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{}
	if clientID != "" {
		params["client_id"] = clientID
	}
	if maxResults > 0 {
		params["max"] = maxResults
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.client.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.client.list", r, jsonOut, printClientList)
}

func printClientList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.client.list — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-30s %-8s %-7s %s\n", "CLIENT_ID", "ENABLED", "PUBLIC", "INTERNAL_ID")
	for _, row := range rows {
		clientID := truncate(stringField(row, "clientId"), 30)
		enabled := fmt.Sprintf("%t", boolField(row, "enabled"))
		public := fmt.Sprintf("%t", boolField(row, "publicClient"))
		id := stringField(row, "id")
		fmt.Fprintf(w, "  %-30s %-8s %-7s %s\n", clientID, enabled, public, id)
	}
	fmt.Fprintf(w, "  (%d clients)\n", total)
}

// newClientGetCmd returns the `meho keycloak client get` command.
//
// Maps to op_id `keycloak.client.get`. GETs
// /admin/realms/{realm}/clients/{id} where --id is the client's
// internal UUID (the `id` field from `keycloak client list`, NOT the
// human clientId). Returns the full ClientRepresentation; the client
// secret is redacted.
func newClientGetCmd() *cobra.Command {
	var (
		targetName        string
		clientUUID        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get",
		Short: "Read one Keycloak client's full config by internal UUID (secret redacted)",
		Long: "get dispatches keycloak.client.get for the client whose internal\n" +
			"UUID is --id (the `id` field from `meho keycloak client list`,\n" +
			"NOT the human clientId). Renders the redirect URIs, web origins,\n" +
			"and protocol mappers; the client secret is redacted. --json emits\n" +
			"the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak client get --target rdc-keycloak --id 11111111-1111-1111-1111-111111111111\n" +
			"  meho keycloak client get --target rdc-keycloak --id <uuid> --json | jq '.result.client'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClientGet(cmd, targetName, clientUUID, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&clientUUID, "id", "",
		"the client's internal UUID (from `meho keycloak client list`) (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("id"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func runClientGet(
	cmd *cobra.Command,
	targetName, clientUUID string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"id": clientUUID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.client.get", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.client.get", r, jsonOut, printClientGet)
}

func printClientGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.client.get — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	client, err := decodeWrappedObject(r.Result, "client")
	if err != nil || client == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  clientId:      %s\n", stringField(client, "clientId"))
	fmt.Fprintf(w, "  id:            %s\n", stringField(client, "id"))
	fmt.Fprintf(w, "  enabled:       %t\n", boolField(client, "enabled"))
	fmt.Fprintf(w, "  publicClient:  %t\n", boolField(client, "publicClient"))
	printStringList(w, "redirectUris", client["redirectUris"])
	printStringList(w, "webOrigins", client["webOrigins"])
	if mappers, ok := client["protocolMappers"].([]any); ok && len(mappers) > 0 {
		names := make([]string, 0, len(mappers))
		for _, m := range mappers {
			if md, ok := m.(map[string]any); ok {
				names = append(names, stringField(md, "name"))
			}
		}
		fmt.Fprintf(w, "  protocolMappers: %s\n", strings.Join(names, ", "))
	}
}

// printStringList renders a JSON array of strings under a label,
// skipping the line entirely when the value is absent or empty.
func printStringList(w io.Writer, label string, v any) {
	arr, ok := v.([]any)
	if !ok || len(arr) == 0 {
		return
	}
	parts := make([]string, 0, len(arr))
	for _, item := range arr {
		if s, ok := item.(string); ok {
			parts = append(parts, s)
		}
	}
	fmt.Fprintf(w, "  %-14s %s\n", label+":", strings.Join(parts, ", "))
}

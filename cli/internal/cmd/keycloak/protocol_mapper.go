// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newProtocolMapperCmd returns the `meho keycloak protocol-mapper` parent
// with the approval-gated write verb `create`
// (keycloak.protocol_mapper.create) — the op that wires the tenant_id /
// tenant_role claims the backplane row-scopes on. There is no read verb in
// this sub-tree; protocol mappers are read via `keycloak client get`
// (they ride in the ClientRepresentation).
func newProtocolMapperCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "protocol-mapper",
		Short:        "Keycloak protocol-mapper sub-verbs (create)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newProtocolMapperCreateCmd())
	return cmd
}

// newProtocolMapperCreateCmd returns the `meho keycloak protocol-mapper
// create` command (keycloak.protocol_mapper.create — approval-gated).
// POSTs .../clients/{id}/protocol-mappers/models with the
// ProtocolMapperRepresentation from --representation-file. Keys on the
// client UUID — pass --id directly, or pass --client-id for name→UUID
// resolution. A 409 already-exists is idempotent.
func newProtocolMapperCreateCmd() *cobra.Command {
	var (
		f          writeFlags
		repFile    string
		clientUUID string
		clientID   string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Add a protocol mapper to a Keycloak client (approval-gated)",
		Long: "create dispatches keycloak.protocol_mapper.create with the\n" +
			"ProtocolMapperRepresentation from --representation-file (JSON) —\n" +
			"e.g. the tenant_id / tenant_role claim mappers the backplane\n" +
			"row-scopes on. Keys on the client UUID — pass --id directly, or\n" +
			"pass --client-id (the human clientId) for resolution. Requires\n" +
			"approval; a 409 already-exists is an idempotent success.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak protocol-mapper create --target rdc-keycloak --client-id meho-web -f mapper-tenant-id.json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if clientUUID == "" && clientID == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --id or --client-id is required"), f.jsonOut)
			}
			rep, serr := loadRepresentation(repFile)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			params := map[string]any{"representation": rep}
			if clientUUID != "" {
				params["id"] = clientUUID
			}
			if clientID != "" {
				params["client_id"] = clientID
			}
			return dispatchWrite(cmd, "keycloak.protocol_mapper.create", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVarP(&repFile, "representation-file", "f", "",
		"path to a JSON file with the ProtocolMapperRepresentation body (required)")
	cmd.Flags().StringVar(&clientUUID, "id", "",
		"the client's internal UUID (skips name→UUID resolution)")
	cmd.Flags().StringVar(&clientID, "client-id", "",
		"the human clientId (resolved to UUID when --id is absent)")
	if err := cmd.MarkFlagRequired("representation-file"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

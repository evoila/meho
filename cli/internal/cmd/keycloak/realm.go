// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRealmCmd returns the `meho keycloak realm` parent with the read verb
// `get` (keycloak.realm.get) and the approval-gated write verbs `create`
// (keycloak.realm.create) and `update` (keycloak.realm.update).
func newRealmCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "realm",
		Short:        "Keycloak realm sub-verbs (get, create, update)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRealmGetCmd())
	cmd.AddCommand(newRealmCreateCmd())
	cmd.AddCommand(newRealmUpdateCmd())
	return cmd
}

// newRealmCreateCmd returns the `meho keycloak realm create` command
// (keycloak.realm.create — approval-gated). POSTs /admin/realms with the
// RealmRepresentation from --representation-file. A 409 already-exists is
// treated as an idempotent success.
func newRealmCreateCmd() *cobra.Command {
	var (
		f       writeFlags
		repFile string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a Keycloak realm (approval-gated)",
		Long: "create dispatches keycloak.realm.create with the\n" +
			"RealmRepresentation read from --representation-file (JSON). The op\n" +
			"requires approval; a 409 already-exists is an idempotent success.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak realm create --target rdc-keycloak -f realm-evba.json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			rep, serr := loadRepresentation(repFile)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			return dispatchWrite(cmd, "keycloak.realm.create", f.targetName,
				map[string]any{"representation": rep}, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVarP(&repFile, "representation-file", "f", "",
		"path to a JSON file with the RealmRepresentation body (required)")
	if err := cmd.MarkFlagRequired("representation-file"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// newRealmUpdateCmd returns the `meho keycloak realm update` command
// (keycloak.realm.update — approval-gated). PUTs /admin/realms/{realm}
// (default the target's managed realm; --realm overrides) with the partial
// RealmRepresentation from --representation-file.
func newRealmUpdateCmd() *cobra.Command {
	var (
		f         writeFlags
		repFile   string
		realmName string
	)
	cmd := &cobra.Command{
		Use:   "update",
		Short: "Update a Keycloak realm's top-level config (approval-gated)",
		Long: "update dispatches keycloak.realm.update with the partial\n" +
			"RealmRepresentation from --representation-file (JSON). Defaults to\n" +
			"the target's managed realm; --realm overrides. Requires approval.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak realm update --target rdc-keycloak -f realm-patch.json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			rep, serr := loadRepresentation(repFile)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			params := map[string]any{"representation": rep}
			if realmName != "" {
				params["realm"] = realmName
			}
			return dispatchWrite(cmd, "keycloak.realm.update", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVarP(&repFile, "representation-file", "f", "",
		"path to a JSON file with the partial RealmRepresentation body (required)")
	cmd.Flags().StringVar(&realmName, "realm", "",
		"realm to update (defaults to the target's managed realm)")
	if err := cmd.MarkFlagRequired("representation-file"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// newRealmGetCmd returns the `meho keycloak realm get` command.
//
// Maps to op_id `keycloak.realm.get`. GETs /admin/realms/{realm}
// against the target's managed realm and returns the
// RealmRepresentation (login settings, token lifespans, themes, SMTP,
// realm-wide policy). Any nested secret is redacted.
func newRealmGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get",
		Short: "Read the managed realm's top-level configuration",
		Long: "get dispatches keycloak.realm.get and renders the managed\n" +
			"realm's top-level config (realm / enabled / sslRequired /\n" +
			"loginTheme / token lifespans). Secrets are redacted by the\n" +
			"connector. --json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak realm get --target rdc-keycloak\n" +
			"  meho keycloak realm get --target rdc-keycloak --json | jq '.result.realm'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRealmGet(cmd, targetName, jsonOut, backplaneOverride)
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

func runRealmGet(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.realm.get", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "keycloak.realm.get", r, jsonOut, printRealmGet)
}

func printRealmGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.realm.get — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	realm, err := decodeWrappedObject(r.Result, "realm")
	if err != nil || realm == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"realm", "enabled", "sslRequired", "loginTheme", "accessTokenLifespan"} {
		v, ok := realm[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-20s %v\n", key+":", v)
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRealmCmd returns the `meho keycloak realm` parent with one
// sub-verb: `get` (keycloak.realm.get).
func newRealmCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "realm",
		Short:        "Keycloak realm sub-verbs (get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRealmGetCmd())
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

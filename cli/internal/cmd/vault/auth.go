// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vault

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// Identity op IDs registered by G3.3-T3 (#547). All read-only. The
// list verbs take no params; the read verbs take a single identifier
// param (`username` for userpass, `role_name` for approle) per the
// G3.3-T3 schemas.
const (
	opAuthUserpassList = "vault.auth.userpass.list"
	opAuthUserpassRead = "vault.auth.userpass.read"
	opAuthApproleList  = "vault.auth.approle.list"
	opAuthApproleRead  = "vault.auth.approle.read"
)

// newAuthCmd returns the `meho vault auth` parent command and
// assembles its four read-only identity verbs.
func newAuthCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "auth",
		Short:        "Vault identity verbs (userpass / approle, read-only)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAuthListVerbCmd(
		"userpass-list", opAuthUserpassList,
		"List configured userpass users",
		"userpass-list dispatches op_id=\"vault.auth.userpass.list\" against\n"+
			"connector_id=\"vault-1.x\". Read-only; result carries the\n"+
			"userpass roster (set-shaped — large rosters return a JSONFlux\n"+
			"handle + sample, drill in with the result verbs).",
		"  meho vault auth userpass-list --target rdc-vault",
	))
	cmd.AddCommand(newAuthReadVerbCmd(
		"userpass-read", "<user>", "username", opAuthUserpassRead,
		"Read one userpass user (policies, ttl)",
		"userpass-read dispatches op_id=\"vault.auth.userpass.read\" against\n"+
			"connector_id=\"vault-1.x\". <user> is the userpass username (no\n"+
			"mount prefix). Read-only; result carries the user's policies +\n"+
			"token ttls.",
		"  meho vault auth userpass-read --target rdc-vault svc-deploy",
	))
	cmd.AddCommand(newAuthListVerbCmd(
		"approle-list", opAuthApproleList,
		"List configured approle role names",
		"approle-list dispatches op_id=\"vault.auth.approle.list\" against\n"+
			"connector_id=\"vault-1.x\". Read-only; result carries the\n"+
			"approle role-name roster.",
		"  meho vault auth approle-list --target rdc-vault",
	))
	cmd.AddCommand(newAuthReadVerbCmd(
		"approle-read", "<role>", "role_name", opAuthApproleRead,
		"Read one approle role (policies, ttls)",
		"approle-read dispatches op_id=\"vault.auth.approle.read\" against\n"+
			"connector_id=\"vault-1.x\". <role> is the AppRole role name (no\n"+
			"mount prefix). Read-only; result carries the role's policies +\n"+
			"token/secret-id ttls.",
		"  meho vault auth approle-read --target rdc-vault ci-runner",
	))
	return cmd
}

// authAddrFlags binds the shared address flags onto a command. Split
// out so the list-verb and read-verb factories share one flag-wiring
// implementation.
type authAddrFlags struct {
	targetName        string
	jsonOut           bool
	backplaneOverride string
}

func bindAuthAddrFlags(cmd *cobra.Command) *authAddrFlags {
	f := &authAddrFlags{}
	cmd.Flags().StringVar(&f.targetName, "target", "",
		"Vault target slug to dispatch against (resolved server-side)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return f
}

// dispatchAuth runs the resolve → dispatch → render pipeline shared by
// every auth verb. params is nil for the list verbs.
func dispatchAuth(cmd *cobra.Command, f *authAddrFlags, opID string, params map[string]any) error {
	backplaneURL, err := backplane.Resolve(f.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), f.jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, f.targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, f.jsonOut)
	}
	return conn.Render(cmd, opID, r, f.jsonOut, nil)
}

// newAuthListVerbCmd builds one no-arg, no-param list verb (userpass-
// list / approle-list). They share the exact same shape, differing
// only in op_id + help text.
func newAuthListVerbCmd(use, opID, short, long, example string) *cobra.Command {
	cmd := &cobra.Command{
		Use:           use,
		Short:         short,
		Long:          long + "\n\nExit codes mirror meho operation call.",
		Example:       example,
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindAuthAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return dispatchAuth(cmd, f, opID, nil)
	}
	return cmd
}

// newAuthReadVerbCmd builds one single-identifier read verb (userpass-
// read <user> / approle-read <role>). paramKey is the op's schema key
// for the identifier (`username` or `role_name`); useArg is the
// help-text placeholder (`<user>` / `<role>`).
func newAuthReadVerbCmd(verb, useArg, paramKey, opID, short, long, example string) *cobra.Command {
	cmd := &cobra.Command{
		Use:           verb + " " + useArg,
		Short:         short,
		Long:          long + "\n\nExit codes mirror meho operation call.",
		Example:       example,
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindAuthAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return dispatchAuth(cmd, f, opID, map[string]any{paramKey: args[0]})
	}
	return cmd
}

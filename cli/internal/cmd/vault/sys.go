// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vault

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// System op IDs registered by G3.3-T2 (#546). Read-only diagnostics;
// none take params.
const (
	opSysHealth     = "vault.sys.health"
	opSysSealStatus = "vault.sys.seal_status"
	opSysMountsList = "vault.sys.mounts.list"
	opSysAuthList   = "vault.sys.auth.list"
)

// newSysCmd returns the `meho vault sys` parent command and assembles
// its four read-only diagnostic verbs.
func newSysCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "sys",
		Short:        "Vault system diagnostics (health / seal-status / mounts-list / auth-list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSysVerbCmd(
		"health", opSysHealth,
		"Report Vault health (initialized / sealed / standby)",
		"health dispatches op_id=\"vault.sys.health\" against connector_id=\n"+
			"\"vault-1.x\". Read-only; the result envelope carries\n"+
			"{ok: <bool>, detail: <str>} plus the raw sys/health fields.",
		"  meho vault sys health --target rdc-vault",
	))
	cmd.AddCommand(newSysVerbCmd(
		"seal-status", opSysSealStatus,
		"Read the Vault seal state",
		"seal-status dispatches op_id=\"vault.sys.seal_status\" against\n"+
			"connector_id=\"vault-1.x\". Read-only; result carries\n"+
			"{sealed: <bool>, initialized: <bool>, ...}.",
		"  meho vault sys seal-status --target rdc-vault",
	))
	cmd.AddCommand(newSysVerbCmd(
		"mounts-list", opSysMountsList,
		"List enabled secret backends",
		"mounts-list dispatches op_id=\"vault.sys.mounts.list\" against\n"+
			"connector_id=\"vault-1.x\". Read-only; result carries the\n"+
			"enabled secret-engine mount map.",
		"  meho vault sys mounts-list --target rdc-vault",
	))
	cmd.AddCommand(newSysVerbCmd(
		"auth-list", opSysAuthList,
		"List enabled auth backends",
		"auth-list dispatches op_id=\"vault.sys.auth.list\" against\n"+
			"connector_id=\"vault-1.x\". Read-only; result carries the\n"+
			"enabled auth-method map.",
		"  meho vault sys auth-list --target rdc-vault",
	))
	return cmd
}

// newSysVerbCmd builds one no-arg, no-param sys verb. The four sys ops
// share the exact same shape (resolve target → dispatch → generic
// render) and differ only in op_id + help text, so a single factory
// avoids four near-identical command bodies. The trailing "Exit codes
// mirror meho operation call." sentence is appended here so every
// verb's help is consistent without repeating it in each caller.
func newSysVerbCmd(use, opID, short, long, example string) *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           use,
		Short:         short,
		Long:          long + "\n\nExit codes mirror meho operation call.",
		Example:       example,
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := resolveBackplane(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
			}
			r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, nil)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return renderCallResult(cmd, opID, r, jsonOut, nil)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"Vault target slug to dispatch against (resolved server-side)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

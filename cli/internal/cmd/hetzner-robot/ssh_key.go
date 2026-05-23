// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newSSHKeyCmd returns `meho hetzner-robot ssh-key` with the list subcommand.
func newSSHKeyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "ssh-key",
		Short:        "List SSH public keys registered in the Hetzner Robot portal",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSSHKeyListCmd())
	return cmd
}

func newSSHKeyListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all SSH public keys registered in the Hetzner Robot portal",
		Long: "list dispatches GET:/key against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of key fingerprint, name, type, and size.\n" +
			"Use the fingerprint when referencing a key in a reinstall request.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot ssh-key list --target rdc-robot\n" +
			"  meho hetzner-robot ssh-key list --target rdc-robot --json | jq '.result[].key.fingerprint'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSSHKeyList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSSHKeyList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/key", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/key", r, jsonOut, printSSHKeyList)
}

func printSSHKeyList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/key — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeRobotList(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(items) == 0 {
		fmt.Fprintln(w, "  (0 SSH keys)")
		return
	}
	fmt.Fprintf(w, "%-50s %-30s %-8s %s\n", "fingerprint", "name", "type", "size")
	for _, item := range items {
		key := getNestedObj(item, "key")
		fingerprint, _ := key["fingerprint"].(string)
		name, _ := key["name"].(string)
		keyType, _ := key["type"].(string)
		size := ""
		if v, ok := key["size"].(float64); ok {
			size = fmt.Sprintf("%d", int(v))
		}
		fmt.Fprintf(w, "%-50s %-30s %-8s %s\n",
			truncate(fingerprint, 50),
			truncate(name, 30),
			keyType,
			size,
		)
	}
}

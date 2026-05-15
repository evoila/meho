// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newHostCmd returns the `meho vmware host` parent command and
// assembles its two verbs (list / evacuate).
func newHostCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "host",
		Short:        "vSphere host verbs (list / evacuate)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newHostListCmd())
	cmd.AddCommand(newHostEvacuateCmd())
	return cmd
}

// newHostListCmd returns `meho vmware host list`. Maps to
// GET:/vcenter/host.
func newHostListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ESXi hosts on a vCenter target",
		Long: "list dispatches GET:/vcenter/host against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Renders moid / name / connection_state /\n" +
			"power_state for human eyes; --json emits the full OperationResult\n" +
			"envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware host list --target rdc-vcenter\n" +
			"  meho vmware host list --target rdc-vcenter --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runHostList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runHostList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/vcenter/host", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/vcenter/host", r, jsonOut, printHostList)
}

func printHostList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vcenter/host — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 hosts)")
		return
	}
	fmt.Fprintf(w, "%-16s %-30s %-16s %-14s\n", "moid", "name", "connection", "power")
	for _, e := range entries {
		fmt.Fprintf(w, "%-16s %-30s %-16s %-14s\n",
			truncate(stringField(e, "host"), 16),
			truncate(stringField(e, "name"), 30),
			truncate(stringField(e, "connection_state"), 16),
			truncate(stringField(e, "power_state"), 14),
		)
	}
}

// newHostEvacuateCmd returns `meho vmware host evacuate <name>`.
// Resolves the name to a moid then dispatches the composite op_id
// vmware.composite.host.evacuate (ships in T6 #509).
func newHostEvacuateCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "evacuate <name-or-id>",
		Short: "Evacuate a host (composite: vMotion all VMs off then maintenance-mode)",
		Long: "evacuate dispatches op_id=\"vmware.composite.host.evacuate\" against\n" +
			"the connector_id=\"vmware-rest-9.0\" connector. The composite\n" +
			"orchestrates vMotion of every VM off the host then transitions\n" +
			"the host into maintenance mode (T6 #509).\n\n" +
			"<name-or-id> accepts a host name (resolved client-side to a moid)\n" +
			"or a moid directly. Resolution failures (not-found, ambiguous)\n" +
			"exit with status 1 and a message listing candidates.\n\n" +
			"Pre-merge of T6 (#509), the dispatcher returns \"operation not\n" +
			"found\" which surfaces in the standard error trailer.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware host evacuate esxi-01 --target rdc-vcenter\n" +
			"  meho vmware host evacuate host-23 --target rdc-vcenter --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runHostEvacuate(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runHostEvacuate(cmd *cobra.Command, nameOrID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	moid, err := resolveName(cmd.Context(), backplaneURL, targetName, "host", nameOrID)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			&output.StructuredError{Code: "resolve_failed", Detail: err.Error(), Exit: 1},
			jsonOut)
	}
	opID := "vmware.composite.host.evacuate"
	params := map[string]any{"host": moid}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, nil)
}

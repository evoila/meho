// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// The three flat list verbs (datacenter / datastore / network) share
// the same shape: dispatch GET:/vcenter/<kind> with no params and
// render a 2-column moid/name table. The newSimpleListCmd factory
// produces the cobra commands; per-kind specialisations live in the
// per-verb factories below.

// simpleListSpec captures the per-verb knobs newSimpleListCmd needs:
// the cobra Use+Short strings, the op_id, and the moid field name
// (which differs per kind — see resolve.go's moidFieldForKind).
type simpleListSpec struct {
	use      string
	short    string
	long     string
	example  string
	opID     string
	moidKey  string
	zeroText string
}

func newSimpleListCmd(spec simpleListSpec) *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           spec.use,
		Short:         spec.short,
		Long:          spec.long,
		Example:       spec.example,
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSimpleList(cmd, spec, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSimpleList(cmd *cobra.Command, spec simpleListSpec, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, spec.opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, spec.opID, r, jsonOut, makeSimpleListPrinter(spec))
}

func makeSimpleListPrinter(spec simpleListSpec) func(io.Writer, *CallResult) {
	return func(w io.Writer, r *CallResult) {
		fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, spec.opID, r.Status, r.DurationMs)
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
			fmt.Fprintln(w, spec.zeroText)
			return
		}
		fmt.Fprintf(w, "%-20s %-40s\n", "moid", "name")
		for _, e := range entries {
			fmt.Fprintf(w, "%-20s %-40s\n",
				truncate(stringField(e, spec.moidKey), 20),
				truncate(stringField(e, "name"), 40),
			)
		}
	}
}

// newDatacenterCmd returns `meho vmware datacenter`. The parent is a
// thin namespace command with one verb (`list`) so the future
// `datacenter <other-verb>` additions slot under the same parent
// without renaming.
func newDatacenterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "datacenter",
		Short:        "vSphere datacenter verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSimpleListCmd(simpleListSpec{
		use:   "list",
		short: "List vSphere datacenters on a vCenter target",
		long: "list dispatches GET:/vcenter/datacenter against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Renders moid / name for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		example:  "  meho vmware datacenter list --target rdc-vcenter\n  meho vmware datacenter list --target rdc-vcenter --json",
		opID:     "GET:/vcenter/datacenter",
		moidKey:  "datacenter",
		zeroText: "  (0 datacenters)",
	}))
	return cmd
}

// newDatastoreCmd returns `meho vmware datastore`. See
// newDatacenterCmd's comment for the wrapping rationale.
func newDatastoreCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "datastore",
		Short:        "vSphere datastore verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSimpleListCmd(simpleListSpec{
		use:   "list",
		short: "List vSphere datastores on a vCenter target",
		long: "list dispatches GET:/vcenter/datastore against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Renders moid / name for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		example:  "  meho vmware datastore list --target rdc-vcenter\n  meho vmware datastore list --target rdc-vcenter --json",
		opID:     "GET:/vcenter/datastore",
		moidKey:  "datastore",
		zeroText: "  (0 datastores)",
	}))
	return cmd
}

// newNetworkCmd returns `meho vmware network`. See
// newDatacenterCmd's comment for the wrapping rationale.
func newNetworkCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "network",
		Short:        "vSphere network verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSimpleListCmd(simpleListSpec{
		use:   "list",
		short: "List vSphere networks on a vCenter target",
		long: "list dispatches GET:/vcenter/network against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Renders moid / name for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		example:  "  meho vmware network list --target rdc-vcenter\n  meho vmware network list --target rdc-vcenter --json",
		opID:     "GET:/vcenter/network",
		moidKey:  "network",
		zeroText: "  (0 networks)",
	}))
	return cmd
}

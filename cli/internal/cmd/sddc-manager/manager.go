// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newManagerCmd returns the `meho sddc-manager manager` sub-tree.
func newManagerCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "manager",
		Short:        "SDDC Manager appliance operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newManagerListCmd())
	return cmd
}

func newManagerListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List SDDC Manager appliances (FQDN, IP, version, management domain)",
		Long: "list dispatches sddc.manager.list against connector_id=\"sddc-rest-9.0\".\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager manager list --target rdc-sddc-manager\n" +
			"  meho sddc-manager manager list --target rdc-sddc-manager --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runManagerList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runManagerList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "sddc.manager.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "sddc.manager.list", r, jsonOut, printManagerList)
}

func printManagerList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		conn.PrintGeneric(w, "sddc.manager.list", r)
		return
	}
	fmt.Fprintf(w, "sddc-manager appliances (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 appliances)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-45s  %-20s  %s\n", "id", "fqdn", "version", "management_domain")
	for _, e := range entries {
		id := truncate(sddcStringField(e, "id"), 36)
		fqdn := truncate(sddcStringField(e, "fqdn"), 45)
		ver := truncate(sddcStringField(e, "version"), 20)
		domain := ""
		if d := sddcNestedField(e, "domain"); d != nil {
			domain = sddcStringField(d, "name")
		}
		fmt.Fprintf(w, "%-36s  %-45s  %-20s  %s\n", id, fqdn, ver, domain)
	}
}

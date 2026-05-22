// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"
)

// Provider-plane verb: `meho vcf-automation user list` -- system-scope
// users on the VCFA appliance.
func newUserCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "user",
		Short:        "Provider-plane VCFA system users (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newUserListCmd())
	return cmd
}

func newUserListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List provider-plane system users on a VCFA appliance",
		Example:       "  meho vcf-automation user list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProviderListVerb(cmd,
				"GET:/cloudapi/1.0.0/users",
				targetName, jsonOut, backplaneOverride,
				printUserList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printUserList(w io.Writer, r *CallResult) {
	const opID = "GET:/cloudapi/1.0.0/users"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeProviderListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 users)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-30s  %-10s\n", "id", "username", "fullName", "enabled")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-30s  %-10v\n",
			truncate(vcfaStringField(e, "id"), 36),
			truncate(vcfaStringField(e, "username"), 30),
			truncate(vcfaStringField(e, "fullName"), 30),
			e["isEnabled"],
		)
	}
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/version"
)

// newVersionCmd returns the `meho version` subcommand. v0.1-T1 prints
// only the CLI build information; the backplane-version line that
// Initiative #42 mandates lands in G2.6-T3 once the backplane URL
// config seam exists (no point printing "not configured" until then —
// the line is purely additive).
func newVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print CLI version and build metadata",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			info := version.Get()
			// Route the version line through cmd.OutOrStdout() with
			// fmt.Fprintf — NOT cmd.Print/Printf/Println. In cobra
			// v1.10.2 (command.go:1434-1446) the Print* helpers
			// delegate to OutOrStderr(), so the un-configured binary
			// writes its version banner to stderr. Install scripts
			// and smoke tests parse stdout, so the output contract
			// for `meho version` is "version line on stdout, nothing
			// on stderr". The unit test below pins both halves.
			fmt.Fprintf(
				cmd.OutOrStdout(),
				"meho %s (commit %s, built %s)\n",
				info.Version, info.Commit, info.Date,
			)
			return nil
		},
	}
}

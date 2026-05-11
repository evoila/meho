// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
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
			// cmd.Printf writes to the command's configured stdout
			// (cmd.OutOrStdout()); using it instead of fmt.Printf
			// keeps the subcommand testable — version_test.go swaps
			// the writer to a buffer to assert the rendered output.
			cmd.Printf(
				"meho %s (commit %s, built %s)\n",
				info.Version, info.Commit, info.Date,
			)
			return nil
		},
	}
}

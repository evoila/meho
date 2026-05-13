// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package targets implements the `meho targets` subcommand tree.
//
// Four verbs:
//
//   - meho targets list [--product P] [--json]
//   - meho targets describe <name|alias> [--json]
//   - meho targets probe <name|alias>
//   - meho targets import <file> [--update] [--dry-run] [--json]
package targets

import (
	"github.com/spf13/cobra"
)

// NewCommand returns the `meho targets` parent command.
func NewCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "targets",
		Short: "Operate the MEHO targets registry",
		Long: "List, describe, probe, and import targets registered in the " +
			"operator's tenant.\n\n" +
			"Use 'meho targets import <file>' to bulk-import from a targets.yaml " +
			"file. Other write operations (create, update, delete) are available " +
			"via the REST API at /api/v1/targets.",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newDescribeCmd())
	cmd.AddCommand(newProbeCmd())
	cmd.AddCommand(newImportCmd())
	return cmd
}

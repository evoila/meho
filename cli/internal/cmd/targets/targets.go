// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package targets implements the `meho targets` subcommand tree.
//
// Three verbs:
//
//   - meho targets list [--product P] [--json]
//   - meho targets describe <name|alias> [--json]
//   - meho targets probe <name|alias>
//
// All verbs are read-only (operator role). Write verbs (create / update /
// delete) are deferred to v0.2 per the T5 out-of-scope list.
package targets

import (
	"github.com/spf13/cobra"
)

// NewCommand returns the `meho targets` parent command.
func NewCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "targets",
		Short: "Operate the MEHO targets registry",
		Long: "List, describe, and probe targets registered in the operator's " +
			"tenant.\n\n" +
			"All verbs are read-only in v0.2; write operations (create, update, " +
			"delete) are available via the REST API at /api/v1/targets.",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newDescribeCmd())
	cmd.AddCommand(newProbeCmd())
	return cmd
}

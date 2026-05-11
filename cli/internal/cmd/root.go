// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package cmd assembles the cobra command tree for the meho CLI.
// The root command exposes global flags consumed by every subcommand
// (--config, -v/--verbose); subcommand-specific behaviour lives in
// sibling files (version.go and, in later tasks, login.go / status.go).
package cmd

import "github.com/spf13/cobra"

// Execute builds the command tree and runs it, returning any error
// produced by the executed subcommand. The caller is responsible for
// translating that error into a process exit code; cobra has already
// rendered the human-facing error message to stderr because the root
// command is configured with SilenceUsage = false and the default
// error-printing behaviour.
func Execute() error {
	return newRootCmd().Execute()
}

// newRootCmd constructs a fresh root command. A constructor (rather
// than a package-level var) keeps the command tree free of mutable
// global state, which matters for tests: every test gets its own
// independent tree and can swap stdout/stderr via SetOut / SetErr.
func newRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:   "meho",
		Short: "Operator CLI for the MEHO governance backplane",
		Long: "meho is the operator-facing CLI for the MEHO governance " +
			"backplane. v0.1 ships login, status, and version; further " +
			"operations are discovered from the backplane at runtime " +
			"(see Goal #11).",
		// SilenceUsage stops cobra from dumping the full usage block
		// when a RunE returns an error — operator-facing tooling
		// should surface a one-line failure, not a wall of help text.
		SilenceUsage: true,
		// SilenceErrors is left false so cobra still writes the error
		// message to stderr; main() only sets the exit code.
		SilenceErrors: false,
	}

	// Global flags. Bound to no destination yet — later tasks read
	// them via cmd.Flags().GetString / GetBool inside their RunE
	// functions, which keeps the root command free of subcommand
	// concerns and avoids global state.
	root.PersistentFlags().String(
		"config",
		"",
		"path to meho config file (default: $XDG_CONFIG_HOME/meho/config.yaml)",
	)
	root.PersistentFlags().BoolP(
		"verbose",
		"v",
		false,
		"enable verbose output",
	)

	root.AddCommand(newVersionCmd())
	return root
}

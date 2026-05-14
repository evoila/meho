// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package cmd assembles the cobra command tree for the meho CLI.
// The root command exposes global flags consumed by every subcommand
// (--config, -v/--verbose); subcommand-specific behaviour lives in
// sibling files (version.go, login.go, status.go).
package cmd

import (
	"context"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/cmd/operation"
	"github.com/evoila/meho/cli/internal/cmd/retrieval"
	"github.com/evoila/meho/cli/internal/discovery"
)

// Execute builds the command tree and runs it, returning any error
// produced by the executed subcommand. The caller is responsible for
// translating that error into a process exit code; cobra has already
// rendered the human-facing error message to stderr because the root
// command is configured with SilenceUsage = true (suppress the usage
// wall on RunE errors).
//
// The returned error may satisfy output.ExitCoder; main inspects it
// to pick the process exit code (auth_expired → 2, unreachable → 3,
// unexpected → 4, generic → 1).
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
			"backplane. v0.1 ships login (G2.6-T2), version (G2.6-T1), and " +
			"status (G2.6-T3). Further operations are discovered from the " +
			"backplane at runtime — adding an operation to the backplane " +
			"doesn't require a new CLI binary release (see Goal #11 §5).",
		// SilenceUsage stops cobra from dumping the full usage block
		// when a RunE returns an error — operator-facing tooling
		// should surface a one-line failure, not a wall of help text.
		SilenceUsage: true,
		// SilenceErrors is left false so cobra still writes the error
		// message to stderr for non-status subcommands; status sets
		// SilenceErrors = true on itself to take over both the JSON
		// and human error rendering paths.
		SilenceErrors: false,
	}

	// Global flags. Bound to no destination yet — later tasks read
	// them via cmd.Flags().GetString / GetBool inside their RunE
	// functions, which keeps the root command free of subcommand
	// concerns and avoids global state.
	root.PersistentFlags().String(
		"config",
		"",
		"path to meho config file (default: $XDG_CONFIG_HOME/meho/config.json)",
	)
	root.PersistentFlags().BoolP(
		"verbose",
		"v",
		false,
		"enable verbose output",
	)

	root.AddCommand(newVersionCmd())
	root.AddCommand(newLoginCmd())
	root.AddCommand(newStatusCmd())

	// G4.3-T2 (#441) -- retrieval-quality + migration-decision tooling.
	// `meho retrieval eval` ships first; sibling verbs (usage T5b #464,
	// retire-checklist T6 #445) graft onto the same parent in their own
	// PRs.
	root.AddCommand(retrieval.NewRootCmd())

	// G0.6-T13 (#481) -- operation meta-tool surface for the G0.6
	// dispatcher substrate. `meho operation groups/search/call` wrap
	// the three /api/v1/operations/* routes shipped by G0.6-T8 (#399).
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in verb names.
	root.AddCommand(operation.NewRootCmd())

	// Server-driven subcommand discovery (Goal #11 §5). Fetched
	// best-effort on startup so the operator's `meho --help` lists
	// the full set of operations the backplane advertises. v0.1
	// backplanes return an empty manifest — the scaffold runs but
	// produces no extra commands. v0.2+ operations land here
	// without a CLI binary release.
	//
	// The fetch is silent on every failure path: a missing endpoint
	// (404 before G2.2 ships it), an offline operator, or a
	// misconfigured backplane all degrade to "no extra commands"
	// rather than blocking the entire CLI. The configured
	// backplane URL comes from the same config.json `meho login`
	// writes — operators with no login persist no URL and skip the
	// discovery fetch entirely.
	registerDynamicSubcommands(root)

	return root
}

// registerDynamicSubcommands runs the discovery fetch and grafts
// any returned commands onto rootCmd. Splits out as a named
// function for two reasons:
//
//  1. Tests can swap the function via setDynamicRegistrar (below)
//     to control startup-time behaviour without touching network.
//  2. Errors during dynamic registration (a collision with a
//     built-in subcommand name) print a warning to stderr but
//     never abort startup — the operator still gets to run the
//     local subcommands.
//
// The fetch budget is bounded by discovery.fetchTimeout so a hung
// backplane TCP connection can't block a `meho version` invocation.
func registerDynamicSubcommands(root *cobra.Command) {
	if dynamicRegistrar != nil {
		dynamicRegistrar(root)
		return
	}
	cfg, err := auth.LoadConfig()
	if err != nil || cfg.BackplaneURL == "" {
		// No login yet, or the operator removed the config file —
		// nothing to discover against. Silent: the local-only
		// command set is fully usable.
		return
	}

	// background ctx is fine here: the fetch's own context has the
	// discovery.fetchTimeout cap applied internally. Using
	// cobra.Command.Context() would be nicer but cobra hasn't
	// constructed it yet at command-tree-build time.
	manifest, err := discovery.Fetch(context.Background(), http.DefaultClient, cfg.BackplaneURL)
	if err != nil {
		// Decoding failures (the only error class Fetch returns)
		// are surfaced as a stderr warning but never abort.
		root.PrintErrf("warning: dynamic subcommand discovery failed: %v\n", err)
		return
	}
	if err := discovery.Register(root, manifest); err != nil {
		root.PrintErrf("warning: dynamic subcommand registration: %v\n", err)
	}
}

// dynamicRegistrar overrides registerDynamicSubcommands in tests so
// unit tests can deterministically register synthetic manifests
// without standing up a real backplane HTTP server. nil in
// production.
var dynamicRegistrar func(*cobra.Command)

// setDynamicRegistrar is the test-only seam onto dynamicRegistrar.
// Returns a cleanup function so tests can restore the production
// (nil) value on teardown.
//
// Exposed at package scope (lowercase) so root_test.go can use it
// without exporting the underlying var. Callers must use the
// returned cleanup to avoid contaminating sibling tests.
func setDynamicRegistrar(fn func(*cobra.Command)) func() {
	prev := dynamicRegistrar
	dynamicRegistrar = fn
	return func() { dynamicRegistrar = prev }
}

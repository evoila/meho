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
	"github.com/evoila/meho/cli/internal/cmd/audit"
	"github.com/evoila/meho/cli/internal/cmd/connector"
	"github.com/evoila/meho/cli/internal/cmd/kb"
	"github.com/evoila/meho/cli/internal/cmd/operation"
	"github.com/evoila/meho/cli/internal/cmd/retrieval"
	"github.com/evoila/meho/cli/internal/cmd/targets"
	"github.com/evoila/meho/cli/internal/cmd/vault"
	"github.com/evoila/meho/cli/internal/cmd/vmware"
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

	// G0.7-T5 (#405) -- spec-ingestion + review workflow surface for
	// the G0.7 pipeline (Initiative #389). `meho connector
	// ingest/list/review/edit-group/edit-op/enable/disable` wrap the
	// seven /api/v1/connectors* routes shipped by G0.7-T6 (#406).
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in verb names.
	root.AddCommand(connector.NewRootCmd())

	// G0.3-T5 (#256) + G0.3-T6 (#257) -- targets registry verbs
	// (list / describe / probe / import) for Initiative #224. Wraps
	// the read + probe + create routes of /api/v1/targets/*.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `targets` parent.
	root.AddCommand(targets.NewRootCmd())

	// G8.1-T3 (#467) -- audit-query verbs (query / recent / show /
	// who-touched / my-recent) for Initiative #334. Wraps the four
	// /api/v1/audit/* routes shipped by G8.1-T2 (#466). Registered
	// before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `audit` parent.
	root.AddCommand(audit.NewRootCmd())

	// G4.1-T4 (#418) -- kb verbs (ingest / search / list / show /
	// add / delete) for Initiative #331. Wraps the five /api/v1/kb*
	// routes shipped by G4.1-T2 (#416) plus the /api/v1/retrieve
	// route for the search verb. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `kb` parent.
	root.AddCommand(kb.NewRootCmd())

	// G3.1-T7 (#511) -- vmware-rest-9.0 operator alias verbs for
	// Initiative #227. The verb tree pre-bakes connector_id=
	// "vmware-rest-9.0" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on
	// every invocation. PR-1 ships raw-REST verbs (about, vm list/info,
	// host list, cluster list, datacenter/datastore/network list,
	// operation search/call); composite-backed verbs (vm create, host
	// evacuate, cluster patch) become end-to-end dispatchable once
	// G3.1-T5 (#508) + G3.1-T6 (#509) register the underlying
	// composite ops. Registered before registerDynamicSubcommands so
	// the backplane manifest cannot shadow the built-in `vmware`
	// parent.
	root.AddCommand(vmware.NewRootCmd())

	// G3.3-T6 (#550) -- vault-1.x operator alias verbs for Initiative
	// #366. The verb tree pre-bakes connector_id="vault-1.x" on top of
	// the existing /api/v1/operations/call dispatcher route so operators
	// don't type the connector ID on every invocation. Ships the KV-v2
	// (read/list/put/versions/delete), sys (health/seal-status/mounts-
	// list/auth-list), and auth (userpass/approle list+read) verbs over
	// the typed ops registered by G3.3-T1/T2/T3 (#545/#546/#547).
	// `meho vault kv read --target rdc-vault secret <path>` replaces the
	// consumer's `_secret-read.sh` wrapper. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot shadow
	// the built-in `vault` parent.
	root.AddCommand(vault.NewRootCmd())

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

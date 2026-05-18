// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package migrate hosts the cobra commands under `meho migrate ...` for
// G5.3-T1 (#608) of Initiative #375. v0.1 ships the command-tree skeleton
// with the `memory` subcommand — all flags registered, flow logic deferred
// to T2–T5. The huh interactive-form library is declared as a dependency in
// this task and its form surface will be used by T2–T5.
//
// The package deliberately does not import cli/internal/cmd helpers (the
// in-package HTTP helper trio used by kb/, audit/, connector/, etc.) because
// the migrate tree is laptop-local — it reads and writes local memory files
// and calls the backplane submit route, not the streaming read routes. The
// import-cycle convention documented in kb/kb.go header still applies: any
// shared helper must live in an internal/migrate* package, not in a shared
// cmd/* package.
package migrate

import (
	// huh is imported here to establish charm.land/huh/v2 as a direct
	// dependency in go.mod (supply-chain rationale: §devops/supply-chain).
	// T2–T5 (#609–#612) use the form surface in the memory-migration flow;
	// T1 declares the dependency so go.sum captures the full transitive
	// closure at a single known commit boundary.
	_ "charm.land/huh/v2"

	"github.com/spf13/cobra"
)

// NewRootCmd returns the `meho migrate` parent command. The command is
// grafted onto the top-level meho command tree by cmd/root.go alongside
// the other built-in verb trees, before registerDynamicSubcommands so the
// backplane manifest cannot shadow the built-in `migrate` verb.
//
// G5.3-T1 (#608) ships the skeleton: the parent command plus the `memory`
// subcommand with flags parsed and a stub RunE. Flow logic (scanner,
// interactive picker, dry-run preview, submission, mark-migrated) lands
// in T2–T5 of Initiative #375.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "migrate",
		Short: "Migrate laptop-local data to the MEHO backplane (G5.3)",
		Long: "Migrate laptop-local operator data to the backplane. " +
			"v0.1 ships the memory subcommand skeleton; flow logic " +
			"(scanner, picker, submission) lands in G5.3-T2–T5 " +
			"(Initiative #375).",
		SilenceUsage: true,
	}
	cmd.AddCommand(newMemoryCmd())
	return cmd
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"errors"

	"github.com/spf13/cobra"
)

// newMemoryCmd returns the `meho migrate memory` subcommand. The cobra
// surface is fully wired — all flags registered, short/long help text
// describing the future interactive migration flow. The RunE stub returns
// a "not yet implemented" error so the skeleton compiles and is discoverable
// via `meho migrate memory --help` without accidentally running a partial
// flow.
//
// Flow logic (local memory file scanner, interactive huh form picker, dry-run
// preview, backplane submit, mark-migrated) lands in G5.3-T2–T5 (#609–#612).
func newMemoryCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "memory",
		Short:        "Migrate laptop-local memory entries to the backplane",
		SilenceUsage: true,
		Long: "Scan a local memory directory (or the default XDG location), " +
			"present an interactive picker for entries to migrate, preview " +
			"changes in dry-run mode, and submit selected entries to the " +
			"backplane knowledge base. Full flow lands in G5.3-T2–T5 " +
			"(Initiative #375).",
		RunE: func(_ *cobra.Command, _ []string) error {
			return errors.New("meho migrate memory: not yet implemented (G5.3-T2–T5)")
		},
	}

	// --source: local memory directory to scan. Empty default defers to
	// the XDG-aware resolver added in T2 (#609).
	cmd.Flags().String(
		"source",
		"",
		"path to the local memory directory to scan (default: XDG-resolved in T2)",
	)

	// --dry-run: preview which entries would be migrated without sending
	// anything to the backplane.
	cmd.Flags().Bool(
		"dry-run",
		false,
		"preview entries that would be migrated without submitting",
	)

	// --non-interactive: skip the huh picker and migrate all discovered
	// entries that pass the filter criteria.
	cmd.Flags().Bool(
		"non-interactive",
		false,
		"skip the interactive picker and migrate all discovered entries",
	)

	// --include-machine-local: include entries tagged as machine-local
	// (hostname-scoped) which are excluded by default because they rarely
	// transfer meaningfully across machines.
	cmd.Flags().Bool(
		"include-machine-local",
		false,
		"include machine-local (hostname-scoped) entries in the migration set",
	)

	// --mark-migrated: after successful submission, write a sentinel
	// marker in the source directory so a re-run skips already-migrated
	// entries.
	cmd.Flags().Bool(
		"mark-migrated",
		false,
		"write a migration-complete marker after successful submission",
	)

	return cmd
}

// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package retrieval hosts the cobra commands under `meho retrieval ...`
// for the G4.3 retrieval-quality / migration-decision tooling
// (Initiative #373). v0.2 ships:
//
//   - `meho retrieval eval` — corpus-driven precision@5 / MRR /
//     coverage report against /api/v1/retrieve/eval (T2 #441).
//   - `meho retrieval usage` — audit-log-backed daily-use telemetry
//     (T5b #464; lands separately).
//   - `meho retrieval retire-checklist` — combined retire-decision
//     verb (T6 #445).
//
// Each subcommand calls one backplane endpoint and renders the
// response in either a human-readable table or `--json` mode.
// Authentication piggybacks on the token meho login wrote — the
// shared cmd.NewAuthedHTTPClient helper handles the bearer
// injection + 401 refresh dance.
package retrieval

import "github.com/spf13/cobra"

// NewRootCmd returns the `meho retrieval` parent command. The
// command is grafted onto the top-level meho command tree by
// cmd/root.go.
//
// The parent itself takes no args and prints its own help; every
// piece of behaviour lives in the per-subcommand RunE closures.
// Callers add new G4.3 verbs by appending to the AddCommand list
// inside this constructor.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "retrieval",
		Short:        "Retrieval-quality + migration-decision tooling (G4.3 #373)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newEvalCmd())
	cmd.AddCommand(newRetireChecklistCmd())
	return cmd
}

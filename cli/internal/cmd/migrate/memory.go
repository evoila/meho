// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"charm.land/huh/v2"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/migrate"
)

// SubmitFn is the seam tests replace with a mock; production code uses
// the real doSubmit wired in newMemoryCmd.
type SubmitFn func(plans []migrate.SubmitPlan) error

// dryRunEnvelope is the machine-readable shape emitted by --dry-run
// (one JSON object per line). Matches the T5 POST body exactly.
type dryRunEnvelope struct {
	Scope    string         `json:"scope"`
	Slug     string         `json:"slug"`
	Body     string         `json:"body"`
	Metadata map[string]any `json:"metadata"`
	SourceID string         `json:"source_id"`
}

func newMemoryCmd() *cobra.Command {
	// Real submit fn is wired inside RunE via newMemoryCmdWithSubmit(nil):
	// a nil submitFn causes RunE to call doSubmit (the real HTTP POST path).
	return newMemoryCmdWithSubmit(nil)
}

// newMemoryCmdWithSubmit is the testable variant: callers inject a submitFn.
func newMemoryCmdWithSubmit(submitFn SubmitFn) *cobra.Command {
	cmd := &cobra.Command{
		Use:          "memory",
		Short:        "Migrate laptop-local memory entries to the backplane",
		SilenceUsage: true,
		Long: "Scan a local memory directory (or the default XDG location), " +
			"present an interactive picker for entries to migrate, preview " +
			"changes in --dry-run mode, or run headlessly with " +
			"--non-interactive.\n\n" +
			"Set MEHO_ACCESSIBLE=1 for screen-reader-friendly output.",
		RunE: func(cmd *cobra.Command, _ []string) error {
			flags := cmd.Flags()
			source, _ := flags.GetString("source")
			dryRun, _ := flags.GetBool("dry-run")
			nonInteractive, _ := flags.GetBool("non-interactive")
			includeML, _ := flags.GetBool("include-machine-local")
			markMigrated, _ := flags.GetBool("mark-migrated")
			backplaneOverride, _ := flags.GetString("backplane")

			// Resolve the effective submit function: injected for tests,
			// real HTTP POST for production (nil submitFn).
			eff := submitFn
			if eff == nil {
				eff = func(plans []migrate.SubmitPlan) error {
					return doSubmit(cmd, backplaneOverride, plans)
				}
			}

			// ── Resolve source directory ──────────────────────────────
			dir, err := migrate.ResolveSourceDir(source)
			if err != nil {
				return fmt.Errorf("resolve source dir: %w", err)
			}

			// ── Scan ──────────────────────────────────────────────────
			files, err := migrate.ScanDir(dir)
			if err != nil {
				return fmt.Errorf("scan %s: %w", dir, err)
			}
			if len(files) == 0 {
				fmt.Fprintf(cmd.OutOrStdout(), "No memory files found in %s.\n", dir)
				return nil
			}

			opts := migrate.BuildFormOpts{
				IncludeMachineLocal: includeML,
			}

			// ── --dry-run path ────────────────────────────────────────
			if dryRun {
				return runDryRun(cmd, files, opts)
			}

			// ── --non-interactive path ────────────────────────────────
			if nonInteractive {
				plans, refused := nonInteractivePlans(files, opts)
				if len(refused) > 0 {
					for _, f := range refused {
						fmt.Fprintf(cmd.ErrOrStderr(),
							"meho migrate memory: skipping %s (type %q requires interactive review)\n",
							f.Path, f.Type,
						)
					}
				}
				if err := eff(plans); err != nil {
					return err
				}
				if len(refused) > 0 {
					return fmt.Errorf("meho migrate memory: %d entries require interactive review", len(refused))
				}
				if markMigrated {
					_ = migrate.TouchMarker(dir) // non-fatal in T4 stub
				}
				return nil
			}

			// ── Interactive path ──────────────────────────────────────
			form, plans := migrate.BuildForm(files, opts)
			accessible := os.Getenv("MEHO_ACCESSIBLE") == "1"
			form = form.WithAccessible(accessible)

			if err := form.Run(); err != nil {
				// Operator aborted (Ctrl+C / Esc, or said "No" on the
				// batch-confirm then aborted) — surface as clean exit.
				if errors.Is(err, huh.ErrUserAborted) {
					fmt.Fprintf(cmd.OutOrStdout(), "Migration cancelled.\n")
					return nil
				}
				return fmt.Errorf("picker: %w", err)
			}

			migrate.FinalizeSkip(plans)

			toSubmit := filterMigrate(plans)
			if len(toSubmit) == 0 {
				fmt.Fprintf(cmd.OutOrStdout(), "No entries selected for migration.\n")
				return nil
			}

			if err := eff(toSubmit); err != nil {
				return err
			}
			if markMigrated {
				_ = migrate.TouchMarker(dir)
			}
			return nil
		},
	}

	cmd.Flags().String("source", "", "path to the local memory directory to scan (default: XDG-resolved)")
	cmd.Flags().Bool("dry-run", false, "preview entries that would be migrated without submitting")
	cmd.Flags().Bool("non-interactive", false, "skip the interactive picker; migrates only user/feedback entries")
	cmd.Flags().Bool("include-machine-local", false, "include machine-local entries (default: skip them)")
	cmd.Flags().Bool("mark-migrated", false, "touch the migration-complete marker after successful submission")
	cmd.Flags().String("backplane", "", "backplane URL override (default: from meho login config)")

	return cmd
}

// runDryRun prints one JSON envelope per migrate-selected file (default
// plan, no form) and makes no network call.
func runDryRun(cmd *cobra.Command, files []migrate.MemoryFile, opts migrate.BuildFormOpts) error {
	enc := json.NewEncoder(cmd.OutOrStdout())
	enc.SetEscapeHTML(false)
	for _, f := range files {
		plan := migrate.DefaultPlan(f, opts)
		if plan.Skip {
			continue
		}
		md := plan.File.Metadata
		if md == nil {
			md = map[string]any{}
		}
		env := dryRunEnvelope{
			Scope:    plan.Scope,
			Slug:     plan.Slug,
			Body:     plan.Body,
			Metadata: md,
			SourceID: sourceID(plan),
		}
		if err := enc.Encode(env); err != nil {
			return err
		}
	}
	return nil
}

// nonInteractivePlans returns plans for user/feedback files (at their
// suggested scope) and the refused list (project/reference).
// Machine-local files are always skipped in non-interactive mode,
// regardless of IncludeMachineLocal (spec requirement: operator must
// review machine-local content interactively).
func nonInteractivePlans(files []migrate.MemoryFile, opts migrate.BuildFormOpts) (plans []migrate.SubmitPlan, refused []migrate.MemoryFile) {
	safeOpts := opts
	safeOpts.IncludeMachineLocal = false
	for _, f := range files {
		plan := migrate.DefaultPlan(f, safeOpts)
		if plan.Skip {
			continue
		}
		switch f.Type {
		case "user", "feedback":
			plans = append(plans, plan)
		default:
			refused = append(refused, f)
		}
	}
	return plans, refused
}

// filterMigrate returns only the non-skip plans.
func filterMigrate(plans []migrate.SubmitPlan) []migrate.SubmitPlan {
	var out []migrate.SubmitPlan
	for _, p := range plans {
		if !p.Skip {
			out = append(out, p)
		}
	}
	return out
}

// sourceID builds the stable source_id string from a plan's BodySHA256.
func sourceID(plan migrate.SubmitPlan) string {
	prefix := plan.File.BodySHA256
	if len(prefix) > migrate.SourceIDPrefix {
		prefix = prefix[:migrate.SourceIDPrefix]
	}
	return "laptop-migration/" + prefix
}


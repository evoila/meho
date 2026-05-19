// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"charm.land/huh/v2"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/migrate"
)

// SubmitFn is the seam T5 replaces with the real backplane POST.
// T4 wires a no-op stub so the command tree compiles and the dry-run
// path works end-to-end without a network call.
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

// SourceIDPrefix is the number of hex chars taken from the SHA-256 body
// hash to build a source_id. Shared constant between T4 dry-run output
// and T5 POST (keeping them bit-for-bit identical ensures --dry-run
// preview = exactly what would be sent).
const SourceIDPrefix = 12

func newMemoryCmd() *cobra.Command {
	return newMemoryCmdWithSubmit(stubSubmitFn)
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
				// TenantConfigured / IsTenantAdmin resolved from auth in T5;
				// T4 leaves these at their zero values (conservative defaults).
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
						fmt.Fprintf(os.Stderr,
							"meho migrate memory: skipping %s (type %q requires interactive review)\n",
							f.Path, f.Type,
						)
					}
				}
				if err := submitFn(plans); err != nil {
					return err
				}
				if markMigrated {
					_ = migrate.TouchMarker(dir) // non-fatal in T4 stub
				}
				if len(refused) > 0 {
					return fmt.Errorf("meho migrate memory: %d entries require interactive review", len(refused))
				}
				return nil
			}

			// ── Interactive path ──────────────────────────────────────
			form, plans := migrate.BuildForm(files, opts)
			accessible := os.Getenv("MEHO_ACCESSIBLE") != ""
			form = form.WithAccessible(accessible)

			if err := form.Run(); err != nil {
				return fmt.Errorf("picker: %w", err)
			}

			migrate.FinalizeSkip(plans)

			toSubmit := filterMigrate(plans)
			if len(toSubmit) == 0 {
				fmt.Fprintf(cmd.OutOrStdout(), "No entries selected for migration.\n")
				return nil
			}

			if err := submitFn(toSubmit); err != nil {
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
		env := dryRunEnvelope{
			Scope:    plan.Scope,
			Slug:     plan.Slug,
			Body:     plan.Body,
			Metadata: map[string]any{"tags": []string{}},
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
// Uses the first SourceIDPrefix hex chars of the SHA-256 hash.
func sourceID(plan migrate.SubmitPlan) string {
	prefix := plan.File.BodySHA256
	if len(prefix) > SourceIDPrefix {
		prefix = prefix[:SourceIDPrefix]
	}
	return "laptop-migration/" + prefix
}

// stubSubmitFn is the T4 no-op seam. T5 replaces it with the real
// backplane POST implementation.
func stubSubmitFn(plans []migrate.SubmitPlan) error {
	_ = time.Now() // suppress unused import if huh brings in time
	if len(plans) == 0 {
		return nil
	}
	scopes := make([]string, 0, len(plans))
	for _, p := range plans {
		scopes = append(scopes, fmt.Sprintf("%s (%s)", p.Slug, p.Scope))
	}
	return fmt.Errorf("meho migrate memory: submit not yet implemented (T5); would send: %s",
		strings.Join(scopes, ", "))
}

// withHuhForm is used in tests to replace the form runner.
var withHuhForm = func(form *huh.Form) error { return form.Run() }

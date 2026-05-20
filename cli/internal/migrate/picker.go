// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"charm.land/huh/v2"
)

// Action tokens for the per-file action select field.
const (
	// ActionMigrateSuggested uses the scope SuggestScope returned.
	ActionMigrateSuggested = "migrate:suggested"
	// ActionMigrateDifferent shows the scope picker group.
	ActionMigrateDifferent = "migrate:different"
	// ActionMigrateEdit shows the body-edit group before submit.
	ActionMigrateEdit = "migrate:edit"
	// ActionSkipManual keeps the file laptop-local by operator choice.
	ActionSkipManual = "skip:manual"
	// ActionSkipMachineLocal keeps a machine-local flagged file local.
	ActionSkipMachineLocal = "skip:machine-local"
)

// SubmitPlan is one entry in the operator's migration decision. T5
// consumes a []SubmitPlan from the interactive, --dry-run, or
// --non-interactive paths.
type SubmitPlan struct {
	// File is the source MemoryFile.
	File MemoryFile
	// Scope is the chosen scope token (one of the Scope* constants).
	Scope string
	// Slug is the entry identifier validated against ^[a-z0-9][a-z0-9-]*$.
	Slug string
	// Body is the body text to POST (may differ from File.Body if the
	// operator edited it via the body-edit group).
	Body string
	// Skip is true when the operator chose to keep the file laptop-local.
	Skip bool
	// Action records the raw action selection so callers can distinguish
	// between the different migrate/skip variants. Set by BuildForm; also
	// set to ActionMigrateSuggested by DefaultPlan for non-interactive
	// and --dry-run paths.
	Action string
}

// BuildFormOpts controls scope availability and picker defaults.
type BuildFormOpts struct {
	// IsTenantAdmin allows the tenant and target scope options. When false
	// those options are omitted from the scope picker (the server would
	// reject them with 403 anyway, but hiding them prevents confusion).
	IsTenantAdmin bool
	// TenantConfigured is true when the operator's meho config has a
	// default tenant slug. Used by SuggestScope for the project type.
	TenantConfigured bool
	// TargetConfigured is true when the operator's config has a default
	// target. Used to enable the user×target scope option.
	TargetConfigured bool
	// IncludeMachineLocal overrides the default-to-skip behaviour for
	// files flagged by DetectMachineLocal or MachineLocalOptOut. The
	// machine-local badge is still shown; only the default action flips.
	IncludeMachineLocal bool
}

// BuildForm creates an interactive huh form over files. The returned
// plans slice is populated as the operator advances through the form —
// callers must not read plans until form.Run() returns nil.
//
// After form.Run(), call FinalizeSkip(plans) to propagate each plan's
// Action field into its Skip bool.
//
// The form is designed to be run interactively (terminal); for
// --dry-run and --non-interactive paths use DefaultPlan / NonInteractivePlans.
func BuildForm(files []MemoryFile, opts BuildFormOpts) (*huh.Form, []SubmitPlan) {
	plans := make([]SubmitPlan, len(files))

	var groups []*huh.Group

	for i, f := range files {
		fi := i
		ff := f
		ml := DetectMachineLocal(ff.Body, nil)
		isMachineLocal := ml.Flagged || ff.MachineLocalOptOut

		// Initialise the plan defaults.
		plans[fi] = SubmitPlan{
			File:   ff,
			Scope:  SuggestScope(ff, opts.TenantConfigured),
			Slug:   slugFromPath(ff.Path),
			Body:   ff.Body,
			Action: defaultAction(isMachineLocal, opts.IncludeMachineLocal),
		}

		// ── Group 1: note header + action select ──────────────────────
		noteDesc := buildNoteDescription(ff, isMachineLocal)
		mainGroup := huh.NewGroup(
			huh.NewNote().
				Title(filepath.Base(ff.Path)).
				Description(noteDesc),
			huh.NewSelect[string]().
				Title("Action").
				Options(buildActionOptions(plans[fi].Scope, isMachineLocal)...).
				Value(&plans[fi].Action),
		)
		groups = append(groups, mainGroup)

		// ── Group 2: scope picker (hidden unless "migrate:different") ──
		scopeGroup := huh.NewGroup(
			huh.NewSelect[string]().
				Title("Scope").
				OptionsFunc(func() []huh.Option[string] {
					return buildScopeOptions(opts)
				}, nil).
				Value(&plans[fi].Scope),
		).WithHideFunc(func() bool {
			return plans[fi].Action != ActionMigrateDifferent
		})
		groups = append(groups, scopeGroup)

		// ── Group 3: slug input (hidden when skipping) ─────────────────
		slugGroup := huh.NewGroup(
			huh.NewInput().
				Title("Slug").
				Description("Identifier stored in the backplane (^[a-z0-9][a-z0-9-]*$)").
				Value(&plans[fi].Slug).
				Validate(validateSlug),
		).WithHideFunc(func() bool {
			return strings.HasPrefix(plans[fi].Action, "skip:")
		})
		groups = append(groups, slugGroup)

		// ── Group 4: body edit (hidden unless "migrate:edit") ──────────
		editGroup := huh.NewGroup(
			huh.NewText().
				Title("Edit body before migrating").
				Description("Strip machine-local snippets before sending.").
				Value(&plans[fi].Body),
		).WithHideFunc(func() bool {
			return plans[fi].Action != ActionMigrateEdit
		})
		groups = append(groups, editGroup)
	}

	// ── Final confirm group ────────────────────────────────────────────
	var confirmed bool
	confirmGroup := huh.NewGroup(
		huh.NewConfirm().
			TitleFunc(func() string {
				mc := countMigrateFromPlans(plans)
				return fmt.Sprintf("Migrate %d entries (%d skipped). Proceed?", mc, len(plans)-mc)
			}, nil).
			Value(&confirmed),
	)
	groups = append(groups, confirmGroup)

	return huh.NewForm(groups...), plans
}

// FinalizeSkip propagates each plan's Action field into its Skip bool.
// Call after form.Run() returns nil.
func FinalizeSkip(plans []SubmitPlan) {
	for i := range plans {
		plans[i].Skip = strings.HasPrefix(plans[i].Action, "skip:")
	}
}

// DefaultPlan returns the default SubmitPlan for a file without running
// the interactive form. Used by --dry-run (all files) and
// --non-interactive (user/feedback files only).
func DefaultPlan(f MemoryFile, opts BuildFormOpts) SubmitPlan {
	ml := DetectMachineLocal(f.Body, nil)
	isMachineLocal := ml.Flagged || f.MachineLocalOptOut
	action := defaultAction(isMachineLocal, opts.IncludeMachineLocal)
	return SubmitPlan{
		File:   f,
		Scope:  SuggestScope(f, opts.TenantConfigured),
		Slug:   slugFromPath(f.Path),
		Body:   f.Body,
		Skip:   strings.HasPrefix(action, "skip:"),
		Action: action,
	}
}

// slugFromPath derives a default slug from the file's base name:
//   - strip the .md extension
//   - lowercase
//   - replace any run of characters not in [a-z0-9] with a single "-"
//   - trim leading/trailing "-"
//   - if the result is empty or starts with a digit, prepend "entry-"
func slugFromPath(path string) string {
	base := filepath.Base(path)
	base = strings.TrimSuffix(base, filepath.Ext(base))
	base = strings.ToLower(base)
	re := regexp.MustCompile(`[^a-z0-9]+`)
	slug := strings.Trim(re.ReplaceAllString(base, "-"), "-")
	if slug == "" || (slug[0] >= '0' && slug[0] <= '9') {
		slug = "entry-" + slug
	}
	return slug
}

var slugRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)

// validateSlug rejects slugs that don't match ^[a-z0-9][a-z0-9-]*$.
func validateSlug(s string) error {
	if !slugRe.MatchString(s) {
		return fmt.Errorf("slug must match ^[a-z0-9][a-z0-9-]*$ (got %q)", s)
	}
	return nil
}

// defaultAction returns the initial action for a file.
func defaultAction(isMachineLocal, includeML bool) string {
	if isMachineLocal && !includeML {
		return ActionSkipMachineLocal
	}
	return ActionMigrateSuggested
}

// buildNoteDescription returns the header description for a file.
// Body is truncated to 200 chars with newlines stripped.
func buildNoteDescription(f MemoryFile, isMachineLocal bool) string {
	body := strings.ReplaceAll(f.Body, "\n", " ")
	const maxLen = 200
	if len(body) > maxLen {
		body = body[:maxLen] + "…"
	}
	badge := ""
	if isMachineLocal {
		badge = " [machine-local]"
	}
	if f.ParseWarning != "" {
		return fmt.Sprintf("⚠ %s%s\n%s", f.ParseWarning, badge, body)
	}
	return fmt.Sprintf("%s%s", body, badge)
}

// buildActionOptions returns the action select options for one file.
func buildActionOptions(suggestedScope string, isMachineLocal bool) []huh.Option[string] {
	migrateLabel := fmt.Sprintf("Migrate to %s", suggestedScope)
	opts := []huh.Option[string]{
		huh.NewOption(migrateLabel, ActionMigrateSuggested),
		huh.NewOption("Migrate to a different scope…", ActionMigrateDifferent),
		huh.NewOption("Migrate (edit body first)", ActionMigrateEdit),
		huh.NewOption("Skip (keep laptop-local)", ActionSkipManual),
	}
	if isMachineLocal {
		opts = append(opts, huh.NewOption("Skip — machine-local", ActionSkipMachineLocal))
	}
	return opts
}

// buildScopeOptions returns the scope select options filtered by role.
func buildScopeOptions(opts BuildFormOpts) []huh.Option[string] {
	options := []huh.Option[string]{
		huh.NewOption("user", ScopeUser),
	}
	if opts.TenantConfigured {
		options = append(options, huh.NewOption("user×tenant", ScopeUserTenant))
	}
	if opts.TargetConfigured {
		options = append(options, huh.NewOption("user×target", ScopeUserTarget))
	}
	if opts.IsTenantAdmin {
		options = append(options, huh.NewOption("tenant", ScopeTenant))
		options = append(options, huh.NewOption("target", ScopeTarget))
	}
	return options
}

func countMigrateFromPlans(plans []SubmitPlan) int {
	n := 0
	for _, p := range plans {
		if !strings.HasPrefix(p.Action, "skip:") {
			n++
		}
	}
	return n
}

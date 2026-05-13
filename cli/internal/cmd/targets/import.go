// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// knownYAMLFields are extracted into dedicated importEntry fields;
// everything else is spilled into importEntry.Extras.
var knownYAMLFields = map[string]bool{
	"name":         true,
	"aliases":      true,
	"product":      true,
	"host":         true,
	"port":         true,
	"secret_ref":   true,
	"vpn_required": true,
	"notes":        true,
	"auth_model":   true,
}

// importEntry is a single target parsed from a targets.yaml file.
type importEntry struct {
	Name        string
	Aliases     []string
	Product     string
	Host        string
	Port        *int
	SecretRef   *string
	VPNRequired bool
	Notes       *string
	AuthModel   string
	Extras      map[string]any
}

// importAction describes what the import will do with a single entry.
type importAction struct {
	Action string // "CREATE" or "UPDATE"
	Entry  importEntry
}

// importPlan holds the full set of actions resolved before any writes.
type importPlan struct {
	Actions   []importAction
	Conflicts []string // existing names when --update is not set
}

func newImportCmd() *cobra.Command {
	var (
		update    bool
		dryRun    bool
		asJSON    bool
		backplane string
	)

	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Bulk-import targets from a targets.yaml file",
		Long: `Read a targets.yaml file and import each entry into the backplane.

Default: if any target in the file already exists the entire import is
aborted — no partial write. Use --update to PATCH existing targets
instead of aborting.

Use --dry-run to preview the plan without making write API calls.
Combine with --json for machine-parseable output.

Tenant scoping: targets are imported into the tenant carried by your
JWT. To target a different tenant, run "meho login <url>" with the
appropriate credentials first.`,
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			data, err := os.ReadFile(args[0])
			if err != nil {
				return fmt.Errorf("read %s: %w", args[0], err)
			}
			entries, err := parseTargetsYAML(data)
			if err != nil {
				return err
			}
			if len(entries) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "No targets found in file.")
				return nil
			}

			bpURL, err := resolveURL(backplane)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(err.Error()), asJSON)
			}
			client, err := api.NewAuthedClient(cmd.Context(), bpURL, api.AuthedClientOptions{})
			if err != nil {
				if api.IsTokenNotFound(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`", bpURL, bpURL)),
						asJSON)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("build client: %v", err)),
					asJSON)
			}

			plan, err := buildImportPlan(cmd.Context(), client, entries, update)
			if err != nil {
				if api.IsNoRefreshToken(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("token expired; run `meho login %s`", bpURL)),
						asJSON)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unreachable(fmt.Sprintf("list existing targets: %v", err)),
					asJSON)
			}

			if dryRun {
				return printImportPlan(cmd, plan, asJSON)
			}

			if !update && len(plan.Conflicts) > 0 {
				fmt.Fprintf(cmd.ErrOrStderr(),
					"Import aborted: %d target(s) already exist. Re-run with --update to patch them:\n",
					len(plan.Conflicts))
				for _, name := range plan.Conflicts {
					fmt.Fprintf(cmd.ErrOrStderr(), "  %s\n", name)
				}
				return errors.New("import conflict — use --update")
			}

			return executeImportPlan(cmd.Context(), cmd, client, plan)
		},
	}
	cmd.Flags().BoolVar(&update, "update", false, "PATCH existing targets instead of aborting on conflict")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "print plan without making write API calls")
	cmd.Flags().BoolVar(&asJSON, "json", false, "output plan as JSON (use with --dry-run)")
	cmd.Flags().StringVar(&backplane, "backplane", "", "backplane URL (overrides config)")
	return cmd
}

// parseTargetsYAML unmarshals a targets.yaml and returns importEntry slice.
// Unknown fields per entry are spilled into importEntry.Extras.
func parseTargetsYAML(data []byte) ([]importEntry, error) {
	var f struct {
		Targets []map[string]any `yaml:"targets"`
	}
	if err := yaml.Unmarshal(data, &f); err != nil {
		return nil, fmt.Errorf("parse YAML: %w", err)
	}
	entries := make([]importEntry, 0, len(f.Targets))
	for i, raw := range f.Targets {
		e, err := parseYAMLEntry(i, raw)
		if err != nil {
			return nil, err
		}
		entries = append(entries, e)
	}
	return entries, nil
}

func parseYAMLEntry(i int, raw map[string]any) (importEntry, error) {
	var e importEntry

	name, ok := raw["name"].(string)
	if !ok || name == "" {
		return e, fmt.Errorf("entry %d: missing required field 'name'", i)
	}
	e.Name = name

	product, ok := raw["product"].(string)
	if !ok || product == "" {
		return e, fmt.Errorf("entry %d (%s): missing required field 'product'", i, name)
	}
	e.Product = product

	host, ok := raw["host"].(string)
	if !ok || host == "" {
		return e, fmt.Errorf("entry %d (%s): missing required field 'host'", i, name)
	}
	e.Host = host

	e.Aliases = []string{}
	if raw["aliases"] != nil {
		if seq, ok := raw["aliases"].([]any); ok {
			for _, a := range seq {
				if s, ok := a.(string); ok {
					e.Aliases = append(e.Aliases, s)
				}
			}
		}
	}

	if raw["port"] != nil {
		if p, ok := raw["port"].(int); ok {
			e.Port = &p
		}
	}

	if v, ok := raw["secret_ref"].(string); ok {
		e.SecretRef = &v
	}

	if v, ok := raw["vpn_required"].(bool); ok {
		e.VPNRequired = v
	}

	if v, ok := raw["notes"].(string); ok {
		e.Notes = &v
	}

	if v, ok := raw["auth_model"].(string); ok {
		e.AuthModel = v
	} else {
		e.AuthModel = "shared_service_account"
	}

	e.Extras = make(map[string]any)
	for k, v := range raw {
		if !knownYAMLFields[k] {
			e.Extras[k] = v
		}
	}

	return e, nil
}

// buildImportPlan fetches existing targets and classifies each entry as
// CREATE or UPDATE.
func buildImportPlan(ctx context.Context, client *api.AuthedClient, entries []importEntry, update bool) (*importPlan, error) {
	existing, err := listAllTargets(ctx, client)
	if err != nil {
		return nil, err
	}

	existingNames := make(map[string]bool, len(existing))
	for _, t := range existing {
		existingNames[t.Name] = true
	}

	plan := &importPlan{}
	for _, e := range entries {
		if existingNames[e.Name] {
			plan.Actions = append(plan.Actions, importAction{Action: "UPDATE", Entry: e})
			if !update {
				plan.Conflicts = append(plan.Conflicts, e.Name)
			}
		} else {
			plan.Actions = append(plan.Actions, importAction{Action: "CREATE", Entry: e})
		}
	}
	return plan, nil
}

// listAllTargets pages through GET /api/v1/targets until exhausted.
func listAllTargets(ctx context.Context, client *api.AuthedClient) ([]api.TargetSummary, error) {
	limit := 500
	var all []api.TargetSummary
	var cursor *string
	for {
		page, _, err := client.ListTargets(ctx, &api.ListTargetsParams{
			Limit:  &limit,
			Cursor: cursor,
		})
		if err != nil {
			return nil, err
		}
		all = append(all, page...)
		if len(page) < limit {
			break
		}
		last := page[len(page)-1].Name
		cursor = &last
	}
	return all, nil
}

// printImportPlan renders the plan to stdout in human or JSON format.
func printImportPlan(cmd *cobra.Command, plan *importPlan, asJSON bool) error {
	creates := filterActions(plan.Actions, "CREATE")
	updates := filterActions(plan.Actions, "UPDATE")

	if asJSON {
		type jsonPlan struct {
			Create []string `json:"create"`
			Update []string `json:"update"`
			Skip   []string `json:"skip"`
		}
		out := jsonPlan{
			Create: entryNames(creates),
			Update: entryNames(updates),
			Skip:   []string{},
		}
		b, _ := json.Marshal(out)
		fmt.Fprintln(cmd.OutOrStdout(), string(b))
		return nil
	}

	fmt.Fprintf(cmd.OutOrStdout(), "Plan: %d target(s) in input\n\n", len(plan.Actions))
	for _, a := range plan.Actions {
		e := a.Entry
		fmt.Fprintf(cmd.OutOrStdout(), "  %-6s  %-30s (product=%-12s host=%s)\n",
			a.Action, e.Name, e.Product, e.Host)
	}
	if len(plan.Conflicts) > 0 {
		fmt.Fprintf(cmd.OutOrStdout(),
			"\nNote: %d target(s) already exist. Use --update to patch them.\n",
			len(plan.Conflicts))
	}
	fmt.Fprintln(cmd.OutOrStdout(), "\nRun without --dry-run to apply.")
	return nil
}

// executeImportPlan applies the plan: POST new, PATCH existing.
func executeImportPlan(ctx context.Context, cmd *cobra.Command, client *api.AuthedClient, plan *importPlan) error {
	var errMsgs []string
	created, updated := 0, 0

	for _, a := range plan.Actions {
		switch a.Action {
		case "CREATE":
			_, status, err := client.CreateTarget(ctx, entryToCreateRequest(a.Entry))
			if err != nil {
				if status == http.StatusConflict {
					errMsgs = append(errMsgs, fmt.Sprintf("create %s: already exists (use --update)", a.Entry.Name))
				} else {
					errMsgs = append(errMsgs, fmt.Sprintf("create %s: %v", a.Entry.Name, err))
				}
				continue
			}
			fmt.Fprintf(cmd.OutOrStdout(), "created  %s\n", a.Entry.Name)
			created++
		case "UPDATE":
			_, _, err := client.UpdateTarget(ctx, a.Entry.Name, entryToUpdateRequest(a.Entry))
			if err != nil {
				errMsgs = append(errMsgs, fmt.Sprintf("update %s: %v", a.Entry.Name, err))
				continue
			}
			fmt.Fprintf(cmd.OutOrStdout(), "updated  %s\n", a.Entry.Name)
			updated++
		}
	}

	fmt.Fprintf(cmd.OutOrStdout(), "\nDone: %d created, %d updated", created, updated)
	if len(errMsgs) > 0 {
		fmt.Fprintf(cmd.OutOrStdout(), ", %d failed\n", len(errMsgs))
		for _, e := range errMsgs {
			fmt.Fprintf(cmd.ErrOrStderr(), "  error: %s\n", e)
		}
		return fmt.Errorf("%d import error(s)", len(errMsgs))
	}
	fmt.Fprintln(cmd.OutOrStdout(), ".")
	return nil
}

func entryToCreateRequest(e importEntry) api.TargetCreateRequest {
	return api.TargetCreateRequest{
		Name:        e.Name,
		Aliases:     e.Aliases,
		Product:     e.Product,
		Host:        e.Host,
		Port:        e.Port,
		SecretRef:   e.SecretRef,
		AuthModel:   e.AuthModel,
		VPNRequired: e.VPNRequired,
		Extras:      e.Extras,
		Notes:       e.Notes,
	}
}

func entryToUpdateRequest(e importEntry) api.TargetUpdateRequest {
	return api.TargetUpdateRequest{
		Aliases:     e.Aliases,
		Host:        e.Host,
		Port:        e.Port,
		SecretRef:   e.SecretRef,
		AuthModel:   e.AuthModel,
		VPNRequired: e.VPNRequired,
		Extras:      e.Extras,
		Notes:       e.Notes,
	}
}

func filterActions(actions []importAction, kind string) []importAction {
	var out []importAction
	for _, a := range actions {
		if a.Action == kind {
			out = append(out, a)
		}
	}
	return out
}

func entryNames(actions []importAction) []string {
	names := make([]string, len(actions))
	for i, a := range actions {
		names[i] = a.Entry.Name
	}
	return names
}

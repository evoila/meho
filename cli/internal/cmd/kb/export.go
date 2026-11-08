// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// exportResponseBodyCap bounds the export answer. The export returns
// every confirmed, generic entry with its full text in one answer, so
// it gets more room than the 1 MiB of the other kb verbs.
const exportResponseBodyCap int64 = 64 << 20

// newExportCmd returns the `meho kb export` command.
//
//	meho kb export <directory> [--dry-run] [--json] [--backplane <url>]
//
// It calls POST /api/v1/kb/export and writes one <slug>.md file per
// page into <directory>, on this machine. It removes the file of each
// page the answer lists to remove, when the file is there. It never
// touches another file. --dry-run lists what would be added, changed
// and removed, and writes nothing.
func newExportCmd() *cobra.Command {
	var (
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "export <directory>",
		Short: "Write the confirmed, generic kb entries as Markdown pages into a local folder (operator)",
		Long: "export calls POST /api/v1/kb/export and writes the answer into <directory> " +
			"on this machine, for example a folder of the team wiki repo.\n\n" +
			"The answer holds the entries of your tenant with status: confirmed and " +
			"applies_to: product-and-version that were written in MEHO. Entries that " +
			"came in through an import are left out. Each entry becomes <slug>.md, " +
			"with the lesson header and the text. The file of an entry with status: " +
			"retired or replaced is removed. No other file in the folder is touched.\n\n" +
			"An entry that holds what looks like a secret is not exported; the reason " +
			"is listed under skipped.\n\n" +
			"--dry-run lists what would be added, changed and removed, and writes nothing.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runExport(cmd, exportOptions{
				Directory:         args[0],
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"list what would be added, changed and removed, and write nothing")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the plan as JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type exportOptions struct {
	Directory         string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

// exportPlan is what the export did, or would do in a dry run. Each
// list holds file names inside the directory.
type exportPlan struct {
	DryRun    bool               `json:"dry_run"`
	Directory string             `json:"directory"`
	Added     []string           `json:"added"`
	Changed   []string           `json:"changed"`
	Unchanged []string           `json:"unchanged"`
	Removed   []string           `json:"removed"`
	Skipped   []api.KbExportSkip `json:"skipped"`
}

func runExport(cmd *cobra.Command, opts exportOptions) error {
	if opts.Directory == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("export requires a non-empty <directory> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postExport(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 200 without an export result payload", backplaneURL)),
			opts.JSONOut)
	}
	plan, err := applyExport(opts.Directory, resp.JSON200, opts.DryRun)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), plan)
	}
	printExportPlan(cmd.OutOrStdout(), plan)
	return nil
}

func postExport(ctx context.Context, backplaneURL string) (*api.ExportKbPagesApiV1KbExportPostResponse, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		ResponseBodyLimit: exportResponseBodyCap,
	})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ExportKbPagesApiV1KbExportPostResponse, error) {
			return authed.ExportKbPagesApiV1KbExportPostWithResponse(ctx, &api.ExportKbPagesApiV1KbExportPostParams{})
		},
		func(r *api.ExportKbPagesApiV1KbExportPostResponse) int { return r.StatusCode() },
	)
}

// applyExport compares the answer with the files in dir, and writes
// and removes files unless dryRun. Every name is checked before any
// file is touched, so a bad name changes nothing.
func applyExport(dir string, result *api.KbExportResult, dryRun bool) (exportPlan, error) {
	plan := exportPlan{
		DryRun: dryRun, Directory: dir,
		Added: []string{}, Changed: []string{}, Unchanged: []string{}, Removed: []string{},
		Skipped: result.Skipped,
	}
	if plan.Skipped == nil {
		plan.Skipped = []api.KbExportSkip{}
	}
	names := make([]string, 0, len(result.Pages)+len(result.Remove))
	for _, page := range result.Pages {
		names = append(names, page.Path)
	}
	for _, removal := range result.Remove {
		names = append(names, removal.Path)
	}
	for _, name := range names {
		if err := checkPageName(dir, name); err != nil {
			return plan, err
		}
	}
	if !dryRun {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return plan, fmt.Errorf("create %s: %w", dir, err)
		}
	}
	for _, page := range result.Pages {
		target := filepath.Join(dir, page.Path)
		current, err := os.ReadFile(target)
		switch {
		case errors.Is(err, fs.ErrNotExist):
			plan.Added = append(plan.Added, page.Path)
		case err != nil:
			return plan, fmt.Errorf("read %s: %w", target, err)
		case bytes.Equal(current, []byte(page.Content)):
			plan.Unchanged = append(plan.Unchanged, page.Path)
			continue
		default:
			plan.Changed = append(plan.Changed, page.Path)
		}
		if !dryRun {
			if err := os.WriteFile(target, []byte(page.Content), 0o644); err != nil {
				return plan, fmt.Errorf("write %s: %w", target, err)
			}
		}
	}
	for _, removal := range result.Remove {
		target := filepath.Join(dir, removal.Path)
		if _, err := os.Lstat(target); errors.Is(err, fs.ErrNotExist) {
			continue
		} else if err != nil {
			return plan, fmt.Errorf("read %s: %w", target, err)
		}
		plan.Removed = append(plan.Removed, removal.Path)
		if !dryRun {
			if err := os.Remove(target); err != nil {
				return plan, fmt.Errorf("remove %s: %w", target, err)
			}
		}
	}
	return plan, nil
}

// checkPageName refuses a name that is not one plain .md file name, and
// a file that is a link or a folder, so an answer can never write or
// remove a file outside the directory.
func checkPageName(dir, name string) error {
	if name == "" || name != filepath.Base(name) || strings.ContainsAny(name, `/\`) ||
		strings.HasPrefix(name, ".") || !strings.HasSuffix(name, ".md") {
		return fmt.Errorf("the backplane sent the page name %q, which is not a plain .md file name; nothing was written", name)
	}
	info, err := os.Lstat(filepath.Join(dir, name))
	if err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("%s is a link or a folder, not a plain file; nothing was written", filepath.Join(dir, name))
	}
	return nil
}

func printExportPlan(w io.Writer, plan exportPlan) {
	if plan.DryRun {
		fmt.Fprintln(w, "dry-run: nothing was written")
	}
	for _, list := range []struct {
		verb  string
		names []string
	}{{"add", plan.Added}, {"change", plan.Changed}, {"remove", plan.Removed}} {
		for _, name := range list.names {
			fmt.Fprintf(w, "%-7s %s\n", list.verb, filepath.Join(plan.Directory, name))
		}
	}
	fmt.Fprintf(w, "%-11s %d\n", "added:", len(plan.Added))
	fmt.Fprintf(w, "%-11s %d\n", "changed:", len(plan.Changed))
	fmt.Fprintf(w, "%-11s %d\n", "unchanged:", len(plan.Unchanged))
	fmt.Fprintf(w, "%-11s %d\n", "removed:", len(plan.Removed))
	fmt.Fprintf(w, "%-11s %d\n", "skipped:", len(plan.Skipped))
	if len(plan.Skipped) > 0 {
		fmt.Fprintln(w, "skipped (not exported):")
		for _, s := range plan.Skipped {
			fmt.Fprintf(w, "  - %s: %s\n", s.Slug, s.Reason)
		}
	}
}

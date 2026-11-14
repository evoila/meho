// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

const yamlContentType = "application/yaml"

// fileOptions are the flags every policy file command shares.
type fileOptions struct {
	Scope             string
	JSONOut           bool
	BackplaneOverride string
}

func (o *fileOptions) bind(cmd *cobra.Command) {
	f := cmd.Flags()
	f.StringVar(&o.Scope, "scope", "tenant",
		"tenant (your tenant's policy) or install (the whole install; platform admins only)")
	f.BoolVar(&o.JSONOut, "json", false, "print the server's answer as JSON")
	f.StringVar(&o.BackplaneOverride, "backplane", "",
		"backplane URL to call (defaults to the URL recorded by the most recent `meho login`)")
}

func (o *fileOptions) check() error {
	if o.Scope != "tenant" && o.Scope != "install" {
		return fmt.Errorf("--scope must be tenant or install; got %q", o.Scope)
	}
	return nil
}

// addFileCommands adds validate, diff, import, export and history to parent.
func addFileCommands(parent *cobra.Command) {
	parent.AddCommand(newValidateCmd(), newDiffCmd(), newImportCmd(), newExportCmd(), newHistoryCmd())
}

func newValidateCmd() *cobra.Command {
	var opts fileOptions
	cmd := &cobra.Command{
		Use:   "validate FILE",
		Short: "Check a policy file and list every mistake",
		Long: "validate sends FILE to POST /api/v1/policy/validate. MEHO checks " +
			"the shape, the categories and risk levels, and the floors (rules " +
			"that no policy file can loosen). Each mistake is one sentence that " +
			"names its place in the file. Nothing is written.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFile(cmd, opts, args[0], "validate", false)
		},
	}
	opts.bind(cmd)
	return cmd
}

func newDiffCmd() *cobra.Command {
	var opts fileOptions
	cmd := &cobra.Command{
		Use:   "diff FILE",
		Short: "Show what importing a policy file would change (dry run)",
		Long: "diff sends FILE to POST /api/v1/policy/import?preview=true. It " +
			"shows, per section, what the import would add, change and remove, " +
			"and for each classification entry how many operations it matches " +
			"and changes. Nothing is written.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFile(cmd, opts, args[0], "import", true)
		},
	}
	opts.bind(cmd)
	return cmd
}

func newImportCmd() *cobra.Command {
	var opts fileOptions
	cmd := &cobra.Command{
		Use:   "import FILE",
		Short: "Ask to import a policy file; a second admin must approve it",
		Long: "import sends FILE to POST /api/v1/policy/import. The import " +
			"replaces the whole policy: an entry that is not in the file is " +
			"removed. It is a change of rights, so it waits until a second " +
			"admin approves it; you can never approve your own import. Nothing " +
			"changes before that. The same file again says \"No change.\".",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFile(cmd, opts, args[0], "import", false)
		},
	}
	opts.bind(cmd)
	return cmd
}

// planAnswer is the part of a validate or import answer the CLI prints.
type planAnswer struct {
	Ok                bool           `json:"ok"`
	Mistakes          []string       `json:"mistakes"`
	Warnings          []string       `json:"warnings"`
	Diff              map[string]any `json:"diff"`
	NoChange          bool           `json:"no_change"`
	Message           *string        `json:"message"`
	ApprovalRequestID *string        `json:"approval_request_id"`
}

func runFile(cmd *cobra.Command, opts fileOptions, path, route string, preview bool) error {
	if err := opts.check(); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	content, err := os.ReadFile(path) // #nosec G304 -- the operator names the file
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("read %s: %v", path, err)), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postFile(cmd.Context(), backplaneURL, route, opts.Scope, preview, content)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	ok := resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusAccepted
	if !ok && resp.StatusCode != http.StatusUnprocessableEntity {
		return renderHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	var answer planAnswer
	if err := json.Unmarshal(resp.Body, &answer); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode the answer: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), json.RawMessage(resp.Body)); err != nil {
			return err
		}
	} else {
		printPlan(cmd.OutOrStdout(), answer, route == "validate")
	}
	if len(answer.Mistakes) > 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("the file has %d mistake(s); nothing was changed",
				len(answer.Mistakes))), opts.JSONOut)
	}
	return nil
}

func postFile(
	ctx context.Context, backplaneURL, route, scope string, preview bool, content []byte,
) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		body := bytes.NewReader(content)
		if route == "validate" {
			s := api.ValidateApiV1PolicyValidatePostParamsScope(scope)
			return authed.ValidateApiV1PolicyValidatePostWithBody(ctx,
				&api.ValidateApiV1PolicyValidatePostParams{Scope: &s}, yamlContentType, body)
		}
		s := api.ImportPolicyApiV1PolicyImportPostParamsScope(scope)
		return authed.ImportPolicyApiV1PolicyImportPostWithBody(ctx,
			&api.ImportPolicyApiV1PolicyImportPostParams{Scope: &s, Preview: &preview},
			yamlContentType, body)
	})
}

func printPlan(w io.Writer, answer planAnswer, validateOnly bool) {
	for _, m := range answer.Mistakes {
		fmt.Fprintf(w, "mistake: %s\n", sanitize(m))
	}
	for _, m := range answer.Warnings {
		fmt.Fprintf(w, "warning: %s\n", sanitize(m))
	}
	if len(answer.Mistakes) > 0 {
		return
	}
	if validateOnly {
		fmt.Fprintln(w, "The file is valid.")
		return
	}
	if answer.Diff != nil && !answer.NoChange {
		printDiff(w, answer.Diff)
	}
	if answer.Message != nil {
		fmt.Fprintln(w, sanitize(*answer.Message))
	}
	if answer.ApprovalRequestID != nil {
		fmt.Fprintf(w, "Approval request: %s\n", sanitize(*answer.ApprovalRequestID))
	}
}

// printDiff prints each section's added, changed and removed items.
func printDiff(w io.Writer, diff map[string]any) {
	for _, section := range []string{"description", "taxonomy", "classifications"} {
		parts, _ := diff[section].(map[string]any)
		for _, kind := range []string{"added", "changed", "removed"} {
			items, _ := parts[kind].([]any)
			for _, item := range items {
				fmt.Fprintf(w, "%s %s: %s\n", diffMark(kind), section, sanitize(describeItem(item)))
			}
		}
	}
	if n, ok := diff["operations_changed"].(float64); ok {
		fmt.Fprintf(w, "Operations whose values change: %d\n", int(n))
	}
}

func diffMark(kind string) string {
	switch kind {
	case "added":
		return "+"
	case "removed":
		return "-"
	default:
		return "~"
	}
}

// describeItem renders one diff item on one line.
func describeItem(item any) string {
	m, ok := item.(map[string]any)
	if !ok {
		return compactJSON(item)
	}
	var parts []string
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%s", k, compactJSON(m[k])))
	}
	return strings.Join(parts, " ")
}

func compactJSON(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprint(v)
	}
	return string(b)
}

func newExportCmd() *cobra.Command {
	var opts fileOptions
	var outPath string
	cmd := &cobra.Command{
		Use:   "export",
		Short: "Write the stored policy as a policy file",
		Long: "export calls GET /api/v1/policy/export and writes the stored " +
			"policy as a policy: v1 file. Without --output the file goes to stdout.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runExport(cmd, opts, outPath)
		},
	}
	opts.bind(cmd)
	cmd.Flags().StringVarP(&outPath, "output", "o", "", "write the file here instead of stdout")
	return cmd
}

func runExport(cmd *cobra.Command, opts fileOptions, outPath string) error {
	if err := opts.check(); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	ctx := cmd.Context()
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	s := api.ExportApiV1PolicyExportGetParamsScope(opts.Scope)
	resp, err := doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.ExportApiV1PolicyExportGet(ctx, &api.ExportApiV1PolicyExportGetParams{Scope: &s})
	})
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	if outPath == "" {
		_, err = cmd.OutOrStdout().Write(resp.Body)
		return err
	}
	if err := os.WriteFile(outPath, resp.Body, 0o600); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("write %s: %v", outPath, err)), opts.JSONOut)
	}
	fmt.Fprintf(cmd.ErrOrStderr(), "wrote the policy to %s\n", outPath)
	return nil
}

func newHistoryCmd() *cobra.Command {
	var opts fileOptions
	var limit int
	cmd := &cobra.Command{
		Use:   "history",
		Short: "List the imported policy files, newest first",
		Long: "history calls GET /api/v1/policy/history. MEHO keeps the last 20 " +
			"imported files: who imported each one, who approved it, when, and " +
			"what it changed. --json prints the files and the diffs too.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runHistory(cmd, opts, limit)
		},
	}
	opts.bind(cmd)
	cmd.Flags().IntVar(&limit, "limit", 20, "how many entries to show (1 to 20)")
	return cmd
}

func runHistory(cmd *cobra.Command, opts fileOptions, limit int) error {
	if err := opts.check(); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if limit < 1 || limit > 20 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be 1 to 20; got %d", limit)), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	ctx := cmd.Context()
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	s := api.HistoryApiV1PolicyHistoryGetParamsScope(opts.Scope)
	resp, err := doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.HistoryApiV1PolicyHistoryGet(ctx,
			&api.HistoryApiV1PolicyHistoryGetParams{Scope: &s, Limit: &limit})
	})
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	var rows []api.PolicyRevisionView
	if err := json.Unmarshal(resp.Body, &rows); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode the history: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), rows)
	}
	w := cmd.OutOrStdout()
	if len(rows) == 0 {
		fmt.Fprintln(w, "No policy file was imported yet.")
		return nil
	}
	for _, row := range rows {
		fmt.Fprintf(w, "%s  imported by %s  approved by %s  sha256 %s\n",
			row.Ts.UTC().Format("2006-01-02T15:04:05Z"), sanitize(row.ImportedBySub),
			sanitize(row.ApprovedBySub), shortSHA(row.FileSha256))
	}
	return nil
}

func shortSHA(s string) string {
	if len(s) > 12 {
		return s[:12]
	}
	return s
}

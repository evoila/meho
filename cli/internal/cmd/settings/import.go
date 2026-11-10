// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package settings

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

const yamlContentType = "application/yaml"

// importAnswer is the answer of POST /api/v1/settings/import. It is
// decoded here rather than through the generated type because a refused
// apply carries the same body on 422, where the generated type expects
// FastAPI's validation error.
type importAnswer struct {
	OK         bool      `json:"ok"`
	Mistakes   []string  `json:"mistakes"`
	Warnings   []string  `json:"warnings"`
	Diff       *diffBody `json:"diff"`
	Applied    bool      `json:"applied"`
	RevisionID *string   `json:"revision_id"`
	Failed     []string  `json:"failed"`
}

// newImportCmd returns `meho settings import`.
//
// Exit codes: 0 previewed or applied, 1 a wrong command line (--json
// without --yes on an apply), 2 auth_expired, 3 unreachable, 4 the file
// has mistakes or an item could not be written, 5 insufficient_role.
// Answering no exits 0, the same as `meho targets delete`.
func newImportCmd() *cobra.Command {
	var opts importOptions
	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Check a settings file, show what it changes, and apply it",
		Long: "import sends the file to POST /api/v1/settings/import. It always " +
			"shows the preview first: every mistake as one sentence, then the " +
			"diff per section. `not in file` lists what MEHO has and the file " +
			"does not; that is information only, because an import never " +
			"deletes.\n\n" +
			"--preview stops there and writes nothing. Otherwise the command " +
			"asks for a yes; --yes skips the question, and closed stdin counts " +
			"as no.\n\n" +
			"--json prints the answer of the endpoint as it is. An apply with " +
			"--json needs --yes, because there is no preview to read first.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.File = args[0]
			return runImport(cmd, opts)
		},
	}
	cmd.Flags().BoolVar(&opts.Preview, "preview", false, "show the mistakes and the diff; write nothing")
	cmd.Flags().BoolVar(&opts.Yes, "yes", false, "apply without asking")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "print the endpoint's answer as JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by 'meho login')")
	return cmd
}

type importOptions struct {
	File              string
	Preview           bool
	Yes               bool
	JSONOut           bool
	BackplaneOverride string
}

func runImport(cmd *cobra.Command, opts importOptions) error {
	if opts.JSONOut && !opts.Preview && !opts.Yes {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage("an import with --json needs --preview or --yes"), true)
	}
	data, err := os.ReadFile(opts.File)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("read %s: %v", opts.File, err)), opts.JSONOut)
	}
	backplaneURL, err := ResolveBackplane(cmd, opts.BackplaneOverride, opts.JSONOut)
	if err != nil {
		return err
	}
	ctx := cmd.Context()

	if opts.JSONOut && !opts.Preview {
		raw, answer, err := postImport(ctx, backplaneURL, data, false)
		if err != nil {
			return RenderError(cmd, backplaneURL, err, true)
		}
		printRaw(cmd.OutOrStdout(), raw)
		return applyOutcome(cmd, answer, true)
	}

	raw, preview, err := postImport(ctx, backplaneURL, data, true)
	if err != nil {
		return RenderError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		printRaw(cmd.OutOrStdout(), raw)
		return mistakesOutcome(cmd, preview, true)
	}

	out := cmd.OutOrStdout()
	printPreview(out, preview)
	if !preview.OK {
		return mistakesOutcome(cmd, preview, false)
	}
	if opts.Preview {
		fmt.Fprintln(out, "\nThis was a preview; nothing was written.")
		return nil
	}
	if preview.Diff == nil || !preview.Diff.hasChanges() {
		return nil
	}
	if !opts.Yes && !confirm(cmd) {
		fmt.Fprintln(out, "declined: nothing was written")
		return nil
	}

	_, applied, err := postImport(ctx, backplaneURL, data, false)
	if err != nil {
		return RenderError(cmd, backplaneURL, err, false)
	}
	printApply(out, applied)
	return applyOutcome(cmd, applied, false)
}

// postImport sends the file. A 422 is an answer too: the apply's way of
// reporting mistakes, with the preview's body.
func postImport(ctx context.Context, backplaneURL string, data []byte, preview bool) ([]byte, *importAnswer, error) {
	params := &api.ImportSettingsApiV1SettingsImportPostParams{Preview: &preview}
	resp, err := call(ctx, backplaneURL,
		func(ctx context.Context, authed *api.AuthedClient) (*http.Response, error) {
			return authed.ImportSettingsApiV1SettingsImportPostWithBody(
				ctx, params, yamlContentType, bytes.NewReader(data))
		},
		func(r *http.Response) int {
			if r.StatusCode == http.StatusUnauthorized {
				_ = r.Body.Close()
			}
			return r.StatusCode
		},
	)
	if err != nil {
		return nil, nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil, fmt.Errorf("read the answer: %w", err)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusUnprocessableEntity {
		return nil, nil, &HTTPError{Status: resp.StatusCode, Body: raw}
	}
	answer := &importAnswer{}
	if err := json.Unmarshal(raw, answer); err != nil {
		return nil, nil, &HTTPError{Status: resp.StatusCode, Body: raw}
	}
	return raw, answer, nil
}

func printRaw(w io.Writer, raw []byte) {
	fmt.Fprintln(w, strings.TrimSpace(string(raw)))
}

func confirm(cmd *cobra.Command) bool {
	fmt.Fprint(cmd.ErrOrStderr(), "\nApply these changes? [y/N] ")
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

func mistakesOutcome(cmd *cobra.Command, answer *importAnswer, jsonOut bool) error {
	if answer.OK {
		return nil
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf(
		"the file has %d mistake(s); nothing was written", len(answer.Mistakes))), jsonOut)
}

func applyOutcome(cmd *cobra.Command, answer *importAnswer, jsonOut bool) error {
	if len(answer.Mistakes) > 0 {
		return mistakesOutcome(cmd, answer, jsonOut)
	}
	if len(answer.Failed) == 0 {
		return nil
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf(
		"%d item(s) could not be written; the rest were", len(answer.Failed))), jsonOut)
}
